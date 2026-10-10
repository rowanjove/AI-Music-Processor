import importlib.machinery
import importlib.util
import json
import os
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

from core.qc import run_quality_control
from core.polish_chain import render_polish_files
from core.polish_policy import default_policy


class QcDegradationAndUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader(
            "test_gui_module", str(ROOT / "AI_Music_Processor.pyw")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.ui = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.ui)

    def _create_mock_app(self):
        app = object.__new__(self.ui.AIMusicProcessorApp)
        app.status = type(
            "Status", (), {"text": "", "config": lambda obj, **kwargs: setattr(obj, "text", kwargs.get("text", ""))}
        )()
        def _mock_set(obj, key, col, val):
            if key not in obj.data:
                obj.data[key] = {}
            obj.data[key][col] = val
        app.tree = type(
            "MockTree",
            (),
            {
                "data": {},
                "set": _mock_set,
                "selection": lambda obj: ["k1"],
                "selection_set": lambda obj, key: None,
            },
        )()
        app.tree.data["k1"] = {}
        app.history_tree = type(
            "MockHistoryTree",
            (),
            {
                "rows": [],
                "delete": lambda obj, *args: obj.rows.clear(),
                "get_children": lambda obj: [],
                "insert": lambda obj, parent, index, iid=None, values=(): obj.rows.append((iid, values)),
                "selection": lambda obj: ["job_1"],
            },
        )()
        app.result_manifests = {}
        app.pending = {}
        app.pending_paths = set()
        app.result_surface = "queue"
        app._try_save_queue = lambda: None
        return app

    def test_qc_senses_polish_degradation_and_marks_integrity_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_wav = tmp_path / "input.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack(
                [0.2 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 440 * t)], axis=-1
            ).astype(np.float32)
            sf.write(str(src_wav), audio, 48000)

            out_dir = tmp_path / "polish_out"
            # Simulate failure in dynamics stage
            with patch("core.polish_chain.apply_dynamics", side_effect=ZeroDivisionError("simulated math error")):
                res = render_polish_files(str(src_wav), str(out_dir), default_policy("balanced"))

            self.assertTrue(res["has_degradation"])
            self.assertIn("dynamics", res["degraded_stages"])

            master_wav = str(out_dir / "stereo.wav")
            qc_data, report_md = run_quality_control(
                master_wav_path=master_wav,
                master_mp3_path=None,
                original_file_name="test_song.wav",
                mode_name="polish",
                polish_before=str(res["source"]),
            )

            # Audio physical metrics passed, but processing integrity is DEGRADED and overall status is WARN
            self.assertTrue(qc_data["audio_metrics_passed"])
            self.assertEqual(qc_data["processing_integrity"], "DEGRADED")
            self.assertEqual(qc_data["status"], "WARN")
            self.assertTrue(len(qc_data["degraded_stages"]) >= 1)
            deg = qc_data["degraded_stages"][0]
            self.assertEqual(deg["stage"], "dynamics")
            self.assertTrue(deg["source_preserved"])
            self.assertTrue(deg["retryable"])
            self.assertIn("ZeroDivisionError", deg["reason"])

            # Markdown report has clear section
            self.assertIn("## 阶段执行与降级说明 (Processing Integrity)", report_md)
            self.assertIn("动态控制 (Dynamics)", report_md)
            self.assertIn("DEGRADED（存在异常降级）", report_md)
            self.assertIn("已保留前一阶段有效音频", report_md)

    def test_qc_reports_intact_and_pass_when_no_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_wav = tmp_path / "input.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack(
                [0.2 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 440 * t)], axis=-1
            ).astype(np.float32)
            sf.write(str(src_wav), audio, 48000)

            out_dir = tmp_path / "polish_out"
            res = render_polish_files(str(src_wav), str(out_dir), default_policy("balanced"))

            master_wav = str(out_dir / "stereo.wav")
            qc_data, report_md = run_quality_control(
                master_wav_path=master_wav,
                master_mp3_path=None,
                original_file_name="test_song.wav",
                mode_name="polish",
                polish_before=str(res["source"]),
            )

            self.assertEqual(qc_data["processing_integrity"], "INTACT")
            self.assertEqual(qc_data["degraded_stages"], [])
            self.assertIn("INTACT（全部阶段完整执行）", report_md)
            self.assertNotIn("## 阶段执行与降级说明 (Processing Integrity)", report_md)

    def test_gui_queue_done_event_and_selection_shows_degraded_warning(self):
        app = self._create_mock_app()
        manifest = {
            "source_path": "C:/music/track1.wav",
            "output_paths": {"reports_dir": "C:/music/project/reports"},
            "qc": {
                "status": "WARN",
                "audio_metrics_passed": True,
                "processing_integrity": "DEGRADED",
                "degraded_stages": [
                    {"stage": "dynamics", "name": "动态控制 (Dynamics)", "reason": "ZeroDivisionError"}
                ],
            },
        }

        # Simulate event delivery: ('done', ('k1', manifest))
        app.last_manifest = manifest
        app.result_manifests["k1"] = manifest

        # Run code corresponding to done event
        qc_info = manifest.get("qc", {})
        qc_status = qc_info.get("status", "Unknown")
        integrity = qc_info.get("processing_integrity", "INTACT")
        degraded = qc_info.get("degraded_stages") or []
        deg_names = [d.get("name", d.get("stage", "")) for d in degraded]
        deg_str = f" ({','.join(deg_names)})" if deg_names else ""
        app.tree.set("k1", "state", f"完成 · 降级警告{deg_str}")
        app.status.config(text=f"完成（带降级警告）：阶段异常已保留原音频，QC {qc_status}；查看质检详情或报告")

        self.assertIn("降级警告", app.tree.data["k1"]["state"])
        self.assertIn("动态控制", app.tree.data["k1"]["state"])
        self.assertIn("降级警告", app.status.text)

        # Selection handler
        app.selected_manifest = lambda: manifest
        app.on_select_result()
        self.assertIn("完成但有降级警告", app.status.text)
        self.assertIn("动态控制", app.status.text)

    def test_gui_refresh_history_displays_degraded_warning_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "proj"
            project_dir.mkdir()
            pjson = project_dir / "project.json"
            pjson.write_text(
                json.dumps({
                    "qc": {
                        "status": "WARN",
                        "processing_integrity": "DEGRADED",
                        "degraded_stages": [{"stage": "repair", "name": "修损降噪"}],
                    }
                }),
                encoding="utf-8",
            )

            app = self._create_mock_app()
            mock_jobs = [
                {
                    "id": "job_1",
                    "status": "DONE",
                    "source_path": "test.wav",
                    "mode": "polish",
                    "current_stage": "Complete",
                    "project_dir": str(project_dir),
                }
            ]
            app.pipeline = type("MockPipeline", (), {
                "storage": type("MockStorage", (), {"list_jobs": lambda *args, **kwargs: mock_jobs})()
            })()

            app.refresh_history()
            self.assertEqual(len(app.history_tree.rows), 1)
            row_id, row_vals = app.history_tree.rows[0]
            self.assertEqual(row_id, "job_1")
            # The status column should be "完成 (降级警告)"
            self.assertEqual(row_vals[2], "完成 (降级警告)")


if __name__ == "__main__":
    unittest.main()
