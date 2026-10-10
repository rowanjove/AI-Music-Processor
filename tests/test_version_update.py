"""Fixture gate tests, not actual Portable runtime acceptance."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import version_update as updates
from core.paths import PORTABLE_RELEASE_CONTENT


def _bundle(root, version):
    root.mkdir()
    files = {'VERSION': version.encode(), 'AI-Music-Processor.exe': b'MZ fixture '+version.encode(),
             'PORTABLE_RELEASE': PORTABLE_RELEASE_CONTENT.encode('ascii')}
    records = []
    for name, content in sorted(files.items()):
        (root/name).write_bytes(content)
        records.append({'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    manifest = {'schema_version': 1, 'files': records, 'file_count': len(records),
                'file_inventory_sha256': hashlib.sha256(json.dumps(
                    records, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    (root/'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')


def _fixture_gate(**kwargs):
    assert kwargs['fast'] is False
    assert not str(kwargs['data_root']).startswith(str(kwargs['root'])+os.sep)
    kwargs['progress']('fixture full gate')
    return {'status': 'PASS_WITH_WARNINGS', 'root': str(kwargs['root'])}


class VersionUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old, self.source = self.base/'旧版 空格', self.base/'新版本来源'
        self.target, self.config = self.base/'installed'/'新版本', self.base/'userdata'/'config'
        _bundle(self.old, 'v1')
        _bundle(self.source, 'v2')
        self.config.mkdir(parents=True)
        (self.config/'app.json').write_bytes(b'user settings must stay')
        (self.config.parent/'jobs.sqlite').write_bytes(b'user jobs must stay')

    def _update(self, **kwargs):
        kwargs.setdefault('verifier', _fixture_gate)
        return updates.stage_update_bundle(self.old, self.source, self.target, self.config, **kwargs)

    def _selection(self):
        return self.config/updates.SELECTION_NAME

    def _failed(self):
        receipts = [path for path in self.config.glob('version-*.json')
                    if path.name != updates.SELECTION_NAME]
        failures = [json.loads(path.read_text(encoding='utf-8')) for path in receipts
                    if json.loads(path.read_text(encoding='utf-8')).get('status') == 'FAILED']
        self.assertTrue(failures)
        return failures[-1]

    def test_update_and_rollback_preserve_versions_and_user_data(self):
        events = []
        result = self._update(progress=events.append)
        self.assertEqual(result['launcher_path'], str(self.target/'AI-Music-Processor.exe'))
        self.assertEqual(result['verification_status'], 'PASS_WITH_WARNINGS')
        self.assertFalse(result['launched'])
        self.assertEqual(result['verification_kind'], 'INJECTED_VERIFIER_NOT_RELEASE_PROOF')
        selection = json.loads(self._selection().read_text(encoding='utf-8'))
        self.assertFalse(selection['full_verification'])
        self.assertEqual(selection['previous']['root'], str(self.old))
        self.assertEqual((self.target/'VERSION').read_bytes(), b'v2')
        self.assertEqual((self.old/'VERSION').read_bytes(), b'v1')
        rollback = updates.rollback_previous_version(self.config, verifier=_fixture_gate)
        self.assertEqual(rollback['launcher_path'], str(self.old/'AI-Music-Processor.exe'))
        self.assertEqual(json.loads(self._selection().read_text(encoding='utf-8'))['previous']['root'], str(self.target))
        self.assertEqual((self.config/'app.json').read_bytes(), b'user settings must stay')
        self.assertEqual((self.config.parent/'jobs.sqlite').read_bytes(), b'user jobs must stay')
        self.assertIn('fixture full gate', events)

    def test_default_uses_current_trusted_verifier_with_full_flag(self):
        with mock.patch('core.runtime_verify.verify_runtime', side_effect=_fixture_gate) as gate:
            result = self._update(verifier=None)
        self.assertEqual(gate.call_args.kwargs['root'], self.target)
        self.assertIs(gate.call_args.kwargs['fast'], False)
        self.assertEqual(result['verification_kind'], 'CURRENT_TRUSTED_FULL_RUNTIME_VERIFIER')
        # This is a mock call-chain assertion, not actual full-gate evidence.

    def test_full_gate_failure_keeps_old_selection_bytes_and_receipt(self):
        self._update()
        before = self._selection().read_bytes()
        def fail(**kwargs):
            return {'status': 'FAIL', 'root': str(kwargs['root'])}
        with self.assertRaises(updates.VersionUpdateError):
            updates.rollback_previous_version(self.config, verifier=fail)
        self.assertEqual(self._selection().read_bytes(), before)
        self.assertEqual(self._failed()['verification']['status'], 'FAIL')

    def test_wrong_root_or_status_never_selects(self):
        for result in ({'status': 'PASS', 'root': str(self.old)}, {'status': 'READY', 'root': str(self.target)}, None):
            with self.subTest(result=result):
                target = self.base/('trial-'+str(len(list(self.base.glob('trial-*')))))
                with self.assertRaises(updates.VersionUpdateError):
                    updates.stage_update_bundle(self.old, self.source, target, self.config,
                                                verifier=lambda **kw: result)
                self.assertFalse(self._selection().exists())
        self._failed()

    def test_cancel_during_copy_keeps_selection_absent_and_cleans_stage(self):
        cancel = threading.Event()
        def progress(event):
            if isinstance(event, dict) and event.get('event') == 'file':
                cancel.set()
        with self.assertRaises(updates.VersionUpdateError):
            self._update(cancel=cancel, progress=progress)
        self.assertFalse(self._selection().exists())
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.target.parent.glob('*.installing')))
        self._failed()

    def test_cancel_after_gate_does_not_publish_selection(self):
        cancel = threading.Event()
        def gate(**kwargs):
            result = _fixture_gate(**kwargs)
            cancel.set()
            return result
        with self.assertRaises(updates.VersionUpdateError):
            self._update(verifier=gate, cancel=cancel)
        self.assertFalse(self._selection().exists())
        self.assertTrue(self.target.exists())  # Separate verified copy may remain.
        self._failed()

    def test_cancel_after_ready_receipt_does_not_commit(self):
        cancel = threading.Event()
        original = updates._atomic_json
        def write(path, value):
            original(path, value)
            if value.get('status') == 'VERIFIED_READY':
                cancel.set()
        with mock.patch.object(updates, '_atomic_json', side_effect=write):
            with self.assertRaises(updates.VersionUpdateError):
                self._update(cancel=cancel)
        self.assertFalse(self._selection().exists())
        self._failed()

    def test_tampered_previous_and_selected_block_rollback(self):
        self._update()
        before = self._selection().read_bytes()
        for root, content in ((self.old, b'v1'), (self.target, b'v2')):
            with self.subTest(root=str(root)):
                (root/'VERSION').write_bytes(b'bad')
                with self.assertRaises(updates.VersionUpdateError):
                    updates.rollback_previous_version(self.config, verifier=_fixture_gate)
                self.assertEqual(self._selection().read_bytes(), before)
                (root/'VERSION').write_bytes(content)

    def test_identity_change_inside_gate_blocks_selection(self):
        def gate(**kwargs):
            (kwargs['root']/'VERSION').write_bytes(b'changed')
            return _fixture_gate(**kwargs)
        with self.assertRaises(updates.VersionUpdateError):
            self._update(verifier=gate)
        self.assertFalse(self._selection().exists())
        self._failed()

    def test_corrupt_selection_retained_and_failed_receipt_written(self):
        self._selection().write_bytes(b'{broken')
        with self.assertRaises(updates.VersionUpdateError):
            self._update()
        self.assertEqual(self._selection().read_bytes(), b'{broken')
        self._failed()

    def test_concurrent_selection_change_is_never_overwritten(self):
        outsider = b'changed by another writer'
        def gate(**kwargs):
            self._selection().write_bytes(outsider)
            return _fixture_gate(**kwargs)
        with self.assertRaises(updates.VersionUpdateError):
            self._update(verifier=gate)
        self.assertEqual(self._selection().read_bytes(), outsider)
        self._failed()

    def test_rejected_paths_busy_missing_previous_and_existing_target(self):
        with self.assertRaises(updates.VersionUpdateError):
            self._update(in_use=True)
        self._failed()
        with self.assertRaises(updates.VersionUpdateError):
            updates.rollback_previous_version(self.config, verifier=_fixture_gate)
        with self.assertRaises(updates.VersionUpdateError):
            updates.stage_update_bundle(self.old, self.source, self.old/'nested', self.config, verifier=_fixture_gate)
        with self.assertRaises(updates.VersionUpdateError):
            updates.stage_update_bundle(self.old, self.source, self.target, self.source/'config', verifier=_fixture_gate)
        self.target.mkdir(parents=True)
        (self.target/'keep.txt').write_bytes(b'keep')
        with self.assertRaises(updates.VersionUpdateError):
            self._update()
        self.assertEqual((self.target/'keep.txt').read_bytes(), b'keep')

    def test_source_checksum_invalid_or_unlisted_user_data_rejected(self):
        (self.source/'VERSION').write_bytes(b'tampered')
        with self.assertRaises(updates.VersionUpdateError):
            self._update()
        self.assertFalse(self.target.exists())
        (self.source/'VERSION').write_bytes(b'v2')
        (self.source/'output').mkdir()
        (self.source/'output'/'user.wav').write_bytes(b'user data')
        with self.assertRaises(updates.VersionUpdateError):
            self._update()
        self.assertFalse(self.target.exists())

    def test_reparse_source_rejected_and_external_files_preserved(self):
        link = self.base/'source-link'
        try:
            os.symlink(str(self.source), str(link), target_is_directory=True)
        except OSError:
            self.skipTest('symlink not supported')
        self.addCleanup(link.unlink)
        with self.assertRaises(updates.VersionUpdateError):
            updates.stage_update_bundle(self.old, link, self.target, self.config, verifier=_fixture_gate)
        self.assertEqual((self.source/'VERSION').read_bytes(), b'v2')

    def test_operation_lock_blocks_overlapping_update(self):
        with updates._operation_lock(self.config):
            with self.assertRaises(updates.VersionUpdateError):
                self._update()
        self.assertFalse(self._selection().exists())

    def test_selection_write_failure_preserves_old_bytes(self):
        self._update()
        before = self._selection().read_bytes()
        original = updates._atomic_json
        def write(path, value):
            if path.name == updates.SELECTION_NAME:
                raise OSError('simulated selection commit failure')
            original(path, value)
        with mock.patch.object(updates, '_atomic_json', side_effect=write):
            with self.assertRaises(updates.VersionUpdateError):
                updates.rollback_previous_version(self.config, verifier=_fixture_gate)
        self.assertEqual(self._selection().read_bytes(), before)
        self._failed()


if __name__ == '__main__':
    unittest.main()
