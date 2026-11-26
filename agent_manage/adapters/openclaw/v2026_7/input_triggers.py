"""Correlate native user records without storing their text or guessing turn order."""
import sqlite3
import threading
from collections import OrderedDict

from ....core.store import canonical


def valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()


def evidence(identifier, key):
    if valid_id(identifier) and isinstance(key, str) and len(key) <= 261 and key.endswith(":user") and valid_id(key[:-5]):
        return key[:-5], identifier
    return None


def event_evidence(payload):
    message, mid = payload.get("message"), payload.get("messageId")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    key = message.get("idempotencyKey")
    meta = message.get("__openclaw", {})
    if not isinstance(meta, dict) or meta.get("id", mid) != mid or meta.get("idempotencyKey", key) != key:
        return None
    return evidence(mid, key)


class InputTriggers:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime
        self.pending = OrderedDict()
        self.lock = threading.Lock()

    def remember(self, thread, segment, pairs):
        if not pairs:
            return
        try:
            with self.lock, self.store.connect() as db:
                # Enrichment is optional: do not stall chat behind a contended writer.
                db.execute("PRAGMA busy_timeout=40")
                self._merge(db, thread, segment, pairs)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorname", "")  # Python 3.11+ exposes the SQLite code.
            if not code.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")) and str(exc) not in (
                    "database is locked", "database table is locked"):
                raise

    def _merge(self, db, thread, segment, pairs):
        candidates = {}
        for native, mid in pairs:
            candidates[native] = mid if candidates.get(native, mid) == mid else None
        keys = list(candidates)
        for start in range(0, len(keys), 256):
            batch = keys[start:start + 256]
            slots = ",".join("?" for _ in batch)
            rows = {r["native"]: r for r in db.execute(f"""SELECT native,
                json_extract(data,'$.threadId') AS thread, json_extract(data,'$.segmentId') AS segment,
                json_type(data,'$.triggerMessageId') AS form, json_extract(data,'$.triggerMessageId') AS mid,
                json_extract(data,'$.executionDetached') AS detached FROM objects
                WHERE kind='run' AND runtime=? AND native IN ({slots})""", (self.runtime, *batch))}
            updates = []
            for native in batch:
                mid, row = candidates[native], rows.get(native)
                if row is None:
                    key = (thread, segment, native)
                    self.pending[key] = mid if self.pending.get(key, mid) == mid else None
                    if len(self.pending) > 128:
                        self.pending.popitem(last=False)
                elif ((row["thread"], row["segment"]) == (thread, segment) and not row["detached"]
                      and (row["form"] is None or row["form"] == "text" and row["mid"] != mid)):
                    updates.append((mid, self.runtime, native, thread, segment, mid))
            if updates:
                # SQL null marks a missing field; JSON null marks a conflict and cannot be rebound.
                db.executemany("""UPDATE objects SET data=json_set(data,'$.triggerMessageId',
                    CASE WHEN json_type(data,'$.triggerMessageId') IS NULL THEN ? ELSE NULL END)
                    WHERE kind='run' AND runtime=? AND native=? AND json_extract(data,'$.executionDetached') IS NOT 1
                    AND json_extract(data,'$.threadId')=? AND json_extract(data,'$.segmentId')=?
                    AND (json_type(data,'$.triggerMessageId') IS NULL OR
                        (json_type(data,'$.triggerMessageId')='text' AND json_extract(data,'$.triggerMessageId') IS NOT ?))""",
                    updates)

    def register_observed(self, native, data):
        key = (data["threadId"], data["segmentId"], native)
        with self.lock:
            if key in self.pending:
                data["triggerMessageId"] = self.pending.pop(key)
            self.store.object("run", self.runtime, native, data)

    def live(self, payload, pair, watched_thread):
        key, physical = payload.get("sessionKey"), payload.get("sessionId")
        if not valid_id(key) or not valid_id(physical):
            return
        thread = watched_thread
        if not thread:
            # A Manager-owned Run can retain its native subscription after its last tab leaves.
            row = self.store.lookup("run", self.runtime, pair[0])
            if not row:
                return
            thread = row["data"]["threadId"]
            item = self.store.get(thread, kind="thread", runtime=self.runtime)
            if item["data"].get("nativeSessionKey", item["native"]) != key:
                return
        segment = self.store.object_id("segment", self.runtime, canonical([thread, physical]))
        self.remember(thread, segment, [pair])
