import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import candidate_separator as candidate


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_package_file(root, relative, content):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _freeze_inventory(root, package, assets):
    rows = []
    paths = [path for path in Path(package).rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    paths.extend(Path(path) for path in assets)
    for path in sorted(set(paths)):
        rows.append({"path": path.relative_to(root).as_posix(), "sha256": _sha(path)})
    inventory = Path(root) / "runtime-manifest" / "inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(json.dumps({"files": rows}), encoding="utf-8")
    return inventory


class CandidateSeparatorTests(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {}
        for name in ("audio_separator", "audio_separator.separator", "pymss"):
            self._saved_modules[name] = sys.modules.pop(name, None)

    def tearDown(self):
        for name in ("audio_separator.separator", "audio_separator", "pymss"):
            sys.modules.pop(name, None)
            if self._saved_modules.get(name) is not None:
                sys.modules[name] = self._saved_modules[name]

    def _source(self, root):
        path = Path(root) / "song.wav"
        sf.write(path, np.zeros((1600, 2), dtype=np.float32), 16000, subtype="FLOAT")
        return path

    def _roformer_files(self, root):
        package = Path(root) / "audio_separator"
        _write_package_file(package, "__init__.py", "")
        _write_package_file(package, "separator.py", '''
import os
import soundfile as sf
import numpy as np

class Separator:
    def __init__(self, model_file_dir, output_dir, **kwargs):
        self.model_file_dir = os.environ.get("AUDIO_SEPARATOR_MODEL_DIR", model_file_dir)
        self.output_dir = output_dir
        self.loaded = None
    def download_file_if_not_exists(self, url, path):
        raise AssertionError("the test must install the frozen offline callback")
    def load_model(self, model_filename, force_reload=False):
        self.download_file_if_not_exists("metadata-url", os.path.join(self.model_file_dir, "download_checks.json"))
        self.download_file_if_not_exists("config-url", os.path.join(self.model_file_dir, "model_bs_roformer_ep_317_sdr_12.9755.yaml"))
        self.download_file_if_not_exists("weight-url", os.path.join(self.model_file_dir, model_filename))
        assert os.path.basename(self.model_file_dir) == "models"
        self.loaded = (model_filename, force_reload)
    def separate(self, source, custom_output_names=None):
        assert self.loaded == ("model_bs_roformer_ep_317_sdr_12.9755.ckpt", True)
        assert custom_output_names == {"Vocals": "candidate_vocal", "Instrumental": "candidate_instrumental"}
        source_audio, rate = sf.read(source, always_2d=True, dtype="float32")
        paths = []
        for name in ("candidate_vocal", "candidate_instrumental"):
            path = os.path.join(self.output_dir, name + ".wav")
            sf.write(path, source_audio, rate, subtype="FLOAT")
            paths.append(path)
        return [os.path.basename(path) for path in paths]
    def close(self):
        pass
''')
        model_dir = Path(root) / "models"
        model_dir.mkdir()
        weight = model_dir / candidate.ROFORMER_MODEL
        weight.write_bytes(b"managed candidate checkpoint fixture")
        config = model_dir / candidate.ROFORMER_CONFIG
        config.write_text("model: {target_instrument: Vocals}\n", encoding="utf-8")
        support = []
        path = model_dir / "download_checks.json"
        path.write_text("{}", encoding="utf-8")
        support.append(path)
        return package, weight, config, support

    def _pymss_files(self, root):
        package = Path(root) / "pymss"
        _write_package_file(package, "__init__.py", '''
import numpy as np

class MSSeparator:
    def __init__(self, model_type, model_path, config_path, **kwargs):
        assert model_type == "bs_roformer"
        assert model_path.endswith("model_bs_roformer_ep_317_sdr_12.9755.ckpt")
        self.config = {"audio": {"sample_rate": 16000},
                       "training": {"instruments": ["vocals", "instrumental"]}}
    def separate(self, audio, pbar=False, stems=None):
        assert stems == ["vocals", "instrumental"]
        return {name: np.array(audio, dtype=np.float32) for name in stems}
    def close(self):
        pass
''')
        model_dir = Path(root) / "models"
        model_dir.mkdir()
        weight = model_dir / candidate.PYMSS_MODEL
        config = model_dir / candidate.PYMSS_CONFIG
        weight.write_bytes(b"managed candidate checkpoint fixture")
        config.write_text("audio: {sample_rate: 16000}\ntraining: {instruments: [vocals, instrumental]}\n", encoding="utf-8")
        return package, weight, config

    def test_audio_separator_roformer_api_runs_two_stems_and_commits_hashes(self):
        with tempfile.TemporaryDirectory(prefix="candidate-roformer-") as folder:
            root = Path(folder)
            source = self._source(root)
            package, weight, config, support = self._roformer_files(root)
            sys.path.insert(0, str(root))
            self.addCleanup(lambda: sys.path.remove(str(root)) if str(root) in sys.path else None)
            api_hash = candidate.package_source_sha256("audio_separator")
            _freeze_inventory(root, package, [weight, config, *support])
            identity = {
                "backend": "roformer",
                "managed_root": str(root),
                "api_package_sha256": api_hash,
                "model_filename": candidate.ROFORMER_MODEL,
                "weight_path": str(weight), "weight_sha256": _sha(weight),
                "config_path": str(config), "config_sha256": _sha(config),
                "support_file_sha256": {path.name: _sha(path) for path in support},
            }
            configured_model_dir = str(root / "models")
            original_model_dir = os.environ.get("AUDIO_SEPARATOR_MODEL_DIR")
            with patch.object(candidate.sys, "version_info", (3, 10)), \
                 patch.dict(os.environ, {"AUDIO_SEPARATOR_MODEL_DIR": configured_model_dir}):
                result = candidate.run_candidate_separator("roformer", source, root / "candidate", identity)
                self.assertEqual(os.environ.get("AUDIO_SEPARATOR_MODEL_DIR"), configured_model_dir)
            self.assertEqual(result["status"], "EXECUTED")
            self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")
            self.assertTrue(Path(result["vocal"]["path"]).is_file())
            self.assertTrue(Path(result["instrumental"]["path"]).is_file())
            self.assertEqual(result["vocal"]["sample_rate"], 16000)
            self.assertEqual(result["vocal"]["channels"], 2)
            rogue_model_dir = str(root / "outside-model-dir")
            with patch.object(candidate.sys, "version_info", (3, 10)), \
                 patch.dict(os.environ, {"AUDIO_SEPARATOR_MODEL_DIR": rogue_model_dir}):
                with self.assertRaises(candidate.CandidateIdentityError):
                    candidate.run_candidate_separator("roformer", source, root / "candidate-rogue", identity)
                self.assertEqual(os.environ.get("AUDIO_SEPARATOR_MODEL_DIR"), rogue_model_dir)
            self.assertEqual(os.environ.get("AUDIO_SEPARATOR_MODEL_DIR"), original_model_dir)

    def test_roformer_requires_its_frozen_yaml_and_rejects_unknown_local_downloads(self):
        with tempfile.TemporaryDirectory(prefix="candidate-yaml-") as folder:
            root = Path(folder)
            package, weight, config, support = self._roformer_files(root)
            sys.path.insert(0, str(root))
            self.addCleanup(lambda: sys.path.remove(str(root)) if str(root) in sys.path else None)
            wrong_config = root / "models" / "mdx_model_data.json"
            wrong_config.write_text("{}", encoding="utf-8")
            api_hash = candidate.package_source_sha256("audio_separator")
            _freeze_inventory(root, package, [weight, config, wrong_config, *support])
            identity = {
                "backend": "roformer", "managed_root": str(root),
                "api_package_sha256": api_hash, "model_filename": candidate.ROFORMER_MODEL,
                "weight_path": str(weight), "weight_sha256": _sha(weight),
                "config_path": str(wrong_config), "config_sha256": _sha(wrong_config),
                "support_file_sha256": {path.name: _sha(path) for path in support},
            }
            with self.assertRaises(candidate.CandidateIdentityError):
                candidate._verify_identity("roformer", candidate._BACKENDS["roformer"], identity)
            with self.assertRaises(candidate.CandidateBackendUnavailable):
                candidate._verify_local_frozen_asset(root / "models" / "unknown.json",
                    {"weight": weight, "config": config}, identity)

    def test_relative_output_that_escapes_work_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="candidate-output-") as folder:
            root = Path(folder)
            work = root / "work"
            work.mkdir()
            with self.assertRaises(candidate.CandidateIdentityError):
                candidate._resolve_candidate_output("../outside.wav", work)

    def test_pymss_api_uses_frozen_stems_and_float_outputs(self):
        with tempfile.TemporaryDirectory(prefix="candidate-pymss-") as folder:
            root = Path(folder)
            source = self._source(root)
            package, weight, config = self._pymss_files(root)
            sys.path.insert(0, str(root))
            self.addCleanup(lambda: sys.path.remove(str(root)) if str(root) in sys.path else None)
            api_hash = candidate.package_source_sha256("pymss")
            _freeze_inventory(root, package, [weight, config])
            identity = {
                "backend": "pymss", "managed_root": str(root),
                "api_package_sha256": api_hash,
                "model_filename": candidate.PYMSS_MODEL,
                "weight_path": str(weight), "weight_sha256": _sha(weight),
                "config_path": str(config), "config_sha256": _sha(config),
                "stems": {"vocal": "vocals", "instrumental": "instrumental"},
            }
            with patch.object(candidate.sys, "version_info", (3, 10)):
                result = candidate.run_candidate_separator("pymss", source, root / "candidate", identity)
            self.assertEqual(result["status"], "EXECUTED")
            self.assertEqual(result["api"], "pymss.MSSeparator.separate")
            self.assertEqual(result["quality_acceptance"], "NOT_ASSESSED")
            for item in (result["vocal"], result["instrumental"]):
                self.assertEqual(sf.info(item["path"]).subtype, "FLOAT")

    def test_missing_identity_or_unmanaged_model_never_falls_back_to_hp2(self):
        with tempfile.TemporaryDirectory(prefix="candidate-closed-") as folder:
            source = self._source(folder)
            with self.assertRaises(candidate.CandidateIdentityError):
                with patch.object(candidate.sys, "version_info", (3, 10)):
                    candidate.run_candidate_separator("roformer", source, Path(folder) / "out", {
                        "backend": "roformer",
                    })
            with self.assertRaises(ValueError):
                candidate.run_candidate_separator("hp2", source, Path(folder) / "out", {})


if __name__ == "__main__":
    unittest.main()
