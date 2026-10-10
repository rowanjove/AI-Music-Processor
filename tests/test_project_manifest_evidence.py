import json
import shutil
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

from core.pipeline import AudioPipeline
from core.storage import JobStore, sha256_file


class ProjectManifestEvidenceTests(unittest.TestCase):
    def test_process_file_done_writes_authoritative_project_and_stage_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            time = np.arange(48000, dtype=np.float32) / 48000
            samples = .1 * np.sin(2*np.pi*440*time)
            sf.write(source, np.column_stack((samples, samples)), 48000, subtype="FLOAT")
            store = JobStore(root / "jobs.db")
            pipeline = AudioPipeline(storage=store, log_callback=lambda _message: None)

            def ingest(path, job_temp, **_kwargs):
                normalized = Path(job_temp) / "01_standard_input.wav"
                shutil.copyfile(path, normalized)
                return {"standard_wav": str(normalized), "metadata": {"duration": 1.0}}

            def analyze(path, reports_dir):
                report = {
                    "repair": {"nan_inf": False, "dc_problem": False},
                    "noise": {"noise_floor": -110.0},
                    "needs": {}, "severity": {}, "reasons": {},
                }
                report_path = Path(reports_dir) / "input_analysis.json"
                report_path.write_text(json.dumps(report), encoding="utf-8")
                return {"metadata": {"duration": 1.0}, "analysis": report, "report": str(report_path)}

            def render_stage(_name, input_path, output_path, *_args):
                shutil.copyfile(input_path, output_path)
                return {"wav": str(output_path)}

            def master(input_path, master_dir, title, _formats, _preset, _rate):
                output = Path(master_dir) / (title + ".wav")
                shutil.copyfile(input_path, output)
                return {"wav": str(output), "mp3": None, "flac": None, "master_json": None}

            def qc(_wav, _mp3, _name, _mode, _model, _offset, _duration, _preset,
                   _alignment, reports_dir, _stems, _flac, *_args):
                report = Path(reports_dir) / "process_report.md"
                report.write_text("technical smoke only\n", encoding="utf-8")
                return {"qc_data": {}, "report_path": str(report)}

            try:
                with patch.object(AudioPipeline, "_ingest", side_effect=ingest), \
                     patch.object(AudioPipeline, "_analyze", side_effect=analyze), \
                     patch("core.polish_chain.render_stage", side_effect=render_stage), \
                     patch.object(pipeline, "_master", side_effect=master), \
                     patch.object(pipeline, "_qc", side_effect=qc), \
                     patch("core.polish_policy.route_polish", side_effect=lambda _report, policy: {
                         "reference": {"enabled": False}, "no_problem": True,
                         "profile": policy["profile"],
                     }):
                    result = pipeline.process_file(
                        str(source), mode="polish", output_formats=["wav"],
                        custom_output_dir=str(root / "output"),
                    )
                self.assertEqual(store.get_job(result["job_id"])["status"], "DONE")
                project_path = Path(result["output_paths"]["reports_dir"]).parent / "project.json"
                project = json.loads(project_path.read_text(encoding="utf-8"))
                self.assertEqual(project["job_id"], result["job_id"])
                self.assertEqual(project["source_sha256"], sha256_file(source))
                self.assertEqual(project["job_spec_sha256"], project["job_spec"]["spec_hash"])
                stages = {row["stage_name"]: row for row in project["stage_records"]}
                self.assertIn("_input_manifest", stages["Ingest"]["parameters"])
                self.assertEqual(stages["Ingest"]["parameters"]["_input_manifest"]["input"]["sha256"],
                                 sha256_file(source))
                self.assertEqual(stages["Tone"]["parameters"]["_eta_execution_kind"], "executed")
                self.assertTrue(stages["Tone"]["output_hashes"]["wav"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
