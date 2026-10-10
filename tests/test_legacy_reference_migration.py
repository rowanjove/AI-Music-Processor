"""Opened legacy databases register original cache inputs across DB boundaries."""
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.cache_manager import CacheManager
from core.storage import JobBusyError, JobStore, cache_source_pin


class LegacyReferenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='旧源引用 ')
        self.root = Path(self.tmp.name)
        self.output = self.root / 'output'
        self.project = self.output / 'producer'
        (self.project / '.work').mkdir(parents=True)
        self.source = self.project / '.work' / 'legacy.wav'
        self.source.write_bytes(b'original legacy audio')
        self.original = self.root / 'input.wav'
        self.original.write_bytes(b'producer input')
        self.producer = JobStore(self.root / 'producer.db')
        job = self.producer.create_job(str(self.original), 'polish', project_dir=str(self.project))
        self.producer.update_job(job, status='DONE')
        self.legacy_path = self.root / 'legacy.db'

    def tearDown(self):
        self.producer.close()
        self.tmp.cleanup()

    def old_database(self, sources=None):
        # Direct old SQL, deliberately bypassing create_job and its pin publisher.
        with sqlite3.connect(str(self.legacy_path)) as conn:
            conn.execute('''CREATE TABLE jobs (
                id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_hash TEXT,
                mode TEXT NOT NULL, parameters_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'PENDING', current_stage TEXT,
                progress REAL NOT NULL DEFAULT 0, project_dir TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT)''')
            for i, source in enumerate(sources or [str(self.source)]):
                conn.execute('INSERT INTO jobs(id,source_path,source_hash,mode,parameters_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                             (str(i), source, 'unchanged-source-hash', 'polish', '{broken legacy snapshot', 'old', 'old'))

    def test_open_legacy_registers_persistent_pin_and_preserves_raw_history(self):
        self.old_database()
        pin = cache_source_pin(self.source)
        self.assertFalse(pin.exists())
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['registered_sources'], 1)
            self.assertEqual(legacy.source_reference_migration['status'], 'COMPLETE')
            self.assertIn('snapshot_error', legacy.list_jobs()[0])
        self.assertTrue(pin.is_file())
        result = CacheManager(self.producer, self.output).collect(ttl_days=0)
        self.assertEqual(result['reclaimed_bytes'], 0)
        self.assertEqual(self.source.read_bytes(), b'original legacy audio')
        with sqlite3.connect(str(self.legacy_path)) as conn:
            row = conn.execute('SELECT source_path,source_hash,parameters_json,created_at,updated_at FROM jobs').fetchone()
        self.assertEqual(row, (str(self.source), 'unchanged-source-hash', '{broken legacy snapshot', 'old', 'old'))

    def test_busy_producer_aborts_open_then_retry_registers_before_collection(self):
        self.old_database()
        held = threading.Event()
        release = threading.Event()
        errors = []
        def hold():
            try:
                with self.producer.project_lock(self.project):
                    held.set()
                    release.wait(10)
            except Exception as exc:
                errors.append(exc)
                held.set()
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            self.assertFalse(errors)
            with self.assertRaises(JobBusyError):
                JobStore(self.legacy_path)
            self.assertFalse(cache_source_pin(self.source).exists())
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['registered_sources'], 1)
        CacheManager(self.producer, self.output).collect(ttl_days=0)
        self.assertTrue(self.source.exists())

    def test_permission_failure_is_not_reported_as_success(self):
        self.old_database()
        real_open = Path.open
        def denied(path, *args, **kwargs):
            if path.suffix == '.pin':
                raise PermissionError('pin directory denied')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            with self.assertRaises(PermissionError):
                JobStore(self.legacy_path)
        self.assertFalse(cache_source_pin(self.source).exists())
        # Failed initialization closes its handle, so the ordinary retry works.
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['registered_sources'], 1)

    def test_missing_and_invalid_history_remains_readable_with_explicit_status(self):
        missing = self.project / '.work' / 'already-missing.wav'
        self.old_database([str(missing), 'invalid\0path'])
        with JobStore(self.legacy_path) as legacy:
            receipt = legacy.source_reference_migration
            self.assertEqual(receipt['status'], 'COMPLETE_WITH_UNAVAILABLE_SOURCES')
            self.assertEqual(receipt['missing_sources'], [str(missing)])
            self.assertEqual(receipt['invalid_sources'], ['invalid\0path'])
            self.assertEqual(len(legacy.list_jobs()), 2)
            self.assertEqual(receipt['discovery_scope'], 'opened_database')

    def test_missing_historical_producer_path_does_not_create_directories(self):
        nonexistent=self.root/'never-created-producer'
        missing=nonexistent/'.work'/'old-input.wav'
        self.old_database([str(missing)])
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['missing_sources'],[str(missing)])
        self.assertFalse(nonexistent.exists())

    def test_source_permission_failure_aborts_open_and_can_retry(self):
        self.old_database()
        real_stat = Path.stat
        def denied(path, *args, **kwargs):
            if path == self.source:
                raise PermissionError('source inspection denied')
            return real_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', denied):
            with self.assertRaises(PermissionError):
                JobStore(self.legacy_path)
        self.assertFalse(cache_source_pin(self.source).exists())
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['registered_sources'], 1)

    def test_alias_history_pins_physical_source(self):
        alias = self.root / 'project alias'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(self.project)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            alias.symlink_to(self.project, target_is_directory=True)
        alias_source = alias / '.work' / 'legacy.wav'
        self.old_database([str(alias_source)])
        with JobStore(self.legacy_path) as legacy:
            self.assertEqual(legacy.source_reference_migration['registered_sources'], 1)
        self.assertEqual(cache_source_pin(alias_source), cache_source_pin(self.source))
        CacheManager(self.producer, self.output).collect(ttl_days=0)
        self.assertEqual(self.source.read_bytes(), b'original legacy audio')

    def test_unopened_unknown_database_is_not_claimed_discovered(self):
        self.old_database()
        # An unopened foreign DB is outside the registration boundary. Do not
        # silently pretend the producer has inspected arbitrary private DBs.
        self.assertFalse(cache_source_pin(self.source).exists())
        with JobStore(self.root / 'other-empty.db') as other:
            self.assertEqual(other.source_reference_migration['registered_sources'], 0)
            self.assertEqual(other.source_reference_migration['discovery_scope'], 'opened_database')
        self.assertFalse(cache_source_pin(self.source).exists())


if __name__ == '__main__':
    unittest.main()
