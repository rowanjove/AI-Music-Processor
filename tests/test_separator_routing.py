import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.separator_routing import plan_separator_routes


class SeparatorRoutingTests(unittest.TestCase):
    def test_unprofiled_legacy_route_stays_one_hp2_pass(self):
        plan = plan_separator_routes()
        self.assertEqual(plan["status"], "PLANNED")
        self.assertEqual(plan["vocal_backend"], "hp2")
        self.assertEqual(plan["instrumental_backend"], "hp2")
        self.assertEqual(plan["execution"], "single_pass")
        self.assertFalse(plan["applied_by_job"])
        self.assertEqual(plan["quality_acceptance"], "NOT_ASSESSED")
        explicit = plan_separator_routes("hp2", "hp2")
        self.assertEqual(explicit["execution"], "single_pass")

    def test_a_different_model_is_not_executed(self):
        for vocal, instrumental in (("hp5", "hp2"), ("hp2", "hp3"), ("mdx", "roformer"), ("inst_hq_3", "kim_vocal_2")):
            plan = plan_separator_routes(vocal, instrumental)
            self.assertEqual(plan["status"], "NOT_RUN")
            self.assertIsNone(plan["execution"])
            self.assertFalse(plan["applied_by_job"])
            self.assertEqual(plan["listening_quality"], "not_assessed")
            self.assertEqual(plan["quality_acceptance"], "NOT_ASSESSED")

    def test_kim_vocal_and_inst_hq_keep_their_own_parameters(self):
        plan = plan_separator_routes("kim_vocal_2", "inst_hq_3")
        self.assertTrue(plan["available"])
        self.assertEqual(plan["status"], "PLANNED")
        self.assertEqual(plan["execution"], "split")
        self.assertFalse(plan["executed"])
        self.assertFalse(plan["applied_by_job"])
        self.assertEqual(plan["kept"], {"vocal": "kim_vocal_2", "instrumental": "inst_hq_3"})
        self.assertEqual(plan["vocal_model"]["keeps"], "vocal")
        self.assertEqual(plan["instrumental_model"]["keeps"], "instrumental")
        self.assertEqual(plan["vocal_model"]["backend_kind"], "mdx")
        self.assertEqual(plan["instrumental_model"]["backend_kind"], "mdx")
        self.assertEqual(plan["vocal_model"]["parameters"], {})
        self.assertEqual(plan["instrumental_model"]["parameters"], {})
        self.assertNotIn("agg", plan["vocal_model"]["parameters"])
        self.assertNotIn("agg", plan["instrumental_model"]["parameters"])
        self.assertEqual(plan["quality_acceptance"], "NOT_ASSESSED")
        self.assertIn("没有运行音频推理", plan["reason"])
        hp2 = plan_separator_routes()
        self.assertEqual(hp2["vocal_model"]["parameters"], {"agg": 10})
        self.assertEqual(hp2["vocal_model"]["backend_kind"], "vr")

    def test_dual_route_executes_only_when_weights_and_loader_exist(self):
        import tempfile
        from unittest.mock import patch
        from core.separator_routing import (
            INST_FILE,
            KIM_FILE,
            dual_profile,
            refuse_unexecuted_route,
            voice_separate_binding,
        )
        def fake_metadata(path):
            return {"primary_stem": "Vocals" if Path(path).name == KIM_FILE else "Instrumental"}

        with tempfile.TemporaryDirectory() as folder:
            blocked = plan_separator_routes("kim_vocal_2", "inst_hq_3", weight_dir=folder)
            self.assertFalse(blocked["executed"])
            self.assertIsNone(blocked["execution"])
            self.assertIn("缺少权重", blocked["reason"])
            self.assertIn("不会改跑 HP2", blocked["reason"])
            for name in (KIM_FILE, INST_FILE):
                path = Path(folder) / name
                path.write_bytes(b"onnx")
            with patch("core.separator_routing.mdx_loader_implemented", return_value=True), \
                 patch("core.separator_routing.default_weight_dir", return_value=folder), \
                 patch("core.mdx_separator.load_model_metadata", side_effect=fake_metadata):
                ready = plan_separator_routes("kim_vocal_2", "inst_hq_3", weight_dir=folder)
                binding = voice_separate_binding(dual_profile(), "voice_conversion")
                with self.assertRaises(RuntimeError) as caught:
                    refuse_unexecuted_route(binding)
        self.assertFalse(ready["executed"])
        self.assertEqual(ready["execution"], "split")
        self.assertEqual(ready["status"], "PLANNED")
        self.assertEqual(ready["vocal_model"]["parameters"], {})
        self.assertEqual(ready["instrumental_model"]["parameters"], {})
        self.assertNotIn("agg", ready["vocal_model"]["parameters"])
        self.assertEqual(ready["quality_acceptance"], "NOT_ASSESSED")
        self.assertIn("没有运行音频推理", ready["reason"])
        self.assertEqual(binding["execution"], "split")
        self.assertFalse(binding["route"]["executed"])
        self.assertIn("没有执行推理", str(caught.exception))
        self.assertIn("不会改跑 HP2", str(caught.exception))
