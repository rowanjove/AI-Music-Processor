import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_portable_acceptance as runner


class PortableAcceptanceRunnerTests(unittest.TestCase):
    def test_report_keeps_unrun_matrix_unrun_even_with_operator_attestation(self):
        with tempfile.TemporaryDirectory(prefix="amp acceptance fixture ") as directory:
            root = Path(directory)
            manifest_dir = root / "runtime-manifest"
            manifest_dir.mkdir()
            manifest_path = manifest_dir / "runtime.json"
            manifest_path.write_text(json.dumps({"snapshot_sha256": "a" * 64}), encoding="utf-8")
            report = runner.build_report(root, {"status": "PASS"}, operator_attested=True)
            self.assertEqual(report["classification"], "OPERATOR_ATTESTED_PREFLIGHT")
            self.assertTrue(report["operator_clean_machine_attestation"])
            self.assertTrue(all(status == "NOT_RUN" for status in report["acceptance_matrix"].values()))
            self.assertEqual(report["runtime_snapshot_sha256"], "a" * 64)

    def test_evidence_path_must_be_absolute_and_outside_package(self):
        with tempfile.TemporaryDirectory(prefix="amp acceptance paths ") as directory:
            root = Path(directory) / "package"
            data = Path(directory) / "user-data"
            root.mkdir()
            data.mkdir()
            with self.assertRaisesRegex(ValueError, "absolute"):
                runner._output_path(root, data, "relative.json")
            with self.assertRaisesRegex(ValueError, "outside"):
                runner._output_path(root, data, str(root / "bad.json"))
            self.assertEqual(runner._output_path(root, data, str(data / "ok.json")), data / "ok.json")


if __name__ == "__main__":
    unittest.main()
