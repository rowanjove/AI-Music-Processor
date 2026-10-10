"""Durable SQLite state for the AI music processing pipeline.

The storage layer deliberately contains no audio/tooling code.  It is small enough
to be useful in the desktop app and explicit enough that a failed process can be
resumed without guessing what happened to a stage's files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional
from core.paths import application_data_paths

_project_lock_state = threading.local()


def cache_source_pin(source_path):
    """Persistent original-input protection shared by independent databases."""
    # Alias paths (junctions, symlinks, mapped directory names) must pin the
    # physical file the collector sees, including consumers in another DB.
    source = Path(os.path.abspath(source_path)).resolve()
    for parent in source.parents:
        if parent.name.casefold() == '.work':
            identity = os.path.normcase(str(source)).encode('utf-8')
            return parent.parent / '.cache-source-references' / (hashlib.sha256(identity).hexdigest()+'.pin')
    return None


STAGE_STATES = {"PENDING", "RUNNING", "DONE", "FAILED", "SKIPPED", "CANCELLED"}
JOB_STATES = {"PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED", "PAUSED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: os.PathLike[str] | str, chunk_size: int = 1024 * 1024) -> str:
    """Return a stable SHA-256 for a file without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_path(path: os.PathLike[str] | str) -> str:
    """Hash a file or a directory tree deterministically."""
    target = Path(path)
    if target.is_file():
        return sha256_file(target)
    if not target.is_dir():
        raise FileNotFoundError(str(target))
    digest = hashlib.sha256()
    for item in sorted((p for p in target.rglob("*") if p.is_file()), key=lambda p: p.relative_to(target).as_posix()):
        relative = item.relative_to(target).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


def hash_file(path: os.PathLike[str] | str) -> str:
    """Backwards-friendly alias used by integrations."""
    return sha256_file(path)


def stage_cache_key(stage_name: str, input_hash: Any, parameters: Any) -> str:
    payload = canonical_json({"stage": stage_name, "input": input_hash, "parameters": parameters})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _default_database_path(base_dir=None, environ=None, release_mode=None):
    return str(application_data_paths(base_dir, environ=environ, release_mode=release_mode)["database"])


DEFAULT_DATABASE_PATH = _default_database_path()

class JobBusyError(RuntimeError):
    pass


class JobOwnershipError(RuntimeError):
    pass


class CorruptJobError(ValueError):
    pass


def _job_parameters(value: Any, job_id: str) -> dict:
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError("parameters must be an object")
        return result
    except (TypeError, ValueError) as exc:
        raise CorruptJobError(f"任务 {job_id} 参数快照损坏，拒绝恢复：{exc}") from exc


