import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.qc import run_quality_control


class QcTruthfulnessTests(unittest.TestCase):
    def _create_stems(self, folder, audio, sr=48000):
        stem_paths = []
        for name in ("01_vocal_original.wav", "02_vocal_cleaned.wav", "03_vocal_aligned.wav", "04_instrumental.wav"):
            p = folder / name
            sf.write(str(p), audio, sr)
            stem_paths.append(str(p))
        return stem_paths

    def test_alignment_reports_offset_residual_and_max_local_in_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            master_wav = tmp_path / "master.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 440 * t)], axis=-1).astype(np.float32)
            sf.write(str(master_wav), audio, 48000)
            stem_paths = self._create_stems(tmp_path, audio)

            # Alignment with 15.2ms residual and 18ms max local
            align_file = tmp_path / "align.json"
            align_file.write_text(
                json.dumps({
                    "offset_ms": 42.5,
                    "residual_ms": 15.2,
                    "max_local_residual_ms": 18.0,
                    "correlation": 0.88,
                    "verified": True,
                }),
                encoding="utf-8",
            )

            data, report = run_quality_control(
                master_wav_path=str(master_wav),
                master_mp3_path=None,
                original_file_name="song.wav",
                mode_name="voice_conversion",
                sync_offset_ms=42.5,
                alignment_path=str(align_file),
                stem_paths=stem_paths,
            )

            # Data fields
            self.assertEqual(data["alignment_offset_ms"], 42.5)
            self.assertEqual(data["residual_offset_ms"], 15.2)
            self.assertEqual(data["max_local_residual_ms"], 18.0)
            self.assertTrue(data["alignment_verified"])

            # Report distinguishes them clearly in ms
            self.assertIn("对齐整体修正量：42.50ms", report)
            self.assertIn("对齐残余偏移：15.20ms", report)
            self.assertIn("对齐最大局部误差：18.00ms", report)
            self.assertIn("已通过置信校验（相关系数 0.880）", report)
            # Residual 15.2ms generates issue for 10-30ms range
            self.assertIn("未达到小于10ms的目标", report)
            self.assertEqual(data["status"], "WARN")

    def test_alignment_large_residual_fails_qc(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            master_wav = tmp_path / "master.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 440 * t)], axis=-1).astype(np.float32)
            sf.write(str(master_wav), audio, 48000)
            stem_paths = self._create_stems(tmp_path, audio)

            align_file = tmp_path / "align.json"
            align_file.write_text(
                json.dumps({
                    "offset_ms": 100.0,
                    "residual_ms": 35.0,
                    "max_local_residual_ms": 40.0,
                    "correlation": 0.5,
                    "verified": False,
                }),
                encoding="utf-8",
            )

            data, report = run_quality_control(
                master_wav_path=str(master_wav),
                master_mp3_path=None,
                original_file_name="song.wav",
                mode_name="voice_conversion",
                alignment_path=str(align_file),
                stem_paths=stem_paths,
            )

            self.assertEqual(data["status"], "FAIL")
            self.assertIn("对齐后残余误差超过 30ms（实测 40.00ms）", data["failures"])
            self.assertIn("对齐相关性不足，不能确认同步；需试听", data["issues"])

    def test_multidimensional_quality_facts_and_listening_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            master_wav = tmp_path / "master.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 440 * t)], axis=-1).astype(np.float32)
            sf.write(str(master_wav), audio, 48000)

            data, report = run_quality_control(
                master_wav_path=str(master_wav),
                master_mp3_path=None,
                original_file_name="clean.wav",
                mode_name="polish",
            )

            # Facts are explicitly distinguished
            self.assertTrue(data["audio_file_valid"])
            self.assertTrue(data["audio_metrics_passed"])
            self.assertTrue(data["stage_execution_intact"])
            self.assertEqual(data["processing_integrity"], "INTACT")
            self.assertEqual(data["quality_acceptance"], "READY_FOR_EXTERNAL_VALIDATION")
            self.assertEqual(data["listening_quality"], "not_assessed")

            # Report explicitly documents external validation requirement and non-assessed listening
            self.assertIn("音频文件有效性：**已完整解码验证（有限浮点数值）**", report)
            self.assertIn("客观数值指标：**通过**", report)
            self.assertIn("阶段执行完整度：INTACT（全部阶段完整执行）", report)
            self.assertIn("人工音质听审：**未评估 (not_assessed - 客观指标通过不代表免审，音色/伪影需人工试听)**", report)
            self.assertIn("质量验收状态：**待外部/人工验收 (READY_FOR_EXTERNAL_VALIDATION)**", report)

    def test_truncated_export_fails_closed_without_container_only_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            master_wav = tmp_path / "master.wav"
            t = np.linspace(0, 1, 48000, endpoint=False)
            audio = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 440 * t)], axis=-1).astype(np.float32)
            sf.write(str(master_wav), audio, 48000)

            # Corrupt export file (e.g. truncated MP3 or invalid payload)
            corrupt_export = tmp_path / "corrupt_export.mp3"
            corrupt_export.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00corrupted payload not decodable")

            with self.assertRaises((RuntimeError, OSError, ValueError)):
                run_quality_control(
                    master_wav_path=str(master_wav),
                    master_mp3_path=str(corrupt_export),
                    original_file_name="clean.wav",
                    mode_name="polish",
                )


if __name__ == "__main__":
    unittest.main()
