import json
import os
import tempfile
import unittest
from pathlib import Path

from core import uvr_candidate_worker as worker


class UVRCandidateWorkerTests(unittest.TestCase):
    def test_failed_retry_artifacts_are_preserved_before_the_next_attempt(self):
        with tempfile.TemporaryDirectory(prefix="uvr-candidate-retry-") as folder:
            root = Path(folder)
            evidence = root / "candidate-worker-evidence.json"
            vocal = root / "vocal.wav"
            instrumental = root / "instrumental.wav"
            evidence.write_text(json.dumps({
                "worker": "uvr_candidate_exact_length",
                "status": "FAILED",
                "failed_work_dir": "stage-with-failed-output",
            }), encoding="utf-8")
            vocal.write_bytes(b"partial vocal")
            instrumental.write_bytes(b"partial instrumental")

            worker._preserve_previous_failed_attempt(str(evidence), (str(vocal), str(instrumental)))

            self.assertFalse(evidence.exists())
            self.assertFalse(vocal.exists())
            self.assertFalse(instrumental.exists())
            self.assertEqual(len(list(root.glob("*.previous-*"))), 3)
            archived_evidence = next(root.glob("candidate-worker-evidence.json.previous-*"))
            self.assertEqual(json.loads(archived_evidence.read_text(encoding="utf-8"))["status"], "FAILED")

    def test_successful_or_unknown_existing_output_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="uvr-candidate-no-overwrite-") as folder:
            root = Path(folder)
            evidence = root / "candidate-worker-evidence.json"
            evidence.write_text(json.dumps({"worker": "uvr_candidate_exact_length", "status": "EXECUTED"}),
                                encoding="utf-8")
            with self.assertRaises(FileExistsError):
                worker._preserve_previous_failed_attempt(str(evidence), ())

    def test_source_alias_with_failed_receipt_is_rejected_before_any_rename(self):
        with tempfile.TemporaryDirectory(prefix="uvr-candidate-source-alias-") as folder:
            root = Path(folder)
            source = root / "candidate-worker-evidence.json"
            payload = json.dumps({
                "worker": "uvr_candidate_exact_length",
                "status": "FAILED",
                "input": {"sha256_before": "source-bytes"},
            }).encode("utf-8")
            source.write_bytes(payload)
            model = root / "weight.pth"
            model.write_bytes(b"weight")
            with self.assertRaisesRegex(ValueError, "paths must be distinct"):
                worker._assert_distinct_paths(
                    str(source), str(model),
                    (str(root / "vocal.wav"), str(root / "instrumental.wav"), str(source)),
                )
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), payload)
            self.assertFalse(Path(str(source) + ".previous-{}".format(os.getpid())).exists())

    def test_output_path_must_stay_inside_experiment_work_directory(self):
        with tempfile.TemporaryDirectory(prefix="uvr-candidate-path-") as folder:
            root = Path(folder) / "work"
            root.mkdir()
            with self.assertRaisesRegex(ValueError, "work directory"):
                worker._output_path(str(root.parent / "escape.wav"), str(root))
            result = worker._output_path(str(root / "vocal.wav"), str(root))
            self.assertEqual(os.path.normcase(result), os.path.normcase(str(root / "vocal.wav")))


if __name__ == "__main__":
    unittest.main()