class JobStore:
    """Thread-safe SQLite repository for jobs, stages, cache and license records."""

    def __init__(self, db_path: os.PathLike[str] | str | None = None) -> None:
        self.db_path = str(Path(db_path or DEFAULT_DATABASE_PATH))
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._owners = threading.local()
        try:
            self._migrate()
            self.source_reference_migration = self._migrate_source_references()
        except Exception:
            self._connection.close()
            raise

    def _migrate_source_references(self):
        """Register existing history inputs in the shared physical-path protocol.

        Opening a known database is the discovery boundary: arbitrary unopened
        databases cannot be found safely. Missing/invalid historical sources stay
        readable, but a live source must acquire the producer lock before its pin
        is published. Busy/permission errors abort opening rather than pretending
        that those inputs are protected. No job snapshot or hash is rewritten.
        """
        with self._lock:
            rows = self._connection.execute('SELECT DISTINCT source_path FROM jobs').fetchall()
        registered = 0
        missing = []
        invalid = []
        for row in rows:
            source = row[0]
            try:
                pin = cache_source_pin(source)
            except (ValueError, TypeError, RuntimeError):
                invalid.append(source)
                continue
            if pin is None:
                continue
            try:
                with self._source_reference(source):
                    registered += 1
            except FileNotFoundError:
                # A disappeared pin directory/lock file is an infrastructure
                # failure, not evidence that an extant input is safely migrated.
                if Path(source).is_file():
                    raise
                missing.append(source)
        return dict(status='COMPLETE_WITH_UNAVAILABLE_SOURCES' if missing or invalid else 'COMPLETE',
                    registered_sources=registered, missing_sources=missing,
                    invalid_sources=invalid, discovery_scope='opened_database')

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_hash TEXT,
            mode TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING',
            current_stage TEXT,
            progress REAL NOT NULL DEFAULT 0,
            project_dir TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS job_stages (
            job_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            input_hash TEXT,
            output_json TEXT NOT NULL DEFAULT '{}',
            output_hashes_json TEXT NOT NULL DEFAULT '{}',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT,
            finished_at TEXT,
            log TEXT,
            error TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, stage_name),
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cache_entries (
            cache_key TEXT PRIMARY KEY,
            stage_name TEXT NOT NULL,
            input_hash TEXT,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            output_hashes_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS output_files (
            job_id TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT,
            kind TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (job_id, path),
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id TEXT PRIMARY KEY,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            name TEXT,
            license_name TEXT,
            source_url TEXT,
            author TEXT,
            note TEXT,
            commercial_status TEXT NOT NULL DEFAULT 'Unknown',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_source_hash ON jobs(source_hash);
        CREATE INDEX IF NOT EXISTS idx_stage_status ON job_stages(status);
        CREATE INDEX IF NOT EXISTS idx_license_subject ON licenses(subject_type, subject_id);
        """
        with self._lock:
            self._connection.executescript(schema)
            self._connection.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in self._connection.execute('PRAGMA table_info(jobs)')}
            for name, kind in [('owner_token', 'TEXT'), ('lease_until', 'REAL'), ('hidden', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    self._connection.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            stage_columns = {r[1] for r in self._connection.execute('PRAGMA table_info(job_stages)')}
            if 'last_used_at' not in stage_columns:
                self._connection.execute('ALTER TABLE job_stages ADD COLUMN last_used_at TEXT')
            # Preserve conflicting historical evidence; ambiguous subjects become Unknown.
            self._connection.execute("UPDATE licenses SET commercial_status='Unknown' WHERE (subject_type,subject_id) IN (SELECT subject_type,subject_id FROM licenses GROUP BY subject_type,subject_id HAVING COUNT(*)>1)")
            self._connection.execute("""CREATE TRIGGER IF NOT EXISTS license_subject_insert_guard
                BEFORE INSERT ON licenses WHEN EXISTS (SELECT 1 FROM licenses WHERE subject_type=NEW.subject_type AND subject_id=NEW.subject_id AND id<>NEW.id)
                BEGIN SELECT RAISE(ABORT,'duplicate license subject'); END""")
            self._connection.commit()

    @contextmanager
    def job_lock(self, job_id: str):
        with self._lock:
            row = self._connection.execute('SELECT project_dir FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row and row['project_dir']:
            with self.project_lock(row['project_dir']): yield
            return
        root = Path(self.db_path).resolve().parent / 'job-locks'
        root.mkdir(parents=True,exist_ok=True)
        with self._file_lock(root/(hashlib.sha256(job_id.encode()).hexdigest()+'.lock')): yield

    @contextmanager
    def project_lock(self, project_dir, *, allow_reentrant=False):
        # A filesystem-scoped lock also protects consumers from other databases.
        root = Path(project_dir).resolve()
        key = os.path.normcase(str(root))
        held = getattr(_project_lock_state, 'held', {})
        if held.get(key, 0):
            if not allow_reentrant:
                raise JobBusyError(f'项目 {root} 正在执行、被引用或清理，不能重复启动')
            held[key] += 1
            try:yield
            finally:held[key] -= 1
            return
        root.mkdir(parents=True,exist_ok=True)
        with self._file_lock(root/'.execution.lock'):
            _project_lock_state.held = held
            held[key] = 1
            try:yield
            finally:held.pop(key, None)

    @contextmanager
    def _source_reference(self, source_path):
        pin = cache_source_pin(source_path)
        if pin is None:
            yield
            return
        # Historical missing sources are read-only evidence. Do not create a
        # producer directory/lock merely because a stale path names .work.
        if not Path(source_path).is_file():
            raise FileNotFoundError(str(source_path))
        with self.project_lock(pin.parent.parent, allow_reentrant=True):
            if not Path(source_path).is_file():
                raise FileNotFoundError(str(source_path))
            pin.parent.mkdir(exist_ok=True)
            info = pin.parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                raise ValueError('缓存源引用目录不能是链接或重解析点')
            # A crash may leave a conservative pin; original inputs are never
            # automatically reclaimed, including after a history row is hidden.
            try:
                with pin.open('xb') as handle:handle.write(b'original-input-v1\n')
            except FileExistsError:
                pass
            yield

    @contextmanager
    def _file_lock(self, path):
        """OS lock survives slow stages; process death releases it immediately."""
        try:
            with open(path, 'xb') as initial:
                initial.write(b'0')
        except FileExistsError:
            pass
        handle = open(path, 'r+b')
        handle.seek(0)
        acquired = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise JobBusyError(f'项目 {path.parent} 正在执行、被引用或清理，不能重复启动') from exc
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def execution(self, job_id: str):
        with self.job_lock(job_id):
            token = uuid.uuid4().hex
            with self.transaction() as conn:
                if not conn.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
                    raise KeyError(job_id)
                conn.execute('UPDATE jobs SET owner_token=?,lease_until=? WHERE id=?', (token,time.time()+60,job_id))
            owners = getattr(self._owners, 'tokens', {})
            self._owners.tokens = {**owners, job_id: token}
            stopped = threading.Event()
            def heartbeat():
                while not stopped.wait(10):
                    try:
                        with self.transaction() as conn:
                            conn.execute('UPDATE jobs SET lease_until=? WHERE id=? AND owner_token=?', (time.time()+60,job_id,token))
                    except sqlite3.Error:
                        # The OS lock still prevents a second runner taking over.
                        pass
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            try:
                yield token
            finally:
                stopped.set(); thread.join(timeout=12)
                with self.transaction() as conn:
                    conn.execute('UPDATE jobs SET owner_token=NULL,lease_until=NULL WHERE id=? AND owner_token=?', (job_id,token))
                self._owners.tokens = owners

    def _assert_owner(self, conn, job_id):
        row = conn.execute('SELECT owner_token FROM jobs WHERE id=?', (job_id,)).fetchone()
        expected = getattr(self._owners, 'tokens', {}).get(job_id)
        actual = row[0] if row else None
        if (actual or expected) and actual != expected:
            raise JobOwnershipError(f'任务 {job_id} 执行所有权已变化，拒绝写入')

    def assert_owner(self, job_id):
        with self._lock:
            self._assert_owner(self._connection, job_id)

    def save_job_parameters(self, job_id, parameters):
        with self.transaction() as conn:
            self._assert_owner(conn, job_id)
            conn.execute('UPDATE jobs SET parameters_json=?,updated_at=? WHERE id=?', (canonical_json(parameters),utc_now(),job_id))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self._connection.in_transaction
            savepoint = 'nested_' + uuid.uuid4().hex
            try:
                self._connection.execute('SAVEPOINT '+savepoint if nested else 'BEGIN IMMEDIATE')
                yield self._connection
                if nested:self._connection.execute('RELEASE SAVEPOINT '+savepoint)
                else:self._connection.commit()
            except Exception:
                if nested:
                    self._connection.execute('ROLLBACK TO SAVEPOINT '+savepoint)
                    self._connection.execute('RELEASE SAVEPOINT '+savepoint)
                else:self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def create_job(
        self,
        source_path: str,
        mode: str,
        parameters: Optional[Mapping[str, Any]] = None,
        source_hash: Optional[str] = None,
        project_dir: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> str:
        job_id = job_id or f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        now = utc_now()
        with self._source_reference(source_path), self.transaction() as conn:
            # A .work input may have been reclaimed before this transaction.
            # GC holds the same writer lock across its fresh reference check
            # and deletion, so never publish a dangling cache-source reference.
            source = Path(os.path.abspath(source_path))
            if any(part.casefold()=='.work' for part in source.parts) and not source.is_file():
                raise FileNotFoundError(str(source))
            conn.execute(
                "INSERT INTO jobs (id,source_path,source_hash,mode,parameters_json,project_dir,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, os.path.abspath(source_path), source_hash, mode, canonical_json(parameters or {}), project_dir, now, now),
            )
        return job_id

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["parameters"] = _job_parameters(item.pop("parameters_json"), item['id'])
        return item

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"source_hash", "status", "current_stage", "progress", "project_dir", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        columns = ", ".join(f"{key} = ?" for key in updates)
        with self.transaction() as conn:
            self._assert_owner(conn, job_id)
            conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*updates.values(), job_id))

    def set_history_hidden(self, job_id, hidden=True):
        if type(hidden) is not bool:raise ValueError('hidden 必须是布尔值')
        with self.job_lock(job_id),self.transaction() as conn:
            row=conn.execute('SELECT owner_token,status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:raise KeyError(job_id)
            if row['owner_token'] or row['status']=='RUNNING':raise JobBusyError('运行中的任务不能移出历史')
            conn.execute('UPDATE jobs SET hidden=?,updated_at=? WHERE id=?',(int(hidden),utc_now(),job_id))

    def list_jobs(self, limit: int = 100, offset: int = 0, status: Optional[str] = None, include_hidden=True) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        query = "SELECT * FROM jobs"
        args: list[Any] = []
        filters=[]
        if not include_hidden:filters.append('hidden=0')
        if status:
            filters.append('status = ?')
            args.append(status)
        if filters:query += ' WHERE '+' AND '.join(filters)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args.extend((limit, offset))
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        items = []
        for row in rows:
            try:
                items.append(self._decode_job(row))
            except CorruptJobError as exc:
                item = dict(row); item.pop('parameters_json', None)
                item.update(parameters={}, snapshot_error=str(exc))
                items.append(item)
        return items

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["parameters"] = _job_parameters(item.pop("parameters_json"), item['id'])
        return item

    def upsert_stage(
        self,
        job_id: str,
        stage_name: str,
        *,
        status: str = "PENDING",
        input_hash: Optional[str] = None,
        outputs: Optional[Mapping[str, Any]] = None,
        output_hashes: Optional[Mapping[str, str]] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        log: Optional[str] = None,
        error: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> None:
        if status not in STAGE_STATES:
            raise ValueError(f"unknown stage status: {status}")
        fields = {
            "status": status,
            "input_hash": input_hash,
            "output_json": canonical_json(outputs or {}),
            "output_hashes_json": canonical_json(output_hashes or {}),
            "parameters_json": canonical_json(parameters or {}),
            "started_at": started_at,
            "finished_at": finished_at,
            "log": log,
            "error": error,
            "attempt": int(attempt or 0),
        }
        with self.transaction() as conn:
            self._assert_owner(conn, job_id)
            if attempt is None:
                prior=conn.execute('SELECT attempt FROM job_stages WHERE job_id=? AND stage_name=?',(job_id,stage_name)).fetchone()
                fields['attempt']=prior['attempt'] if prior is not None else 0
            conn.execute(
                """INSERT INTO job_stages
                (job_id,stage_name,status,input_hash,output_json,output_hashes_json,parameters_json,started_at,finished_at,log,error,attempt)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,stage_name) DO UPDATE SET
                status=excluded.status,input_hash=excluded.input_hash,output_json=excluded.output_json,
                output_hashes_json=excluded.output_hashes_json,parameters_json=excluded.parameters_json,
                started_at=excluded.started_at,finished_at=excluded.finished_at,log=excluded.log,
                error=excluded.error,attempt=excluded.attempt""",
                (job_id, stage_name, *fields.values()),
            )
            if status == 'DONE':
                conn.execute('UPDATE job_stages SET last_used_at=? WHERE job_id=? AND stage_name=?',(utc_now(),job_id,stage_name))

    def touch_stage(self, job_id, stage_name):
        with self.transaction() as conn:
            self._assert_owner(conn,job_id)
            conn.execute('UPDATE job_stages SET last_used_at=? WHERE job_id=? AND stage_name=?',(utc_now(),job_id,stage_name))

    def get_stage(self, job_id: str, stage_name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM job_stages WHERE job_id=? AND stage_name=?", (job_id, stage_name)).fetchone()
        return self._decode_stage(row) if row else None

    def list_stages(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM job_stages WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall()
        return [self._decode_stage(row) for row in rows]

    @staticmethod
    def _decode_stage(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["outputs"] = _json_load(item.pop("output_json"), {})
        item["output_hashes"] = _json_load(item.pop("output_hashes_json"), {})
        item["parameters"] = _json_load(item.pop("parameters_json"), {})
        return item

    def put_cache(
        self,
        cache_key: str,
        stage_name: str,
        input_hash: str,
        parameters: Mapping[str, Any],
        outputs: Mapping[str, Any],
        output_hashes: Mapping[str, str],
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO cache_entries
                (cache_key,stage_name,input_hash,parameters_json,output_json,output_hashes_json,created_at,last_used_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET output_json=excluded.output_json,
                output_hashes_json=excluded.output_hashes_json,last_used_at=excluded.last_used_at""",
                (cache_key, stage_name, input_hash, canonical_json(parameters), canonical_json(outputs), canonical_json(output_hashes), now, now),
            )

    def get_cache(self, cache_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            conn = self._connection
            row = conn.execute("SELECT * FROM cache_entries WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["parameters"] = _json_load(item.pop("parameters_json"), {})
        item["outputs"] = _json_load(item.pop("output_json"), {})
        item["output_hashes"] = _json_load(item.pop("output_hashes_json"), {})
        return item

    def touch_cache(self, cache_key):
        with self.transaction() as conn:
            conn.execute('UPDATE cache_entries SET last_used_at=? WHERE cache_key=?', (utc_now(),cache_key))

    def list_cache(self):
        with self._lock:
            rows = self._connection.execute('SELECT * FROM cache_entries ORDER BY last_used_at').fetchall()
        return [dict(row, outputs=_json_load(row['output_json'], {})) for row in rows]

    def project_job_id(self, project_dir):
        with self._lock:
            rows = self._connection.execute('SELECT id,project_dir FROM jobs').fetchall()
        target = os.path.normcase(os.path.abspath(project_dir))
        return next((r['id'] for r in rows if r['project_dir'] and os.path.normcase(os.path.abspath(r['project_dir'])) == target), None)

    def invalidate_paths(self, paths):
        targets = {os.path.normcase(os.path.abspath(p)) for p in paths}
        with self.transaction() as conn:
            for table, key in [('cache_entries', 'cache_key'), ('job_stages', None)]:
                rows = conn.execute(f'SELECT * FROM {table}').fetchall()
                for row in rows:
                    # Match actual registered output paths, including nested stage objects.
                    def contains(value):
                        if isinstance(value, dict): return any(contains(v) for v in value.values())
                        if isinstance(value, list): return any(contains(v) for v in value)
                        return isinstance(value, str) and os.path.normcase(os.path.abspath(value)) in targets
                    if contains(_json_load(row['output_json'], {})):
                        if key:
                            conn.execute('DELETE FROM cache_entries WHERE cache_key=?', (row[key],))
                        else:
                            conn.execute("UPDATE job_stages SET status='PENDING',output_hashes_json='{}',log='cache GC; rebuild required' WHERE job_id=? AND stage_name=?", (row['job_id'],row['stage_name']))

    def register_output(self, job_id: str, path: os.PathLike[str] | str, kind: Optional[str] = None, file_hash: Optional[str] = None) -> None:
        target = os.path.abspath(os.fspath(path))
        digest = file_hash
        if digest is None and os.path.isfile(target):
            digest = sha256_file(target)
        with self.transaction() as conn:
            self._assert_owner(conn, job_id)
            conn.execute(
                """INSERT INTO output_files(job_id,path,sha256,kind,created_at) VALUES(?,?,?,?,?)
                ON CONFLICT(job_id,path) DO UPDATE SET sha256=excluded.sha256,kind=excluded.kind""",
                (job_id, target, digest, kind, utc_now()),
            )

    def list_outputs(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM output_files WHERE job_id=? ORDER BY path", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def put_license(
        self,
        subject_type: str,
        subject_id: str,
        *,
        name: Optional[str] = None,
        license_name: Optional[str] = None,
        source_url: Optional[str] = None,
        author: Optional[str] = None,
        note: Optional[str] = None,
        commercial_status: str = "Unknown",
        metadata: Optional[Mapping[str, Any]] = None,
        license_id: Optional[str] = None,
    ) -> str:
        if commercial_status not in {"Ready", "Warning", "Unknown"}:
            raise ValueError("commercial_status must be Ready, Warning or Unknown")
        if commercial_status == 'Ready' and not all(str(v or '').strip() for v in (name,license_name,source_url,author,note)):
            commercial_status = 'Unknown'
        license_id = license_id or 'license_' + hashlib.sha256(canonical_json([subject_type,subject_id]).encode()).hexdigest()
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute('SELECT * FROM licenses WHERE subject_type=? AND subject_id=?', (subject_type,subject_id)).fetchall()
            if len(existing) > 1:
                raise ValueError('授权主体存在冲突记录，请先整理证据；不能直接覆盖')
            if existing:
                license_id = existing[0]['id']
            collision = conn.execute('SELECT subject_type,subject_id FROM licenses WHERE id=?', (license_id,)).fetchone()
            if collision and tuple(collision) != (subject_type,subject_id):
                raise ValueError('授权ID已属于另一主体')
            conn.execute(
                """INSERT INTO licenses
                (id,subject_type,subject_id,name,license_name,source_url,author,note,commercial_status,metadata_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,license_name=excluded.license_name,
                source_url=excluded.source_url,author=excluded.author,note=excluded.note,
                commercial_status=excluded.commercial_status,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (license_id, subject_type, subject_id, name, license_name, source_url, author, note, commercial_status, canonical_json(metadata or {}), now, now),
            )
        return license_id

    def list_licenses(self, subject_type: Optional[str] = None, subject_id: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM licenses"
        args: list[Any] = []
        conditions = []
        if subject_type is not None:
            conditions.append("subject_type=?")
            args.append(subject_type)
        if subject_id is not None:
            conditions.append("subject_id=?")
            args.append(subject_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY subject_type, subject_id"
        with self._lock:
            rows = self._connection.execute(query, args).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["metadata"] = _json_load(item.pop("metadata_json"), {})
            if item['commercial_status'] == 'Ready' and not all(str(item.get(k) or '').strip() for k in ('name','license_name','source_url','author','note')):
                item['commercial_status'] = 'Unknown'
            records.append(item)
        counts = {}
        for item in records:
            subject = (item['subject_type'],item['subject_id'])
            counts[subject] = counts.get(subject,0)+1
        for item in records:
            if counts[(item['subject_type'],item['subject_id'])] > 1:
                item['commercial_status'] = 'Unknown'
                item['conflict'] = True
        return records


Storage = JobStore
PipelineStorage = JobStore
