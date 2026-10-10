import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import transcriber


class LMMSFirstRunTests(unittest.TestCase):
    def test_new_profile_is_ready_for_import_without_overwriting_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'program'
            root.mkdir()
            with mock.patch.dict(os.environ, {'AI_MUSIC_PROCESSOR_DATA_ROOT':str(Path(folder)/'data')}):
                config = Path(transcriber.lmms_config(str(root)))
                document = ET.parse(config).getroot()
                self.assertTrue(Path(document.find('paths').get('workingdir')).is_dir())
                self.assertEqual(document.find('app').get('configured'), '1')
                self.assertEqual(document.find('mixer').get('audiodev'), 'SDL (Simple DirectMedia Layer)')
                original = b'<lmms><app configured="0"/><mixer audiodev="user choice"/></lmms>'
                config.write_bytes(original)
                self.assertEqual(transcriber.lmms_config(str(root)), str(config))
                self.assertEqual(config.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
