import json
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.separator_benchmark import (
    DEPENDENCY_MISSING,
    IMPLEMENTED_NOT_SELECTABLE,
    NOT_A_VOICE_BACKEND,
    REQUIRES_PYTHON_310,
    SELECTABLE,
    WEIGHT_MISSING,
    WEIGHT_PRESENT,
    compare_recorded_runs,
    separator_catalog,
)
from core.separator_choice import resolve_separator_request
from core.candidate_separator import _BACKENDS as CANDIDATE_BACKENDS
from core.separator_routing import INST_HQ_3, KIM_VOCAL_2, default_weight_dir, dual_model_identities, plan_separator_routes


def _run(backend, peak, clipped=0):
    return {
        "executed": True,
        "source_id": "short",
        "backend": backend,
        "job_selectable": backend == "hp2",
        "vocal_peak": peak,
        "vocal_duration": 15.0,
        "vocal_clipped": clipped,
        "instrumental_peak": peak / 2,
        "instrumental_clipped": 0,
    }


class SeparatorBenchmarkTests(unittest.TestCase):
    def test_only_hp2_is_selectable_when_its_weight_is_the_only_file(self):
        with tempfile.TemporaryDirectory(prefix="separator-catalog-") as folder:
            for name in ("HP2_all_vocals.pth", "HP3_all_vocals.pth"):
                with open(os.path.join(folder, name), "wb") as handle:
                    handle.write(b"weight")
            with open(os.path.join(folder, "HP5_only_main_vocal.pth"), "wb") as handle:
                handle.write(b"")
            catalog = separator_catalog(folder)
        by_id = {row["id"]: row for row in catalog["backends"]}
        self.assertEqual(catalog["selectable"], ["hp2"])
        self.assertEqual(catalog["default_backend"], "hp2")
        self.assertEqual(by_id["hp2"]["status"], SELECTABLE)
        self.assertEqual(by_id["hp2"]["benchmark_status"], "NOT_RUN")
        self.assertEqual(by_id["hp3"]["status"], WEIGHT_PRESENT)
        self.assertFalse(by_id["hp3"]["job_selectable"])
        self.assertTrue(by_id["hp3"]["job_path_rejects"])
        self.assertEqual(by_id["hp5"]["status"], "WEIGHT_MISSING")
        self.assertFalse(by_id["hp5"]["weight_present"])
        self.assertFalse(by_id["hp5"]["job_selectable"])
        mdx = by_id["mdx"]
        route = plan_separator_routes(KIM_VOCAL_2, INST_HQ_3)
        self.assertEqual(mdx["profile_id"], "dual_profile")
        self.assertEqual(mdx["implementation_status"], "IMPLEMENTED")
        self.assertEqual(mdx["available"], route["available"])
        self.assertEqual(mdx["job_path_rejects"], True)
        self.assertEqual(mdx["benchmark_status"], "NOT_RUN")
        if route["available"]:
            self.assertEqual(mdx["status"], IMPLEMENTED_NOT_SELECTABLE)
            self.assertEqual(mdx["model_identities"], dual_model_identities())
        for backend_id in ("roformer", "pymss"):
            row = by_id[backend_id]
            candidate = CANDIDATE_BACKENDS[backend_id]
            self.assertEqual(row["implementation_status"], "IMPLEMENTED")
            self.assertFalse(row["job_selectable"])
            self.assertFalse(row["available"])
            self.assertEqual(row["benchmark_status"], "NOT_RUN")
            self.assertEqual(row["dependency_available"], importlib.util.find_spec(candidate["package"]) is not None)
            self.assertIn(row["status"], {DEPENDENCY_MISSING, WEIGHT_MISSING, REQUIRES_PYTHON_310})
            self.assertEqual(row["availability_blockers"], [
                blocker for blocker, present in (
                    (REQUIRES_PYTHON_310, sys.version_info < (3, 10)),
                    (DEPENDENCY_MISSING, importlib.util.find_spec(candidate["package"]) is None),
                    (WEIGHT_MISSING, not all((
                        Path("tools/UVR5/models/RoFormer_Models", candidate["model"]).is_file(),
                        Path("tools/UVR5/models/RoFormer_Models", candidate["config"]).is_file(),
                        *(Path("tools/UVR5/models/RoFormer_Models", name).is_file() for name in candidate["support_files"]),
                    ))),
                ) if present
            ])
        ensemble = by_id["ensemble"]
        self.assertEqual(ensemble["implementation_status"], "IMPLEMENTED")
        self.assertEqual(ensemble["profile"], "hp2 + kim_vocal_2/inst_hq_3")
        self.assertEqual(ensemble["combiner"], "samplewise_arithmetic_mean")
        self.assertFalse(ensemble["job_selectable"])
        self.assertTrue(ensemble["job_path_rejects"])
        self.assertEqual(ensemble["available"], by_id["hp2"]["weight_present"] and mdx["available"])
        self.assertEqual(ensemble["benchmark_status"], "NOT_RUN")
        for backend_id in ("roformer", "pymss", "ensemble"):
            self.assertFalse(by_id[backend_id]["job_selectable"])
            self.assertEqual(by_id[backend_id]["benchmark_status"], "NOT_RUN")
        self.assertEqual(by_id["htdemucs"]["status"], NOT_A_VOICE_BACKEND)
        for mode in catalog["quality_modes"].values():
            self.assertEqual(mode["status"], "NOT_CONFIGURED")
            self.assertIsNone(mode["backend"])
        self.assertEqual(catalog["listening_quality"], "not_assessed")
        self.assertEqual(catalog["quality_acceptance"], "NOT_ASSESSED")

    def test_workspace_catalog_keeps_the_job_path_on_hp2(self):
        catalog = separator_catalog()
        by_id = {row["id"]: row for row in catalog["backends"]}
        self.assertEqual(catalog["selectable"], ["hp2"])
        self.assertEqual(resolve_separator_request("hp2")["model_name"], "HP2_all_vocals.pth")
        for backend_id in ("hp3", "hp5", "mdx", "roformer", "pymss"):
            with self.assertRaises(ValueError):
                resolve_separator_request(backend_id)
            self.assertFalse(by_id[backend_id]["job_selectable"])
        self.assertEqual(by_id["mdx"]["profile_id"], "dual_profile")
        self.assertEqual(by_id["mdx"]["available"],
                         plan_separator_routes(KIM_VOCAL_2, INST_HQ_3, default_weight_dir())["available"])
        if by_id["hp3"]["weight_present"]:
            self.assertEqual(by_id["hp3"]["status"], WEIGHT_PRESENT)
            self.assertEqual(by_id["hp3"]["benchmark_status"], "NOT_RUN")
        if by_id["hp5"]["weight_present"]:
            self.assertEqual(by_id["hp5"]["status"], WEIGHT_PRESENT)

    def test_recorded_numbers_do_not_select_or_accept_a_backend(self):
        missing = compare_recorded_runs("short", {"executed": False}, {"executed": False})
        self.assertEqual(missing["status"], "NOT_RUN")
        self.assertIsNone(missing["metrics"])
        self.assertFalse(missing["metrics_are_audited_separator"])
        compared = compare_recorded_runs("short", _run("hp2", 0.5), _run("hp3", 0.25, clipped=2))
        self.assertEqual(compared["status"], "MEASURED")
        self.assertEqual(compared["metrics"]["vocal_peak_delta"], -0.25)
        self.assertEqual(compared["metrics"]["vocal_clipped_delta"], 2)
        self.assertFalse(compared["other_job_selectable"])
        self.assertFalse(compared["selectable_changed"])
        self.assertEqual(compared["listening_quality"], "not_assessed")
        self.assertEqual(compared["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(compared["acceptance_status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertIn("人工听审", compared["missing_evidence"])
        with self.assertRaises(ValueError):
            illegal = _run("hp3", 0.2)
            illegal["job_selectable"] = True
            compare_recorded_runs("short", _run("hp2", 0.5), illegal)

    def test_short_sample_run_is_recorded_and_not_accepted(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "separator_benchmark_vcq020.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["listening_quality"], "not_assessed")
        self.assertEqual(fixture["quality_acceptance"], "NOT_ASSESSED")
        self.assertEqual(fixture["selectable"], ["hp2"])
        self.assertFalse(fixture["metrics_are_audited_separator"])
        self.assertEqual(fixture["acceptance_status"], "READY_FOR_EXTERNAL_VALIDATION")
        self.assertEqual(fixture["runs"]["hp2"]["vocal_peak"], 0.571624755859375)
        self.assertEqual(fixture["runs"]["hp2"]["sample_rate"], 44100)
        self.assertFalse(fixture["runs"]["hp3"]["job_selectable"])
        self.assertFalse(fixture["runs"]["hp5"]["job_selectable"])
        self.assertEqual(fixture["hp3_minus_hp2"]["vocal_clipped_delta"], 0)
        self.assertEqual(fixture["runs"]["mdx"]["benchmark_status"] if "mdx" in fixture["runs"] else "NOT_RUN", "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
