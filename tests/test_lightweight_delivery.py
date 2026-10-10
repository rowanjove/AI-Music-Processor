import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_lightweight import build, source_files


class LightweightDeliveryTests(unittest.TestCase):
    def test_source_inventory_excludes_large_runtime_and_user_trees(self):
        paths = {relative.casefold() for relative, _path in source_files(ROOT)}
        self.assertIn("core/online_components.py", paths)
        self.assertIn("scripts/install_components.ps1", paths)
        for forbidden in ("runtime/", "tools/", "models/", "output/", "song/", "config/", "dist/"):
            self.assertFalse(any(item == forbidden or item.startswith(forbidden) for item in paths), forbidden)
        binary_paths = {item for item in paths if item.endswith(('.exe', '.dll', '.pyd', '.pth', '.pt', '.onnx', '.wav', '.mp3', '.zip'))}
        self.assertTrue(binary_paths.issubset({'ai-music-processor.exe'}), binary_paths)

    def test_build_is_deterministic_enough_and_manifest_hashes_match(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "AI-Music-Processor-source.zip"
            result = build(output, ROOT)
            self.assertEqual(result["status"], "CREATED")
            self.assertLess(result["zip_bytes"], 100 * 1024 * 1024)
            self.assertTrue(Path(result["sha256sums"]).is_file())
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(archive.read("lightweight-manifest.json"))
                names = {name.casefold() for name in archive.namelist()}
                self.assertIn("lightweight_release", names)
                self.assertIn("portable_release", names)
                self.assertEqual(len(names), len(archive.namelist()))
                for record in manifest["files"]:
                    payload = archive.read(record["path"])
                    self.assertEqual(len(payload), record["size"], record["path"])
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"], record["path"])
                self.assertNotIn("tools/rvc/rvc-beta-v2-0618/runtime/python.exe", names)


if __name__ == "__main__":
    unittest.main()
