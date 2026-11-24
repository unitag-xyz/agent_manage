"""Read July JSON session indexes/transcripts directly, without starting OpenClaw.

Only selected messages are materialized. Offsets and a prefix fingerprint keep
pagination stable across append/reset; no session maintenance or writes occur.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import stat
import time
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .response import ProtocolError
from .segment_timing import count, timed

PAGE_BYTES = 320 * 1024
HISTORY_PAGE_BYTES = 1024 * 1024
CHUNK_BYTES = 96 * 1024
MEDIA_CHUNK_BYTES = 5 * 1024 * 1024
TTL_MS = 60 * 60 * 1000
CANONICAL = {"message", "thinking_level_change", "model_change", "compaction",
             "branch_summary", "custom", "custom_message", "label", "session_info"}
EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".svg": "image/svg+xml", ".mp3": "audio/mpeg", ".wav": "audio/wav",
              ".ogg": "audio/ogg", ".opus": "audio/opus", ".m4a": "audio/mp4", ".flac": "audio/flac",
              ".aac": "audio/aac", ".webm": "video/webm", ".mp4": "video/mp4", ".avif": "image/avif",
              ".heic": "image/heic", ".pdf": "application/pdf"}
MISSING = object()
# Hashes only, never media bytes. File identity includes ctime to detect rewrites.
_MEDIA_HASHES = OrderedDict()
_MEDIA_HASH_LOCK = threading.Lock()
_MEDIA_HASH_LIMIT = 256


def fail(code, message):
    level = 10 if code in {"SEGMENT_NOT_FOUND", "MESSAGE_NOT_FOUND", "MEDIA_NOT_FOUND"} else 12 if code == "CURSOR_EXPIRED" else 20 if code == "SEGMENT_READ_FAILED" else 11
    raise ProtocolError(code, message, level)


@timed("jsonEncodeMs")
def dumps(value, *, pretty=False):
    return json.dumps(value, ensure_ascii=False, **({"indent": 2} if pretty else {"separators": (",", ":")}))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def b64url(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64url(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def inside(file, root):
    return file == root or root in file.parents


def absolute(value):
    return Path(os.path.abspath(value))


def safe_file(file, roots):
    file = absolute(file)
    aliases = [alias for root in roots for alias in (absolute(root), absolute(root).resolve())]
    root = next((root for root in aliases if inside(file, root)), None)
    if root is None:
        fail("FORBIDDEN", "File is outside the allowed instance directory")
    current = root
    for part in file.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            fail("FORBIDDEN", "Symbolic links cannot be read")
    real = file.resolve(strict=True)
    if not inside(real, root.resolve()) or not real.is_file():
        fail("FORBIDDEN", "Invalid file")
    return real


def millis(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    return None


def short_text(message):
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
    return content[:180] if isinstance(content, str) else ""


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def tree_entry(raw):
    if raw.get("type") == "session" or "parentId" not in raw or not nonempty(raw.get("id")):
        return None
    parent = raw["parentId"]
    if parent is not None and not nonempty(parent):
        return None
    side = raw.get("appendMode") == "side"
    if raw.get("type") == "leaf":
        target = raw.get("targetId", MISSING)
        append = raw.get("appendParentId", target)
        if any(value is not None and not nonempty(value) for value in (target, append)) or raw.get("appendMode") not in (None, "side"):
            return None
        return {"id": raw["id"], "parent": target, "leaf": target, "append": append, "side": side}
    return {"id": raw["id"], "parent": parent, "leaf": raw["id"] if raw.get("type") in CANONICAL and not side else MISSING,
            "append": raw["id"], "side": side}


@timed("branchSelectionMs")
def select_rows(records):
    """July transcript-tree scan/path semantics, including opaque and side rows."""
    by_id, leaf, append, explicit_updates = {}, None, None, False
    for index, row in enumerate(records):
        raw = row["meta"]
        explicit = tree_entry(raw)
        node = explicit
        if raw.get("type") == "leaf" and explicit:
            if any(target is not None and target not in by_id for target in (node["leaf"], node["append"])):
                fail("SEGMENT_READ_FAILED", "Transcript contains an invalid branch selector")
        if node is None and raw.get("type") in CANONICAL and "parentId" not in raw and nonempty(raw.get("id")):
            node = {"id": raw["id"], "parent": leaf, "leaf": MISSING if raw.get("appendMode") == "side" else raw["id"],
                    "append": raw["id"], "side": raw.get("appendMode") == "side"}
        if node is None:
            continue
        if raw.get("type") in CANONICAL:
            parent = leaf if explicit and not node["side"] and node["parent"] == append and leaf != append else node["parent"]
            seen = set()
            while parent is not None and parent not in seen:
                seen.add(parent)
                ancestor = by_id.get(parent)
                if not ancestor or not ancestor["is_leaf"]:
                    break
                parent = ancestor["parent"]
            node["parent"] = parent
        node.update(index=index, is_leaf=raw.get("type") == "leaf" and explicit is not None)
        by_id[node["id"]] = node
        append = node["append"]
        if node["leaf"] is not MISSING:
            leaf = node["leaf"]
            explicit_updates |= explicit is not None
    if not explicit_updates:
        return records
    selected, seen = [], set()
    while leaf:
        if leaf in seen:
            return []
        seen.add(leaf)
        node = by_id.get(leaf)
        if not node:
            break
        if not node["is_leaf"]:
            selected.append(records[node["index"]])
        leaf = node["parent"]
    return list(reversed(selected))


def generated_id(file):
    name = Path(file).name
    if not name.endswith(".jsonl"):
        return None
    name = name[:-6].split("-topic-", 1)[0]
    fork = re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\w-]+(?:Z|[+-]\d{2}(?:-\d{2})?)?_(.+)", name)
    name = fork[1] if fork else name
    return name if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", name, re.I) else None


class SegmentReader:
    @timed("readerInitMs")
    def __init__(self, request, env):
        self.input, self.env = request, env
        self.root, self.key, self.session_id = absolute(request["root"]), request["sessionKey"], request["sessionId"]
        self.config = request["config"]
        self.agent_id = self.key.split(":")[1]
        agents = self.config.get("agents", {})
        self.agent = next((agent for agent in agents.get("list", []) if agent.get("id") == self.agent_id), None)
        if self.agent is None and self.agent_id != "main":
            fail("FORBIDDEN", "Agent is not configured in this instance")
        store = self.config.get("session", {}).get("store")
        self.native_path_resolver = request.get("_nativePathResolver")
        if self.native_path_resolver is not None and not callable(self.native_path_resolver):
            fail("FORBIDDEN", "Native path resolution requires an internal trusted binding")
        store_path = store.replace("{agentId}", self.agent_id.lower()) if store else None
        self.store = (self.native_path_resolver(store_path) if self.native_path_resolver else absolute(self.expand_home(store_path))) if store else self.root / "agents" / self.agent_id.lower() / "sessions/sessions.json"
        self.roots = [self.store.parent, self.root / "sessions"]
        self.scope = digest(dumps([request["root"], self.key, self.session_id]).encode())
        if self.native_path_resolver:
            self.scope = digest(dumps([request["root"], self.key, self.session_id, request["scopeBinding"]]).encode())
        self.token_key = digest(dumps([request["root"], self.config.get("gateway", {}).get("auth") or {}, env.get("AGENT_MANAGER_HISTORY_CURSOR_KEY", "")]).encode()).encode()
        workspace = (self.agent or {}).get("workspace") or agents.get("defaults", {}).get("workspace")
        self.media_roots = [self.root / "workspace", self.root / "media"]
        if workspace:
            self.media_roots[0] = self.native_path_resolver(workspace) if self.native_path_resolver else absolute(self.expand_home(workspace))
        self.media_identities = {}

    def expand_home(self, value):
        return self.env.get("HOME", str(Path.home())) + value[1:] if value == "~" or value.startswith("~/") else value

    def open_read(self, file):
        if self.native_path_resolver:
            from .path_io import open_without_links
            return open_without_links(file)
        return os.open(file, os.O_RDONLY | os.O_NOFOLLOW)

    def checked_file(self, file, roots):
        checked = safe_file(file, roots)
        # Strict opens must walk the original path, not an already-resolved link target.
        return absolute(file) if self.native_path_resolver else checked

    @timed("indexReadMs")
    def entry(self):
        try:
            file = self.checked_file(self.store, [self.store.parent])
            fd = self.open_read(file)
            with os.fdopen(fd, "rb") as stream:
                store = json.load(stream)
        except FileNotFoundError:
            store = {}
        if not isinstance(store, dict):
            fail("SEGMENT_READ_FAILED", "Invalid session index")
        # Exact keys avoid folding case-sensitive channel/thread identifiers. The
        # frontend supplies the original logical key from the native family index.
        entry = store.get(self.key)
        if not isinstance(entry, dict) or self.session_id not in [entry.get("sessionId"), *entry.get("usageFamilySessionIds", [])]:
            fail("FORBIDDEN", "Physical segment does not belong to this logical thread")
        return entry

    def token(self, value):
        body = b64url(dumps({**value, "scope": self.scope, "v": 1}).encode())
        return body + "." + b64url(hmac.new(self.token_key, body.encode(), hashlib.sha256).digest())

    @timed("referenceValidationMs")
    def decode(self, value, kind):
        try:
            body, signature = value.split(".")
            expected = b64url(hmac.new(self.token_key, body.encode(), hashlib.sha256).digest())
            data = json.loads(unb64url(body))
            if not hmac.compare_digest(signature, expected) or data.get("v") != 1 or data.get("scope") != self.scope or data.get("kind") != kind or type(data.get("expires")) is not int or data["expires"] < time.time() * 1000:
                raise ValueError()
            return data
        except (ValueError, TypeError, KeyError, AttributeError):
            fail("CURSOR_EXPIRED", "Reference expired or belongs to another instance, thread or segment")

    def candidates(self, entry):
        base = self.store.parent
        session_file = entry.get("sessionFile") if self.session_id == entry.get("sessionId") else None
        if self.session_id != entry.get("sessionId"):
            topic = re.search(r":topic:([^:]+)$", self.key)
            session_file = str(base / (self.session_id + ("-topic-" + quote(topic[1], safe="~()*!.'-") if topic else "") + ".jsonl"))
        explicit = None
        if isinstance(session_file, str) and session_file.strip():
            candidate = (self.native_path_resolver(session_file.strip())
                         if self.native_path_resolver and session_file.strip().startswith("/") and self.session_id == entry.get("sessionId")
                         else absolute(base / session_file.strip()))
            # Invalid explicit paths fall back to the generated filename, as July
            # does. Existing in-root links remain visible to safe_file for rejection.
            if inside(candidate, base) or inside(candidate, base.resolve()):
                explicit = candidate
        stale = explicit is not None and generated_id(explicit) not in (None, self.session_id)
        candidates = ([explicit] if explicit and not stale else []) + [base / (self.session_id + ".jsonl")]
        if explicit and stale:
            candidates.append(explicit)
        candidates += [self.root / "agents" / self.agent_id.lower() / "sessions" / (self.session_id + ".jsonl"),
                       Path(self.env.get("HOME", str(Path.home()))) / ".openclaw/sessions" / (self.session_id + ".jsonl")]
        return list(dict.fromkeys(path for path in candidates if any(inside(path, root) or inside(path, root.resolve()) for root in self.roots)))

    def archives(self, candidate, *, all_names=False):
        if not candidate.name.endswith(".jsonl") or not candidate.parent.is_dir():
            return []
        matches = []
        for file in candidate.parent.iterdir():
            if not file.name.startswith(candidate.name + ".reset."):
                continue
            if all_names:
                matches.append((0, file.name, file))
                continue
            suffix = file.name.split(".reset.")[-1]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:\.\d{3})?Z", suffix) or not stat.S_ISREG(file.lstat().st_mode):
                continue
            stamp = millis(suffix[:11] + suffix[11:].replace("-", ":"))
            if stamp is not None:
                matches.append((stamp, file.name, file))
        return sorted(matches, reverse=True) if all_names else sorted(matches, reverse=True)[:128]

    def archive_header_matches(self, file):
        try:
            fd = self.open_read(self.checked_file(file, self.roots))
            with os.fdopen(fd, "rb") as stream:
                for line in stream.read(64 * 1024).splitlines():
                    if line.strip():
                        raw = json.loads(line)
                        return isinstance(raw, dict) and raw.get("type") == "session" and raw.get("id") == self.session_id
        except (OSError, ValueError):
            return False
        return False

    @timed("fileLocateAndOpenMs")
    def open_transcript(self, entry, snapshot):
        candidates = self.candidates(entry)
        available = [candidate for candidate in candidates if candidate.exists()]
        grouped = {}
        for candidate in candidates:
            for stamp, name, file in self.archives(candidate):
                if generated_id(candidate) != self.session_id and not self.archive_header_matches(file):
                    continue
                current = grouped.get(candidate.name)
                if current is None or (stamp, name) > current[:2]:
                    grouped[candidate.name] = (stamp, name, file)
                break
        available += [value[2] for value in grouped.values()]
        if snapshot:
            for candidate in candidates:
                available += [value[2] for value in self.archives(candidate, all_names=True)]
        for candidate in dict.fromkeys(available):
            try:
                checked = self.checked_file(candidate, self.roots)
                fd = self.open_read(checked)
            except FileNotFoundError:
                continue
            opened = os.fstat(fd)
            if snapshot and (str(opened.st_dev) != snapshot.get("dev") or str(opened.st_ino) != snapshot.get("ino")):
                os.close(fd)
                continue
            return fd, opened
        fail("CURSOR_EXPIRED" if snapshot else "SEGMENT_NOT_FOUND", "Transcript snapshot is no longer available" if snapshot else "Physical segment transcript is missing")

    @timed("transcriptScanMs")
    def scan(self, fd, opened, entry, snapshot):
        boundary = snapshot.get("bytes") if snapshot else opened.st_size
        if type(boundary) is not int or not 0 <= boundary <= opened.st_size:
            fail("CURSOR_EXPIRED", "Transcript was truncated")
        if self.input.get("maxTranscriptBytes") and boundary > self.input["maxTranscriptBytes"]:
            fail("TRANSCRIPT_TOO_LARGE", "Transcript exceeds this service's read budget")
        records, hasher, committed, at = [], hashlib.sha256(), 0, 0
        # Buffered readline avoids repeatedly copying large incomplete lines.
        with os.fdopen(os.dup(fd), "rb") as stream:
            while at < boundary:
                record_limit = self.input.get("maxRecordBytes", boundary - at)
                line = stream.readline(min(boundary - at, record_limit + 1))
                if len(line) > record_limit:
                    fail("MESSAGE_TOO_LARGE", "Stored record exceeds this service's read budget")
                if not line:
                    fail("CURSOR_EXPIRED", "Transcript changed during read")
                end = at + len(line)
                if not line.strip():
                    if line.endswith(b"\n"):
                        hasher.update(line)
                        committed = end
                    at = end
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    if not line.endswith(b"\n") and not snapshot and entry.get("sessionId") == self.session_id:
                        break
                    fail("SEGMENT_READ_FAILED", "Transcript contains a malformed complete record")
                if not isinstance(raw, dict):
                    fail("SEGMENT_READ_FAILED", "Invalid transcript record")
                if raw.get("type") == "session" and raw.get("id") != self.session_id:
                    fail("FORBIDDEN", "Transcript header belongs to another physical segment")
                message = raw.get("message") if raw.get("message") is not None else {}
                if not isinstance(message, dict):
                    fail("SEGMENT_READ_FAILED", "Invalid stored message")
                records.append({"meta": {key: raw[key] for key in ("type", "id", "parentId", "targetId", "appendMode", "appendParentId") if key in raw},
                                "start": at, "length": len(line), "timestamp": millis(raw.get("timestamp") if raw.get("timestamp") is not None else message.get("timestamp")),
                                "role": message.get("role"), "preview": short_text(message), "hasMessage": isinstance(raw.get("message"), dict)})
                if self.input.get("maxRecords") and len(records) > self.input["maxRecords"]:
                    fail("TRANSCRIPT_TOO_LARGE", "Transcript record count exceeds this service's read budget")
                hasher.update(line)
                committed = end
                at = end
        fingerprint = hasher.hexdigest()
        if snapshot and (committed != snapshot.get("bytes") or fingerprint != snapshot.get("hash")):
            fail("CURSOR_EXPIRED", "Transcript snapshot changed")
        self.snapshot = snapshot or {"dev": str(opened.st_dev), "ino": str(opened.st_ino), "bytes": committed, "hash": fingerprint}
        self.fd = fd
        count("transcriptBytes", committed)
        count("recordsScanned", len(records))
        selected = select_rows(records)
        self.rows = [row for row in selected if row["hasMessage"] or row["meta"].get("type") in {"compaction", "custom_message"}]
        count("messagesSelected", len(self.rows))
        self.by_id = {}
        for row in self.rows:
            identifier = self.identifier(row)
            if identifier in self.by_id:
                fail("SEGMENT_READ_FAILED", "Transcript contains duplicate message IDs")
            self.by_id[identifier] = row
        return records

    @staticmethod
    def identifier(row):
        return row["meta"].get("id") or "entry-" + format(row["start"], "x")

    @timed("snapshotVerificationMs")
    def finish(self, payload):
        hasher, at = hashlib.sha256(), 0
        while at < self.snapshot["bytes"]:
            block = os.pread(self.fd, min(64 * 1024, self.snapshot["bytes"] - at), at)
            if not block:
                fail("CURSOR_EXPIRED", "Transcript changed before response")
            hasher.update(block)
            at += len(block)
        if hasher.hexdigest() != self.snapshot["hash"]:
            fail("CURSOR_EXPIRED", "Transcript changed before response")
        count("snapshotVerifyBytes", at)
        count("messagesReturned", len(payload.get("messages", [])) + int("message" in payload))
        latest = self.entry()
        if "segment" in payload:
            payload["segment"]["isCurrent"] = latest.get("sessionId") == self.session_id
        if self.input.get("includeSnapshot"):
            payload["snapshotId"] = digest(dumps([self.scope, self.snapshot]).encode())
        return payload

    def ref_token(self, kind, **values):
        return self.token({"kind": kind, "snapshot": self.snapshot, "expires": self.expires, **values})

    def message_ref(self, row, view, omit_thinking=False):
        identifier = self.identifier(row)
        return {"method": "sessions.segments.message.get", "params": {"sessionKey": self.key, "sessionId": self.session_id,
                "messageId": identifier, "ref": self.ref_token("message", messageId=identifier, view=view, omitThinking=omit_thinking)}}

    @timed("messageReadMs")
    def raw(self, row):
        data = os.pread(self.fd, row["length"], row["start"])
        if len(data) != row["length"]:
            fail("CURSOR_EXPIRED", "Message snapshot changed")
        return json.loads(data)

    @staticmethod
    def local_media(value):
        return isinstance(value, str) and not re.match(r"https?://", value, re.I) and Path(value).suffix.lower() in EXTENSIONS

    @timed("mediaReadAndHashMs")
    def local_media_read(self, value, *, with_bytes=False, offset=0, length=MEDIA_CHUNK_BYTES):
        if not self.local_media(value):
            fail("FORBIDDEN", "Unsupported media reference")
        if value.startswith("file://"):
            url = urlparse(value)
            if url.netloc not in ("", "localhost"):
                fail("FORBIDDEN", "Unsupported media reference")
            target = Path(unquote(url.path))
        else:
            target = self.media_roots[0] / value
        if self.native_path_resolver and (value.startswith("file://") or Path(value).is_absolute()):
            from .core.errors import APIError
            try:
                target = self.native_path_resolver(str(target))
            except APIError:
                fail("FORBIDDEN", "Media path is outside the registered mounts")
        try:
            file = self.checked_file(target, self.media_roots)
            fd = self.open_read(file)
        except FileNotFoundError:
            fail("MEDIA_NOT_FOUND", "Media file is missing")
        try:
            if self.native_path_resolver and os.fstat(fd).st_size > 64 * 1024 * 1024:
                fail("FORBIDDEN", "Host media exceeds the read budget")
            def signature():
                value = os.fstat(fd)
                return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
            before = signature()
            key = (file, before)
            with _MEDIA_HASH_LOCK:
                result = _MEDIA_HASHES.get(key)
                if result is not None:
                    _MEDIA_HASHES.move_to_end(key)
            if result is None:
                hasher = hashlib.sha256()
                while True:
                    block = os.read(fd, 128 * 1024)
                    if not block:
                        break
                    hasher.update(block)
                    count("mediaBytesRead", len(block))
                if signature() != before:
                    fail("CURSOR_EXPIRED", "Media changed during read")
                result = {"sha256": hasher.hexdigest(), "contentType": EXTENSIONS[file.suffix.lower()],
                          "totalBytes": before[2]}
                with _MEDIA_HASH_LOCK:
                    _MEDIA_HASHES[key] = result
                    _MEDIA_HASHES.move_to_end(key)
                    while len(_MEDIA_HASHES) > _MEDIA_HASH_LIMIT:
                        _MEDIA_HASHES.popitem(last=False)
            else:
                count("mediaCacheHits")
            data = None
            if with_bytes:
                if type(offset) is not int or not 0 <= offset <= before[2]:
                    fail("CURSOR_EXPIRED", "Invalid chunk boundary")
                data = os.pread(fd, min(length, before[2] - offset), offset)
                count("mediaBytesRead", len(data))
                if len(data) != min(length, before[2] - offset):
                    fail("CURSOR_EXPIRED", "Media changed during read")
            if signature() != before:
                fail("CURSOR_EXPIRED", "Media changed during read")
            return {**result, **({"bytes": data} if with_bytes else {})}
        finally:
            os.close(fd)

    @staticmethod
    def inline_bytes(value):
        encoded = re.sub(r"\s", "", value)
        if not encoded or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded) or len(encoded) % 4 == 1:
            raise ValueError("Invalid base64")
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        if not data or base64.b64encode(data).decode().rstrip("=") != encoded.rstrip("="):
            raise ValueError("Invalid base64")
        return data

    def media_ref(self, row, pointer, source, value, purpose="unknown", inline=None):
        media_hash = None
        metadata = {}
        try:
            if source == "base64":
                data = self.inline_bytes(value)
                media_hash = digest(data)
                metadata["sizeBytes"] = len(data)
                name = (inline or {}).get("fileName")
                mime = (inline or {}).get("mimeType")
                if isinstance(mime, str) and mime in EXTENSIONS.values():
                    metadata["mimeType"] = mime
            else:
                media = self.local_media_read(value)
                media_hash = media["sha256"]
                metadata.update(sizeBytes=media["totalBytes"], mimeType=media["contentType"])
                name = Path(unquote(urlparse(value).path) if value.startswith("file://") else value).name
            if (isinstance(name, str) and 0 < len(name) <= 128 and name.isprintable()
                    and name not in (".", "..") and not any(char in name for char in "/\\:")):
                metadata["fileName"] = name
        except (OSError, ValueError, ProtocolError):
            pass
        return {**({"mediaId": "sha256:" + media_hash} if media_hash else {}),
                **({"purpose": purpose, **metadata} if self.input.get("includeSnapshot") else {}), "method": "sessions.segments.media.get",
                "params": {"sessionKey": self.key, "sessionId": self.session_id,
                           "ref": self.ref_token("media", messageId=self.identifier(row), pointer=pointer, source=source,
                                                 **({"mediaHash": media_hash} if media_hash else {}))}}

    @timed("messageBuildMs")
    def materialize(self, row, *, view="full", omit_thinking=False):
        raw = self.raw(row)
        message = raw["message"] if isinstance(raw.get("message"), dict) else ({"role": "system", "content": raw.get("content"), "customType": raw.get("customType")} if raw.get("type") == "custom_message" else {"role": "system", "content": [{"type": "text", "text": "Compaction"}], "compaction": raw})
        message.update(id=self.identifier(row), timestamp=millis(message.get("timestamp")) if millis(message.get("timestamp")) is not None else row["timestamp"])
        message["__openclaw"] = {**message.get("__openclaw", {}), "id": self.identifier(row), **({"kind": raw.get("type")} if raw.get("type") != "message" else {})}
        media, identities = [], set()

        def add(reference):
            identity = (reference.get("mediaId"), reference.get("purpose"))
            if identity in identities:
                return
            if reference.get("mediaId"):
                identities.add(identity)
            media.append(reference)

        def visit(value, pointer, media_array=False, purpose="unknown"):
            if isinstance(value, list):
                # July/Node references use Object.entries array keys as strings.
                children = [(str(index), child) for index, child in enumerate(value)]
            elif isinstance(value, dict):
                if value.get("type") in {"toolCall", "tool_call", "tool_use", "functionCall"}:
                    purpose = "tool_input"
                if value.get("type") in {"image", "base64"} and isinstance(value.get("data"), str):
                    reference = self.media_ref(row, [*pointer, "data"], "base64", value.pop("data"), purpose, value)
                    value["mediaRef"] = reference
                    add(reference)
                children = list(value.items())
            else:
                return
            for key, child in children:
                if key == "mediaRef":
                    continue
                if isinstance(child, str) and (media_array or key in {"url", "path", "filePath", "MediaPath"}) and self.local_media(child):
                    add(self.media_ref(row, [*pointer, key], "path", child, purpose))
                elif isinstance(child, str) and key in {"text", "content"}:
                    for match in re.finditer(r'MEDIA:\s*([^\s"<>]+)', child):
                        if self.local_media(match[1]):
                            add(self.media_ref(row, [*pointer, key], "text:" + match[1], match[1], purpose))
                elif isinstance(child, (dict, list)):
                    visit(child, [*pointer, key], isinstance(child, list) and key in {"paths", "savedPaths", "mediaUrls", "MediaPaths"}, purpose)
        visit(message, [], purpose={"user": "user_input", "assistant": "assistant_output", "toolResult": "tool_output",
                                   "tool": "tool_output", "function": "tool_output"}.get(message.get("role"), "unknown"))
        if media:
            message["mediaRefs"] = media
        if omit_thinking:
            message.pop("thinking", None)
            if isinstance(message.get("content"), list):
                message["content"] = [block for block in message["content"]
                                      if not isinstance(block, dict) or block.get("type") not in {"thinking", "redacted_thinking", "reasoning"}]
        strip_thinking_signatures(message)
        # Build references against original field positions before removing tool
        # details. Otherwise an image following a text result would point at the
        # wrong raw block after compaction.
        return compact_tools(message) if view == "compact" else message

    def locate(self, identifier):
        if identifier not in self.by_id:
            fail("MESSAGE_NOT_FOUND", "Message is not present in this segment snapshot")
        return self.by_id[identifier]

    def usage_log(self, row):
        raw = self.raw(row)
        message = raw["message"]
        result = {"messageId": self.identifier(row), "role": row["role"], "timestamp": row["timestamp"]}
        for field in ("model", "provider", "runId", "toolCallId"):
            value = message.get(field) or raw.get(field)
            if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable():
                result[field] = value
        duration = message.get("durationMs", raw.get("durationMs"))
        if type(duration) is int and 0 <= duration <= (1 << 53) - 1:
            result["durationMs"] = duration
        usage = message.get("usage")
        if isinstance(usage, dict):
            counters = {name: usage[name] for name in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
                        if type(usage.get(name)) is int and 0 <= usage[name] <= (1 << 53) - 1}
            total = usage.get("totalTokens", usage.get("total"))
            if type(total) is int and 0 <= total <= (1 << 53) - 1:
                counters["totalTokens"] = total
                result["tokens"] = total
            if counters:
                result["usage"] = counters
            cost = usage.get("cost")
            amount = cost.get("total") if isinstance(cost, dict) else cost
            if type(amount) in (int, float) and 0 <= amount <= (1 << 53) - 1:
                result["cost"] = amount
        tool = message.get("toolName")
        details = message.get("details")
        kinds = {"image_generate": "image", "video_generate": "video", "music_generate": "audio", "tts": "audio"}
        count = details.get("count") if isinstance(details, dict) else None
        if (tool == "tts" and isinstance(details, dict)
                and isinstance(details.get("audioPath"), str) and details["audioPath"]):
            count = 1
        if (row["role"] in ("tool", "toolResult") and isinstance(tool, str) and tool in kinds
                and isinstance(details, dict) and message.get("isError") is not True
                and isinstance(result.get("toolCallId"), str)
                and type(count) is int and 0 <= count <= 1000):
            media = {"kind": kinds[tool], "outputCount": count}
            for name in ("provider", "model"):
                text = details.get(name)
                if isinstance(text, str) and 0 < len(text) <= 120 and re.fullmatch(r"[a-zA-Z0-9_./:-]+", text):
                    media[name] = text
            duration = details.get("durationSeconds")
            if type(duration) in (int, float) and 0 < duration <= 86400:
                media["durationSeconds"] = duration
            task = details.get("task")
            if isinstance(task, dict):
                task_id = task.get("taskId")
                if isinstance(task_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", task_id):
                    media["taskId"] = task_id
                native_run = task.get("runId")
                if isinstance(native_run, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", native_run):
                    result.setdefault("runId", native_run)
            result["mediaGeneration"] = media
        return result

    @timed("chunkEncodeMs")
    def chunk(self, data, offset, reference, *, total_size=None, **extra):
        total = len(data) if total_size is None else total_size
        if type(offset) is not int or not 0 <= offset <= total:
            fail("CURSOR_EXPIRED", "Invalid chunk boundary")
        chunk_bytes = self.input.get("mediaLength", MEDIA_CHUNK_BYTES) if self.input["action"] == "media" else CHUNK_BYTES
        end = min(total, offset + chunk_bytes)
        part = data[offset:end] if total_size is None else data[:end - offset]
        count("chunkBytes", end - offset)
        values = {"action": self.input["action"], "messageId": extra["messageId"], "offset": end}
        if self.input["action"] == "media":
            values.update(pointer=reference["pointer"], source=reference["source"], mediaHash=extra["sha256"])
        elif "view" in extra:
            values["view"] = extra["view"]
            values["omitThinking"] = extra.get("omitThinking", False)
            values["format"] = 2
        return {**extra, "encoding": "base64", "data": base64.b64encode(part).decode(), "offset": offset,
                "totalBytes": total, "nextCursor": self.ref_token("chunk", **values) if end < total else None}

    def run(self):
        entry = self.entry()
        action, cursor, ref = self.input["action"], self.input.get("cursor"), self.input.get("ref")
        reference = self.decode(cursor, "page" if action == "history" else "chunk") if cursor else self.decode(ref, "message" if action == "message" else "media") if ref else None
        snapshot = reference.get("snapshot") if reference else None
        if reference and not isinstance(snapshot, dict):
            fail("CURSOR_EXPIRED", "Invalid snapshot")
        fd, opened = self.open_transcript(entry, snapshot)
        try:
            records = self.scan(fd, opened, entry, snapshot)
            self.expires = reference["expires"] if reference else int(time.time() * 1000) + TTL_MS
            base = {"sessionKey": self.key, "sessionId": self.session_id}
            if action == "usage":
                limit = self.input["limit"]
                rows = [row for row in self.rows if row["hasMessage"] and row["role"] in ("user", "assistant", "tool", "toolResult")]
                return self.finish({"logs": [self.usage_log(row) for row in rows[-limit:]]})
            if action == "message":
                identifier = self.input["messageId"]
                if reference and (reference.get("messageId") != identifier or cursor and reference.get("action") != "message"):
                    fail("CURSOR_EXPIRED", "Message reference mismatch")
                if cursor and reference.get("format") != 2:
                    fail("CURSOR_EXPIRED", "Message output format changed; restart the message read")
                row = self.locate(identifier)
                view = reference.get("view", "full") if reference else "full"
                omit_thinking = reference.get("omitThinking", False) if reference else False
                message = self.materialize(row, view=view, omit_thinking=omit_thinking)
                base["messageId"] = identifier
                base["view"] = view
                base["omitThinking"] = omit_thinking
                if not cursor and len(dumps(message, pretty=True).encode()) < PAGE_BYTES:
                    return self.finish({**base, "message": message})
                data = dumps(message).encode()
                return self.finish(self.chunk(data, reference.get("offset", 0) if reference else 0, reference, **base, contentType="application/json", sha256=digest(data)))
            if action == "media":
                if not reference or cursor and reference.get("action") != "media":
                    fail("CURSOR_EXPIRED", "Invalid media reference")
                if "mediaOffset" in self.input:
                    offset, length = self.input["mediaOffset"], self.input["mediaLength"]
                    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MEDIA_CHUNK_BYTES:
                        fail("CURSOR_EXPIRED", "Invalid media range")
                    reference = {**reference, "offset": offset}
                row = self.locate(reference.get("messageId"))
                raw = self.raw(row)
                value = raw.get("message") or {"content": raw.get("content")}
                pointer = reference.get("pointer")
                if not isinstance(pointer, list):
                    fail("CURSOR_EXPIRED", "Invalid media pointer")
                parent = None
                try:
                    for key in pointer:
                        if isinstance(value, list) and isinstance(key, str) and key.isdigit():
                            key = int(key)
                        parent, value = value, value[key]
                except (IndexError, KeyError, TypeError):
                    fail("MEDIA_NOT_FOUND", "Media reference is no longer present")
                if not isinstance(value, str):
                    fail("MEDIA_NOT_FOUND", "Media reference is no longer present")
                source = reference.get("source", "")
                total_size = None
                if source == "base64":
                    data = self.inline_bytes(value)
                    content_type = next((parent[key] for key in ("mimeType", "media_type", "mime_type") if parent.get(key)), "application/octet-stream")
                    media_hash = digest(data)
                else:
                    target = source[5:] if source.startswith("text:") else value
                    if source.startswith("text:") and "MEDIA:" + target not in value and "MEDIA: " + target not in value:
                        fail("MEDIA_NOT_FOUND", "Media marker changed")
                    media = self.local_media_read(target, with_bytes=True, offset=reference.get("offset", 0), length=self.input.get("mediaLength", MEDIA_CHUNK_BYTES))
                    total_size = media["totalBytes"]
                    data, content_type, media_hash = media["bytes"], media["contentType"], media["sha256"]
                if reference.get("mediaHash") and reference["mediaHash"] != media_hash:
                    fail("CURSOR_EXPIRED", "Media changed during chunk read")
                return self.finish(self.chunk(data, reference.get("offset", 0), reference, total_size=total_size, **base, messageId=self.identifier(row), contentType=content_type, sha256=media_hash))
            view = self.input.get("view") or (reference.get("view", "full") if reference else "compact")
            if reference and self.input.get("view") and view != reference.get("view", "full"):
                fail("CURSOR_EXPIRED", "Page view changed; reopen the segment")
            omit_thinking = self.input.get("omitThinking")
            if omit_thinking is None:
                omit_thinking = reference.get("omitThinking", False) if reference else False
            end = reference.get("position") if reference else 0 if self.input["edge"] == "start" else len(self.rows)
            forward = reference.get("direction") == "newer" if reference else self.input["edge"] == "start"
            if type(end) is not int or not 0 <= end <= len(self.rows):
                fail("CURSOR_EXPIRED", "Invalid page boundary")
            messages, start, stop, size = [], end, end, 0
            until_user = not forward and self.input["limit"] != 1
            reached_user = False
            while (len(messages) < self.input["limit"] or (until_user and not reached_user)) and (stop < len(self.rows) if forward else start > 0):
                row = self.rows[stop if forward else start - 1]
                message = self.materialize(row, view=view, omit_thinking=omit_thinking)
                if (omit_thinking or view == "compact") and not has_display_content(message):
                    # Advance the physical boundary even when filtering removes a row.
                    # The page limit counts returned messages, not scanned records.
                    if forward:
                        stop += 1
                    else:
                        start -= 1
                    continue
                count = len(dumps(message, pretty=True).encode())
                if count > PAGE_BYTES / 2:
                    message = {"id": self.identifier(row), "role": row["role"] or "system", "timestamp": row["timestamp"], "content": [],
                               "__openclaw": {"id": self.identifier(row), "truncated": True, "reason": "full_message_reference"}, "fullMessageRef": self.message_ref(row, view, omit_thinking)}
                    count = len(dumps(message, pretty=True).encode())
                if messages and size + count > HISTORY_PAGE_BYTES:
                    break
                size += count
                if forward:
                    messages.append(message)
                    stop += 1
                else:
                    messages.insert(0, message)
                    start -= 1
                if message.get("role") == "user":
                    reached_user = True
            times = [row["timestamp"] for row in records if row["timestamp"] is not None]
            preview = [next((row["preview"] for row in reversed(self.rows) if row["role"] == role), "") for role in ("user", "assistant")]
            return self.finish({**base, "view": view, **({"omitThinking": omit_thinking} if self.input.get("includeSnapshot") else {}), "segment": {"startedAt": min(times) if times else None, "endedAt": max(times) if times else None,
                "isCurrent": entry.get("sessionId") == self.session_id, "readable": True, "preview": preview}, "messages": messages, "reachedUser": reached_user,
                "olderCursor": self.ref_token("page", position=start, direction="older", view=view, omitThinking=omit_thinking) if start > 0 else None,
                "newerCursor": self.ref_token("page", position=stop, direction="newer", view=view, omitThinking=omit_thinking) if stop < len(self.rows) else None})
        finally:
            os.close(fd)


def has_display_content(message):
    # Preserve tool results (including status-only results), control records,
    # and user messages; only omit empty assistant display messages.
    if message.get("role") != "assistant" or message.get("__openclaw", {}).get("kind"):
        return True
    if any(message.get(key) for key in ("thinking", "toolSummary", "tool_calls", "function_call",
                                       "media", "mediaRefs", "MediaPath", "MediaPaths", "mediaUrls", "savedPaths")):
        return True
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                if block.strip():
                    return True
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    if str(block.get("text") or "").strip():
                        return True
                elif block:
                    # Unknown blocks may carry attachments or tool information.
                    return True
            elif block:
                return True
        return False
    return bool(content)


def preview_arguments(arguments):
    if isinstance(arguments, str):
        try:
            # Some stored tool calls encode their arguments as JSON strings.
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    if not isinstance(arguments, dict):
        return {}
    # Never summarize a code body by copying it; only show a bounded command/path.
    for key in ("command", "cmd", "path", "file_path", "filePath", "url", "query", "pattern"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            preview = re.sub(r"\s+", " ", value[:512]).strip()[:180]
            if preview:
                return {key: preview}
    return {}


def strip_thinking_signatures(value):
    """Display output needs readable thinking, not provider replay metadata."""
    if isinstance(value, dict):
        value.pop("thinkingSignature", None)
        for child in value.values():
            strip_thinking_signatures(child)
    elif isinstance(value, list):
        for child in value:
            strip_thinking_signatures(child)


@timed("toolCompactMs")
def compact_tools(message):
    content = message.get("content")
    changed = False
    if isinstance(content, list):
        compacted = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"toolCall", "tool_use", "functionCall"}:
                item = {key: block[key] for key in ("type", "id", "name", "toolCallId") if key in block}
                arguments = preview_arguments(block.get("arguments") or block.get("input"))
                preview = next(iter(arguments.values()), "")
                item.update(arguments=arguments, summary=preview or item.get("name", "Tool call"))
                if "input" in block:
                    item["input"] = arguments.copy()
                compacted.append(item)
                changed = True
            else:
                compacted.append(block)
        message["content"] = compacted
    if message.get("role") in {"toolResult", "tool", "function"}:
        # Preserve images/media blocks and attachment metadata, not arbitrary
        # result details containing source files, logs or complete code output.
        keep = {"id", "role", "timestamp", "toolCallId", "toolName", "name", "isError", "__openclaw",
                "media", "MediaPath", "MediaPaths", "mediaUrls", "savedPaths", "mediaRefs"}
        message = {key: value for key, value in message.items() if key in keep}
        blocks = content if isinstance(content, list) else []
        images = [block for block in blocks if isinstance(block, dict) and block.get("type") in {"image", "audio", "file"}]
        markers = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                markers.extend(match[0] for match in re.finditer(r'MEDIA:\s*[^\s"<>]+', block.get("text", "")))
        message["content"] = images + ([{"type": "text", "text": "\n".join(dict.fromkeys(markers))}] if markers else [])
        message["toolSummary"] = {"name": message.get("toolName") or message.get("name") or "Tool result",
                                  "status": "error" if message.get("isError") is True else "success" if message.get("isError") is False else "unknown"}
        changed = True
    if changed:
        message["__openclaw"] = {**message.get("__openclaw", {}), "compacted": True, "reason": "tool_details_omitted"}
    return message


def read_segment(request, env):
    try:
        return SegmentReader(request, env).run()
    except ProtocolError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        # No raw file text, credentials or implementation diagnostics in errors.
        fail("SEGMENT_READ_FAILED", "Could not read the stored transcript")
