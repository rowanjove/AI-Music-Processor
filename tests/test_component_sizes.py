import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.component_sizes import measure_component_sizes


class ComponentSizeTests(unittest.TestCase):
    def test_inventory_directory_filter_and_cache_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'engine').mkdir()
            (root / 'engine/managed.py').write_bytes(b'code')
            (root / 'engine/training.wav').write_bytes(b'user training data')
            (root / 'runtime-manifest').mkdir()
            inventory = root / 'runtime-manifest/inventory.json'
            inventory.write_text(json.dumps({'files': [{'path': 'engine/managed.py'}]}), encoding='utf-8')
            row = {'id': 'engine.test', 'ownership': 'managed', 'kind': 'tool', 'implemented': True,
                   'probes': [{'type': 'tool', 'release_path': 'engine'}]}
            result = measure_component_sizes(root, {'components': [row]})['components']['engine.test']
            self.assertEqual(result['install_bytes'], 4)
            row['id'] = '../outside'
            refused = measure_component_sizes(root, {'components': [row]}, root / 'cache')['components']['../outside']
            self.assertTrue(refused['errors'])
            inventory.write_text(json.dumps({'files': [{'path': '../escape.py'}]}), encoding='utf-8')
            with self.assertRaises(ValueError):
                measure_component_sizes(root, {'components': [row]})

    def test_whole_distribution_shared_union_and_unknown_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / 'tools/RVC/RVC-beta-v2-0618/runtime/Lib/site-packages'
            metadata = site / 'torch-1.0.dist-info'
            metadata.mkdir(parents=True)
            (metadata / 'METADATA').write_text('Name: torch\nVersion: 1.0\n', encoding='utf-8')
            (metadata / 'RECORD').write_text('torch/module.py,,\ntorch-1.0.dist-info/METADATA,,\ntorch-1.0.dist-info/RECORD,,\n', encoding='utf-8')
            (site / 'torch').mkdir()
            (site / 'torch/module.py').write_bytes(b'payload-not-just-metadata')
            rows = [{'id': 'runtime.python', 'ownership': 'managed', 'kind': 'runtime', 'implemented': True, 'probes': []},
                    {'id': 'runtime.torch', 'ownership': 'managed', 'kind': 'runtime', 'implemented': True, 'probes': []},
                    {'id': 'mode.test', 'ownership': 'managed', 'kind': 'mode_pack', 'implemented': True,
                     'probes': [], 'dependencies': ['runtime.python', 'runtime.torch']}]
            result = measure_component_sizes(root, {'components': rows})['components']
            self.assertEqual(result['runtime.torch']['file_count'], 3)
            self.assertEqual(result['runtime.python']['install_bytes'], result['runtime.torch']['install_bytes'])
            self.assertEqual(result['mode.test']['install_bytes'], result['runtime.python']['install_bytes'])
            self.assertIsNone(result['runtime.torch']['download_bytes'])
            self.assertGreater(result['runtime.torch']['shared_install_bytes'], 0)

    def test_real_cache_and_download_sizes_and_escape_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime').mkdir()
            (root / 'runtime/tool.exe').write_bytes(b'installed')
            cache = root / 'cache'
            (cache / 'tool.test').mkdir(parents=True)
            (cache / 'tool.test' / 'partial').write_bytes(b'cache')
            archive = root / 'offline.zip'
            archive.write_bytes(b'downloaded-archive')
            row = {'id': 'tool.test', 'ownership': 'managed', 'kind': 'tool', 'implemented': True,
                   'probes': [{'type': 'tool', 'release_path': 'runtime/tool.exe'}]}
            result = measure_component_sizes(root, {'components': [row]}, cache, {'tool.test': archive})['components']['tool.test']
            self.assertEqual((result['install_bytes'], result['cache_bytes'], result['download_bytes']), (9, 5, 18))
            row['probes'][0]['release_path'] = '../escape.exe'
            refused = measure_component_sizes(root, {'components': [row]})['components']['tool.test']
            self.assertIsNone(refused['install_bytes'])
            self.assertTrue(refused['errors'])


if __name__ == '__main__':
    unittest.main()
