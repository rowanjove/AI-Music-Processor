import json
import os
import pickle
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import MODEL_PROFILE_DEFAULTS, get_available_rvc_models
from core.rvc_model_inspector import inspect_checkpoint, inspect_model, read_checkpoint, read_index


class _Tensor(object):
    def __init__(self, shape):
        self.shape = shape


def _checkpoint(version="v2", feature=768, speakers=3, rate=48000, f0=1):
    return {
        "version": version,
        "f0": f0,
        "sr": "48k" if rate == 48000 else "40k",
        "info": "1epoch",
        "config": [1025, 32, 192, 192, feature, 2, 6, 3, 0, "1", [], [], [], 512, [], speakers, 256, rate],
        "weight": {
            "emb_g.weight": _Tensor((speakers, 256)),
            "enc_p.emb_phone.weight": _Tensor((192, feature)),
        },
    }


class RvcModelInspectorTests(unittest.TestCase):
    def test_checkpoint_facts_are_not_a_quality_score(self):
        inspected = inspect_checkpoint(_checkpoint())
        self.assertTrue(inspected["readable"])
        self.assertEqual(inspected["version"], "v2")
        self.assertEqual(inspected["sample_rate"], 48000)
        self.assertTrue(inspected["rate_label_matches_config"])
        self.assertEqual(inspected["feature_dim"], 768)
        self.assertEqual(inspected["architecture_feature_dim"], 768)
        self.assertEqual(inspected["embedding_rows"], 3)
        self.assertEqual(inspected["speaker_count"], "NOT_ASSESSED")
        self.assertEqual(inspected["worker_class"], "SynthesizerTrnMs768NSFsid")
        self.assertEqual(inspected["training_metadata"], "1epoch")
        self.assertEqual(inspected["quality_acceptance"], "NOT_ASSESSED")
        self.assertFalse(inspect_checkpoint({"weight": {}})["readable"])
        blank = inspect_checkpoint({
            "version": "v2", "f0": 1, "sr": "48k", "info": "  ", "config": [48000],
            "weight": {"emb_g.weight": _Tensor((1, 256))},
        })
        self.assertIsNone(blank["training_metadata"])
        self.assertIsNone(blank["feature_dim"])

    def test_index_dimension_mismatch_is_not_called_compatible(self):
        model = {"name": "synthetic", "pth": "model.pth", "profile": dict(MODEL_PROFILE_DEFAULTS)}
        report = inspect_model("synthetic", model, inspect_checkpoint(_checkpoint()), {"readable": True, "dimension": 256, "count": 4, "bytes": 10})
        self.assertEqual(report["index"]["dimension_relation"], "DIMENSION_MISMATCH")
        self.assertEqual(report["index"]["same_training_run"], "NOT_ASSESSED")
        self.assertTrue(report["profile_matches_global_default"])
        self.assertFalse(report["profile_calibrated"])
        self.assertEqual(report["calibration_status"], "NOT_ASSESSED")
        self.assertEqual(report["singing_suitability"], "NOT_ASSESSED")
        self.assertEqual(report["health"], "INSPECTED")
        missing = inspect_model("synthetic", model, inspect_checkpoint(_checkpoint()), {"readable": False, "dimension": None, "count": None})
        self.assertEqual(missing["index"]["dimension_relation"], "NOT_ASSESSED")

    def test_file_readers_round_trip_a_tiny_checkpoint_and_index(self):
        import faiss
        import numpy
        import torch
        with tempfile.TemporaryDirectory() as folder:
            checkpoint_path = os.path.join(folder, "tiny.pth")
            index_path = os.path.join(folder, "tiny.index")
            torch.save({
                "version": "v1",
                "f0": 0,
                "sr": "40k",
                "info": "",
                "config": [1025, 32, 192, 192, 256, 2, 6, 3, 0, "1", [3], [[1]], [8], 512, [8], 2, 256, 40000],
                "weight": {
                    "emb_g.weight": torch.zeros(2, 256),
                    "enc_p.emb_phone.weight": torch.zeros(192, 256),
                },
            }, checkpoint_path)
            index = faiss.IndexFlatL2(256)
            index.add(numpy.zeros((2, 256), dtype="float32"))
            faiss.write_index(index, index_path)
            empty = os.path.join(folder, "empty.pth")
            open(empty, "wb").close()
            # The inspector must use the data-only parser even for a valid
            # checkpoint; no torch pickle loader is permitted on this path.
            from unittest import mock
            with mock.patch.object(torch, "load", side_effect=AssertionError("torch.load must not run")):
                checkpoint = read_checkpoint(checkpoint_path)
            facts = read_index(index_path)
            self.assertEqual(read_checkpoint(empty)["readable"], False)
            self.assertEqual(read_index(os.path.join(folder, "missing.index"))["readable"], False)
        self.assertEqual(checkpoint["version"], "v1")
        self.assertEqual(checkpoint["feature_dim"], 256)
        self.assertEqual(checkpoint["sample_rate"], 40000)
        self.assertEqual(checkpoint["worker_class"], "SynthesizerTrnMs256NSFsid_nono")
        self.assertIsNone(checkpoint["training_metadata"])
        self.assertFalse(checkpoint["inference_compatible"])
        self.assertEqual(checkpoint["inference_guard"], None)
        self.assertEqual(facts["dimension"], 256)
        self.assertEqual(facts["count"], 2)
        report = inspect_model("tiny", {"name": "tiny", "profile": dict(MODEL_PROFILE_DEFAULTS)}, checkpoint, facts)
        self.assertEqual(report["index"]["dimension_relation"], "DIMENSION_MATCH")
        self.assertFalse(report["profile_calibrated"])

    def test_user_checkpoint_inspection_rejects_pickle_globals_without_loading(self):
        class Exploit(object):
            def __reduce__(self):
                return (print, ("untrusted checkpoint pickle executed",))

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "untrusted.pth")
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("checkpoint/data.pkl", pickle.dumps(Exploit(), protocol=2))
            import torch
            from unittest import mock
            with mock.patch.object(torch, "load", side_effect=AssertionError("torch.load must not run")):
                report = read_checkpoint(path)
        self.assertFalse(report["readable"])

    def test_installed_models_match_the_recorded_probe(self):
        fixture_path = ROOT / "tests" / "fixtures" / "rvc_model_inspector_vcq022.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        installed = get_available_rvc_models()
        self.assertEqual(fixture["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(fixture["listening_quality"], "not_assessed")
        for key, expected in fixture["models"].items():
            info = installed[key]
            self.assertEqual(os.path.getsize(info["pth"]), expected["pth_bytes"])
            self.assertEqual(os.path.basename(info["index"]), expected["index_name"])
            self.assertEqual(os.path.getsize(info["index"]), expected["index_bytes"])
            self.assertEqual(expected["profile_calibrated"], False)
            self.assertEqual(expected["speaker_count"], "NOT_ASSESSED")
            self.assertEqual(expected["same_training_run"], "NOT_ASSESSED")

    def test_imported_user_files_are_discovered_as_separate_models(self):
        from unittest import mock
        from core import config
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            imported=root/'models'/'rvc'/'imported'
            imported.mkdir(parents=True)
            pth=imported/'my singer.pth'
            pth.write_bytes(b'not yet inspected')
            index=imported/'my singer.index'
            index.write_bytes(b'index bytes')
            managed=root/'rvc-root'
            (managed/'weights').mkdir(parents=True)
            with mock.patch.object(config,'BASE_DIR',folder),mock.patch.object(config,'RVC_ROOT',str(managed)):
                models=get_available_rvc_models()
        self.assertIn('imported_my singer',models)
        self.assertEqual(models['imported_my singer']['pth'],str(pth))
        self.assertEqual(models['imported_my singer']['index'],str(index))
