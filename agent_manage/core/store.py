"""Private local metadata. Native transcripts remain the source of message truth."""
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import APIError


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now_ms():
    return int(time.time() * 1000)


class Store:
    def __init__(self, directory):
        root = Path(directory).absolute()
        if any(p.is_symlink() for p in [root, *root.parents]):
            raise ValueError("Manager state must not traverse symlinks")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise ValueError("Manager state must be owned by the service user with mode 0700")
        self.path = root / "manager.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise ValueError("Manager database must be a private regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported manager state schema; use the matching Manager version")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, runtime TEXT NOT NULL,
                    native TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(kind,runtime,native));
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_key TEXT NOT NULL,
                    digest TEXT NOT NULL, runtime TEXT NOT NULL, method TEXT NOT NULL,
                    status TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    result TEXT, error TEXT, UNIQUE(scope,request_key));
                CREATE TABLE IF NOT EXISTS tickets (
                    hash TEXT PRIMARY KEY, expires INTEGER NOT NULL, context TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases (
                    hash TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL,
                    context TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS operations_runtime_created ON operations(runtime, created);
                CREATE INDEX IF NOT EXISTS runs_by_thread ON objects(runtime, json_extract(data, '$.threadId'))
                    WHERE kind='run';
                CREATE INDEX IF NOT EXISTS runs_by_request ON objects(runtime, json_extract(data, '$.threadId'),
                    json_extract(data, '$.clientRequestId')) WHERE kind='run';
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                db.execute("ALTER TABLE operations ADD COLUMN agent TEXT")
                db.execute("ALTER TABLE operations ADD COLUMN thread TEXT")
                db.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def object_id(kind, runtime, native):
        return kind + "_" + hashlib.sha256(canonical([kind, runtime, native]).encode()).hexdigest()[:32]

    def put_objects(self, runtime, items):
        rows = [(self.object_id(kind, runtime, native), kind, runtime, native, canonical(data))
                for kind, native, data in items]
        if not rows:
            return []
        with self.connect() as db:
            db.executemany("INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data "
                           "WHERE objects.data <> excluded.data", rows)
        return [row[0] for row in rows]

    def object(self, kind, runtime, native, data=None):
        return self.put_objects(runtime, [(kind, native, data or {})])[0]

    def thread_runs(self, runtime, thread, *, request_key=None):
        query = "SELECT native,data FROM objects WHERE kind='run' AND runtime=? AND json_extract(data,'$.threadId')=?"
        args = [runtime, thread]
        if request_key is not None:
            query += " AND json_extract(data,'$.clientRequestId')=?"
            args.append(request_key)
        else:
            query += (" AND json_extract(data,'$.status') IN ('accepted','running','cancel_requested')"
                      " AND json_extract(data,'$.executionDetached') IS NOT 1 LIMIT 129")
        with self.connect() as db:
            return [(row[0], json.loads(row[1])) for row in db.execute(query, args)]

    def lose_run_observation(self, runtime):
        with self.connect() as db:
            db.execute("""UPDATE objects SET data=json_set(data,'$.observation','unknown')
                WHERE kind='run' AND runtime=? AND json_extract(data,'$.status') IN ('accepted','running','cancel_requested')
                AND json_extract(data,'$.observation') IS NOT 'unknown'""", (runtime,))

    def bind_ledger(self, runtime, generation):
        identifier = self.object_id("host_generation", runtime, runtime)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM objects WHERE id=?", (identifier,)).fetchone()
            if row and json.loads(row[0]) != generation:
                db.execute("""UPDATE objects SET data=json_set(data, '$.observation','unknown',
                    '$.executionDetached',json('true')) WHERE kind='run' AND runtime=?
                    AND json_extract(data,'$.status') IN ('accepted','running','cancel_requested')""", (runtime,))
            db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?)",
                       (identifier, "host_generation", runtime, runtime, canonical(generation)))

    def transition_run(self, runtime, native, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,data FROM objects WHERE kind='run' AND runtime=? AND native=?",
                             (runtime, native)).fetchone()
            if row is None:
                raise APIError("NOT_FOUND", "Unknown run")
            data = json.loads(row["data"])
            if data.get("executionDetached") or data.get("status") in ("completed", "failed", "cancelled"):
                return data
            if data.get("status") == "cancel_requested" and changes.get("status") == "running":
                changes.pop("status")
            if any(data.get(key) != value for key, value in changes.items()):
                data.update(changes)
                db.execute("UPDATE objects SET data=? WHERE id=?", (canonical(data), row["id"]))
            return data

    def get(self, identifier, *, kind=None, runtime=None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (identifier,)).fetchone()
        if row is None or kind is not None and row["kind"] != kind or runtime is not None and row["runtime"] != runtime:
            raise APIError("NOT_FOUND", "Object is not present in this runtime")
        return {**dict(row), "data": json.loads(row["data"])}

    def objects(self, kind, runtime):
        with self.connect() as db:
            return [{**dict(row), "data": json.loads(row["data"])} for row in
                    db.execute("SELECT * FROM objects WHERE kind=? AND runtime=? ORDER BY id", (kind, runtime))]

    def thread_run_activity(self, runtime, thread_ids):
        ids = list(dict.fromkeys(thread_ids))
        activity = {}
        if not ids:
            return activity
        with self.connect() as db:
            for start in range(0, len(ids), 128):
                batch = ids[start:start + 128]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(f"""
                    WITH ranked AS (
                        SELECT id, data, json_extract(data, '$.threadId') AS thread,
                            json_extract(data, '$.status') IN ('accepted','running','cancel_requested') AS active,
                            row_number() OVER (
                                PARTITION BY json_extract(data, '$.threadId'),
                                    json_extract(data, '$.status') IN ('accepted','running','cancel_requested')
                                ORDER BY coalesce(json_extract(data, '$.completedAt'), json_extract(data, '$.createdAt')) DESC, id
                            ) AS position
                        FROM objects
                        WHERE kind='run' AND runtime=? AND json_extract(data, '$.threadId') IN ({placeholders})
                          AND json_extract(data,'$.executionDetached') IS NOT 1
                    )
                    SELECT * FROM ranked WHERE (active AND position<=8) OR (NOT active AND position=1)
                    ORDER BY thread, active DESC, position
                """, (runtime, *batch))
                for row in rows:
                    data = json.loads(row["data"])
                    target = activity.setdefault(row["thread"], {"activeRuns": [], "lastRun": None})
                    dto = {"runId": row["id"], **{key: data[key] for key in
                           ("status", "observation", "resultSync", "source", "startedAt", "createdAt", "completedAt", "resultBoundary", "error") if key in data}}
                    if row["active"]:
                        target["activeRuns"].append(dto)
                    else:
                        target["lastRun"] = dto
        return activity

    def lookup(self, kind, runtime, native):
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE kind=? AND runtime=? AND native=?", (kind, runtime, native)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def claim(self, principal, runtime, method, key, params, *, agent_id=None, thread_id=None):
        scope = canonical([principal["issuer"], principal["subject"], principal.get("tenantId"), runtime, method])
        digest = hashlib.sha256(canonical(params).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE scope=? AND request_key=?", (scope, key)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise APIError("IDEMPOTENCY_CONFLICT", "clientRequestId was already used with different parameters")
                return self.operation_row(row), False
            identifier = "op_" + uuid.uuid4().hex
            stamp = now_ms()
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, scope, key, digest, runtime, method, "accepted", stamp, stamp, None, None, agent_id, thread_id))
            row = db.execute("SELECT * FROM operations WHERE id=?", (identifier,)).fetchone()
        return self.operation_row(row), True

    @staticmethod
    def operation_row(row):
        return {"operationId": row["id"], "runtimeId": row["runtime"], "method": row["method"],
                "agentId": row["agent"], "threadId": row["thread"],
                "clientRequestId": row["request_key"], "status": row["status"],
                "observation": "unknown" if row["status"] == "unknown" else "known",
                "createdAt": row["created"], "updatedAt": row["updated"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None}

    def operation(self, identifier, principal, runtime):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=? AND runtime=?", (identifier, runtime)).fetchone()
        if not row or json.loads(row["scope"])[:3] != [principal["issuer"], principal["subject"], principal.get("tenantId")]:
            raise APIError("NOT_FOUND", "Operation not found")
        return self.operation_row(row)

    def operations(self, principal, runtime, key=None, *, agent_ids=None, thread_id=None, method=None):
        owner_prefix = canonical([principal["issuer"], principal["subject"], principal.get("tenantId")])[:-1] + ","
        if method and key is not None:
            query = "SELECT * FROM operations WHERE scope=? AND runtime=?"
            parameters = [canonical([principal["issuer"], principal["subject"], principal.get("tenantId"), runtime, method]), runtime]
        else:
            query = "SELECT * FROM operations WHERE runtime=? AND substr(scope,1,?)=?"
            parameters = [runtime, len(owner_prefix), owner_prefix]
        if key is not None:
            query += " AND request_key=?"
            parameters.append(key)
        if agent_ids is not None and "*" not in agent_ids:
            if not agent_ids:
                return []
            query += " AND agent IN (" + ",".join("?" for _ in agent_ids) + ")"
            parameters.extend(agent_ids)
        for field, value in (("thread", thread_id), ("method", method)):
            if value is not None:
                query += " AND " + field + "=?"
                parameters.append(value)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY created DESC, id DESC LIMIT 200", parameters).fetchall()
        return [self.operation_row(row) for row in rows]

    def checkpoint_thread(self, identifier, thread, result):
        with self.connect() as db:
            db.execute("UPDATE operations SET thread=?, result=?, updated=? WHERE id=? AND status='running'",
                       (thread, canonical(result), now_ms(), identifier))

    def finish(self, identifier, status, *, result=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE operations SET status=?, updated=?, result=coalesce(?,result), error=? WHERE id=?",
                       (status, now_ms(), canonical(result) if result is not None else None,
                        canonical(error) if error is not None else None, identifier))

    def recover_operations(self):
        # Never repeat a write whose native outcome is uncertain.
        with self.connect() as db:
            db.execute("UPDATE operations SET status='unknown', updated=? WHERE status IN ('accepted','running')", (now_ms(),))
        self.prune_credentials()

    def prune_credentials(self):
        with self.connect() as db:
            db.execute("DELETE FROM tickets WHERE expires<?", (now_ms(),))
            db.execute("DELETE FROM leases WHERE expires<?", (now_ms() - 3600000,))
