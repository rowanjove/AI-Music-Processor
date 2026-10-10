import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.config as config
from core.job_spec import freeze
from core.voice_profile_report import (
    HEADING,
    KNOWN_MODEL_KEYS,
    append_effective_profile,
    audit_installed_models,
)

AUDIT = ROOT / "tests" / "fixtures" / "voice_profile_audit.json"


def _parameters():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo",
        pitch_shift=0, f0_method="harvest", index_rate=0.65, generate_midi=False,
        output_formats=["wav"], output_sample_rate=48000, bpm=120,
    )


def _install(root, profile_text):
    weights = Path(root) / "rvc" / "weights"
    weights.mkdir(parents=True)
    (weights / "demo.pth").write_bytes(b"weights")
    profiles = Path(root) / "config" / "model-profiles"
    profiles.mkdir(parents=True)
    if profile_text is not None:
        (profiles / "demo.json").write_text(profile_text, encoding="utf-8")
    return weights


class VoiceProfileTests(unittest.TestCase):
    def test_corrupt_profile_is_ignored_and_valid_override_is_frozen(self):
        with tempfile.TemporaryDirectory() as root:
            _install(root, '{"index_rate":"bad"}')
            with patch.object(config, "RVC_ROOT", str(Path(root) / "rvc")), \
                 patch.object(config, "CONFIG_DIR", str(Path(root) / "config")):
                config.CONFIG_WARNINGS.clear()
                spec = freeze(_parameters(), "voice_conversion")
            profile = spec["effective_rvc"]["profile"]
            self.assertEqual(spec["schema_version"], 1)
            self.assertEqual(spec["index_rate"], 0.65)
            self.assertEqual(profile["index_rate"], 0.65)
            self.assertEqual(profile["protect"], 0.33)
            self.assertEqual(profile["rms_mix_rate"], 0.25)
            self.assertEqual(profile["filter_radius"], 3)
            self.assertTrue(spec["effective_rvc"]["profile_warnings"])
        with tempfile.TemporaryDirectory() as root:
            _install(root, json.dumps({
                "protect": 0.2, "rms_mix_rate": 0.4, "index_rate": 0.5, "filter_radius": 4,
            }))
            with patch.object(config, "RVC_ROOT", str(Path(root) / "rvc")), \
                 patch.object(config, "CONFIG_DIR", str(Path(root) / "config")):
                config.CONFIG_WARNINGS.clear()
                spec = freeze(_parameters(), "voice_conversion")
            profile = spec["effective_rvc"]["profile"]
            self.assertEqual(spec["schema_version"], 1)
            self.assertEqual(spec["index_rate"], 0.65)
            self.assertEqual(profile["protect"], 0.2)
            self.assertEqual(profile["rms_mix_rate"], 0.4)
            self.assertEqual(profile["index_rate"], 0.5)
            self.assertEqual(profile["filter_radius"], 4)
            self.assertFalse(spec["effective_rvc"]["profile_warnings"])

    def test_report_separates_job_index_from_frozen_profile(self):
        with tempfile.TemporaryDirectory() as root:
            weights = _install(root, None)
            overridden = {
                "name": "示例",
                "pth": str(weights / "demo.pth"),
                "profile_path": str(Path(root) / "config" / "model-profiles" / "demo.json"),
                "profile": {
                    "index_rate": 0.5, "protect": 0.2, "rms_mix_rate": 0.4,
                    "filter_radius": 4, "f0_method": "harvest", "pitch": 0,
                },
                "profile_warnings": [],
            }
            (Path(root) / "config" / "model-profiles" / "demo.json").write_text("{}", encoding="utf-8")
            report = Path(root) / "process_report.md"
            report.write_text("## Durable pipeline\n\n- QC JSON: `x`\n", encoding="utf-8")
            append_effective_profile(str(report), 0.8, overridden, "demo")
            append_effective_profile(str(report), 0.8, overridden, "demo")
            text = report.read_text(encoding="utf-8")
            self.assertEqual(text.count(HEADING), 1)
            self.assertIn("任务 index_rate：0.8。RVC 使用这个值。", text)
            self.assertIn("冻结 Profile index_rate：0.5。它不会替换任务 index_rate。", text)
            self.assertIn("protect：0.2", text)
            self.assertIn("rms_mix_rate：0.4", text)
            self.assertIn("filter_radius：4", text)
            self.assertIn("与全局默认相同：否", text)
            self.assertIn("demo.json", text)
            bare = {
                "name": "周杰伦 (Jay Chou)",
                "pth": str(weights / "demo.pth"),
                "profile_path": str(Path(root) / "missing-zhoujielun.json"),
                "profile": dict(config.MODEL_PROFILE_DEFAULTS),
                "profile_warnings": ["模型 Profile 已忽略（zhoujielun: bad）：x"],
            }
            plain = Path(root) / "plain.md"
            plain.write_text("ok\n", encoding="utf-8")
            append_effective_profile(str(plain), 0.65, bare, "zhoujielun")
            plain_text = plain.read_text(encoding="utf-8")
            self.assertIn("任务 index_rate：0.65。RVC 使用这个值。", plain_text)
            self.assertIn("冻结 Profile index_rate：0.65。它不会替换任务 index_rate。", plain_text)
            self.assertIn("protect：0.33", plain_text)
            self.assertIn("rms_mix_rate：0.25", plain_text)
            self.assertIn("权重旁 profile.json：无", plain_text)
            self.assertIn("模型配置 Profile：无", plain_text)
            self.assertIn("与全局默认相同：是", plain_text)
            self.assertIn("已忽略的 Profile：模型 Profile 已忽略（zhoujielun: bad）：x", plain_text)

    def test_installed_models_match_audit_and_pipeline_keeps_job_index(self):
        record = audit_installed_models()
        fixture = json.loads(AUDIT.read_text(encoding="utf-8"))
        self.assertEqual(record, fixture)
        self.assertFalse(record["defaults_changed"])
        self.assertEqual(record["global_defaults"]["index_rate"], 0.65)
        self.assertEqual(record["global_defaults"]["protect"], 0.33)
        self.assertEqual(record["global_defaults"]["rms_mix_rate"], 0.25)
        self.assertEqual(record["global_defaults"]["filter_radius"], 3)
        models = config.get_available_rvc_models()
        directories = set()
        for key in KNOWN_MODEL_KEYS:
            self.assertTrue(record["models"][key]["present"])
            self.assertFalse(record["models"][key]["has_override_file"])
            self.assertEqual(record["models"][key]["profile_warning_count"], 0)
            self.assertTrue(record["models"][key]["matches_defaults"])
            directories.add(os.path.dirname(os.path.abspath(models[key]["pth"])))
        self.assertEqual(len(directories), 1)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        engine = (ROOT / "core" / "rvc_engine.py").read_text(encoding="utf-8")
        self.assertIn("append_effective_profile(report_path, index_rate, params.get(\"effective_rvc\") or {}, rvc_model_key)", pipeline)
        self.assertIn('if mode == "voice_conversion" and os.path.isfile(report_path):', pipeline)
        self.assertIn("index_rate=index_rate", pipeline)
        self.assertIn("'--index-rate', str(float(index_rate))", engine)
        self.assertNotIn("model_profile.get('index_rate'", engine)
        self.assertNotIn('model_profile["index_rate"]', engine)
        self.assertEqual(config.MODEL_PROFILE_DEFAULTS["index_rate"], 0.65)
        self.assertEqual(config.MODEL_PROFILE_DEFAULTS["protect"], 0.33)
        self.assertEqual(config.MODEL_PROFILE_DEFAULTS["rms_mix_rate"], 0.25)
