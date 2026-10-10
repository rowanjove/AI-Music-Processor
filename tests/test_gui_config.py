import importlib.machinery
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import core.config as config
from core.transcriber import validate_editor_input


class ConfigResilienceTests(unittest.TestCase):
    def test_invalid_profile_isolated_and_profile_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            rvc_root = Path(root) / 'rvc'
            (rvc_root / 'weights').mkdir(parents=True)
            (rvc_root / 'weights' / 'zhoujielun.pth').write_bytes(b'fixture')
            profile_dir = Path(root) / 'config' / 'model-profiles'
            profile_dir.mkdir(parents=True)
            (profile_dir / 'zhoujielun.json').write_text('{"index_rate":"bad"}', encoding='utf-8')
            with patch.object(config, 'RVC_ROOT', str(rvc_root)), \
                 patch.object(config, 'CONFIG_DIR', str(Path(root) / 'config')):
                config.CONFIG_WARNINGS.clear()
                models = config.get_available_rvc_models()
                self.assertEqual(models['zhoujielun']['profile']['index_rate'], .65)
                self.assertTrue(models['zhoujielun']['profile_warnings'])
                target = profile_dir / 'saved.json'
                config.save_model_profile(str(target), {'pitch': 3, 'f0_method': 'pm'})
                self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['pitch'], 3)
                self.assertFalse(list(profile_dir.glob('.profile-*.tmp')))

    def test_corrupt_app_settings_fall_back_with_warning(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / 'app.json').write_text('{', encoding='utf-8')
            with patch.object(config, 'CONFIG_DIR', str(root_path)):
                config.CONFIG_WARNINGS.clear()
                self.assertEqual(config.load_settings(), {})
                self.assertTrue(any('app.json' in item for item in config.get_config_warnings()))

    def test_invalid_cache_policy_is_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / 'app.json').write_text(
                json.dumps({'cache_policy': {'enabled': 'yes', 'quota_bytes': -1, 'ttl_days': None}}),
                encoding='utf-8')
            with patch.object(config, 'CONFIG_DIR', str(root_path)):
                config.CONFIG_WARNINGS.clear()
                settings = config.load_settings()
                self.assertTrue(settings['cache_policy']['enabled'])
                self.assertEqual(settings['cache_policy']['quota_bytes'], 20 * 1024**3)
                self.assertIsNone(settings['cache_policy']['ttl_days'])
                self.assertTrue(config.get_config_warnings())

    def test_invalid_setting_types_and_profile_contract_are_rejected(self):
        with tempfile.TemporaryDirectory() as root,patch.object(config,'CONFIG_DIR',root):
            Path(root,'app.json').write_text(json.dumps(dict(generate_midi='bad',custom_output_dir=None,output_formats=[{}],cache_policy=dict(quota_bytes=float('nan')))),encoding='utf-8')
            value=config.load_settings()
            self.assertNotIn('generate_midi',value);self.assertNotIn('output_formats',value)
            self.assertEqual(value['cache_policy']['quota_bytes'],20*1024**3)
        with self.assertRaises(ValueError):config._validate_model_profile({'protect':.8})

    def test_new_opt_in_settings_round_trip_without_erasing_user_fields(self):
        with tempfile.TemporaryDirectory() as root, patch.object(config, 'CONFIG_DIR', root):
            path = Path(root) / 'app.json'
            path.write_text(json.dumps({
                'parameter_search_enabled': True,
                'parameter_search_budget': 'balanced',
                'source_hint': 'udio',
                'source_preclean_enabled': True,
                'vocal_restoration_effect': 'de_echo_normal',
                'user_extension': {'keep': 7},
            }), encoding='utf-8')
            config.CONFIG_WARNINGS.clear()
            values = config.load_settings()
            self.assertTrue(values['parameter_search_enabled'])
            self.assertEqual(values['source_hint'], 'udio')
            self.assertTrue(values['source_preclean_enabled'])
            values['parameter_search_budget'] = 'fast'
            config.save_settings(values)
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['parameter_search_budget'], 'fast')
            self.assertEqual(saved['user_extension'], {'keep': 7})

            path.write_text(json.dumps({'source_hint': 'untrusted', 'source_preclean_enabled': 'yes'}), encoding='utf-8')
            sanitized = config.load_settings()
            self.assertNotIn('source_hint', sanitized)
            self.assertNotIn('source_preclean_enabled', sanitized)


class EditorInputTests(unittest.TestCase):
    def test_supported_editor_inputs_are_checked_without_claiming_import(self):
        with tempfile.TemporaryDirectory() as root:
            midi = Path(root) / 'result.mid'
            midi.write_bytes(b'MThd')
            valid, _ = validate_editor_input('musescore', str(midi))
            self.assertTrue(valid)
            valid, reason = validate_editor_input('lmms', str(Path(root) / 'missing.mid'))
            self.assertFalse(valid)
            self.assertIn('不存在', reason)
            wav = Path(root) / 'result.wav'
            wav.write_bytes(b'fixture')
            valid, reason = validate_editor_input('musescore', str(wav))
            self.assertFalse(valid)
            self.assertIn('不支持', reason)


class GuiQueueFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.ui = importlib.machinery.SourceFileLoader(
            'music_ui_gui_config_tests', str(root / 'AI_Music_Processor.pyw')).load_module()

    def test_queue_persistence_error_does_not_stop_polling(self):
        class Tree:
            def selection(self): return []
            def selection_set(self, key): self.selected = key
            def set(self, *args): pass

        class Status:
            def config(self, **kwargs): self.text = kwargs.get('text')

        app = object.__new__(self.ui.AIMusicProcessorApp)
        app.events = queue.Queue()
        app.is_running = True
        app.active_key = 'row'
        app.pending = {'row': {'path': 'fixture.wav'}}
        app.pending_paths = {'fixture.wav'}
        app.tree = Tree()
        app.status = Status()
        app.result_manifests = {}
        app.last_manifest = None
        app.queue_dirty = False
        app.cache_cleanup_token = 0
        app.cache_stats_token = 0
        app.log_messages = []
        app.log = lambda value: app.log_messages.append(str(value))
        app.refresh_history = lambda: None
        app.save_queue = lambda: (_ for _ in ()).throw(OSError('disk full'))
        scheduled = []
        app.after = lambda delay, callback: scheduled.append((delay, callback))
        app.events.put(('done', ('row', {'qc': {'status': 'PASS'}, 'output_paths': {}})))
        app.events.put(('idle', None))
        app.poll_events()
        self.assertFalse(app.is_running)
        self.assertTrue(scheduled)
        self.assertTrue(app.queue_dirty)
        self.assertTrue(any('队列保存失败' in item for item in app.log_messages))

    def test_result_actions_resolve_selected_queue_job(self):
        class Tree:
            def selection(self): return ['selected']

        app = object.__new__(self.ui.AIMusicProcessorApp)
        app.tree = Tree()
        app.result_manifests = {
            'selected': {'job_id': 'selected', 'output_paths': {'master_wav': 'selected.wav'}}
        }
        app.last_manifest = {'job_id': 'older'}
        app.selected_job = lambda: (_ for _ in ()).throw(AssertionError('must not use history fallback'))
        self.assertEqual(app.selected_manifest()['job_id'], 'selected')

    def test_history_selection_wins_over_retained_queue_selection(self):
        class Tree:
            def selection(self):return ['queue-job']
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'project.json').write_text(json.dumps({'job_id':'history-job','output_paths':{'master_wav':'h.wav'}}),encoding='utf-8')
            app=object.__new__(self.ui.AIMusicProcessorApp)
            app.tree=Tree();app.result_surface='history'
            app.result_manifests={'queue-job':{'job_id':'queue-job'}}
            app.selected_job=lambda:dict(project_dir=directory,source_path='source')
            self.assertEqual(app.selected_manifest()['job_id'],'history-job')
            parameters=self.ui.processing_parameters(dict(mode='polish',schema_version=1,spec_hash='x',effective_rvc={},pitch_shift=0))
            self.assertEqual(parameters,dict(mode='polish',pitch_shift=0))
            parameters=self.ui.processing_parameters(dict(mode='polish',source_hint='suno',source_preclean_enabled=True,parameter_search_enabled=True))
            self.assertEqual(parameters,dict(mode='polish',source_hint='suno',source_preclean_enabled=True,parameter_search_enabled=True))

    def test_unexpected_callback_error_still_schedules_poll(self):
        app=object.__new__(self.ui.AIMusicProcessorApp);calls=[]
        app._drain_events=lambda:(_ for _ in ()).throw(OSError('injected DB failure'))
        app.log=lambda _:None
        app.status=type('Status',(),{'config':lambda *args,**kwargs:None})()
        app.after=lambda delay,callback:calls.append(delay)
        app.poll_events();self.assertEqual(calls,[100])

    def test_offline_component_mutation_is_blocked_while_audio_runs(self):
        class Status:
            def config(self, **kwargs): self.text = kwargs.get('text')

        class Button:
            def __init__(self): self.states=[]
            def state(self, value): self.states.append(value)

        app=object.__new__(self.ui.AIMusicProcessorApp)
        app.is_running=True
        app.is_processing=False
        app.component_install_running=False
        app.component_install_button=Button()
        app.wizard_frame=type('Wizard',(),{'import_button':Button()})()
        app.status=Status()
        app.log_messages=[]
        app.log=lambda value:app.log_messages.append(str(value))
        app._refresh_component_mutation_controls()
        self.assertEqual(app.component_install_button.states[-1],['disabled'])
        self.assertEqual(app.wizard_frame.import_button.states[-1],['disabled'])
        with patch.object(self.ui.filedialog,'askdirectory') as choose:
            app.import_offline_components()
        choose.assert_not_called()
        self.assertIn('正在运行', app.status.text)
        self.assertTrue(any('正在运行' in row for row in app.log_messages))

    def test_audio_start_is_blocked_during_offline_install(self):
        class Status:
            def config(self, **kwargs): self.text = kwargs.get('text')

        app=object.__new__(self.ui.AIMusicProcessorApp)
        app.is_running=False
        app.component_install_running=True
        app.status=Status()
        app.start()
        self.assertIn('离线组件正在安装', app.status.text)


if __name__ == '__main__':
    unittest.main()
