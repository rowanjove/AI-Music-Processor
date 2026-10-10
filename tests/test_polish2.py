"""Polish 2.0 behavior. These tests call the shipped functions on real buffers."""
import math
import os
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analyzer import analyze_audio
from core.cache_fingerprint import fingerprints
from core.job_spec import digest, freeze, validate
from core.polish_analysis import analyze_polish_buffer
from core.polish_dsp import (
    NonFiniteAudio,
    apply_auto_eq,
    apply_declick,
    apply_declip,
    apply_denoise,
    apply_dynamic_eq,
    apply_multiband,
    apply_reference_match,
    apply_stereo,
    apply_transient,
    run_polish_buffers,
)
from core.polish_listening import EXTERNAL, polish_quality_status
from core.polish_policy import (
    ADVANCED_CONTROLS,
    AUTO_EQ_MAX_DB,
    BASIC_CONTROLS,
    EXPERT_CONTROLS,
    REFERENCE_MATCH_MAX_DB,
    STEREO_MAX_WIDTH_DELTA,
    TRANSIENT_MAX_RATIO,
    clamp_policy,
    default_policy,
    master_stage_parameters,
    polish_visible_controls,
    repair_parameters,
    resolve_polish_policy,
    route_polish,
    tone_parameters,
)
from core.polish_qc import compare_polish_buffers, trace_metrics
from core.storage import stage_cache_key
from core.ux_flow import mode_stages


SR = 48000


def _tone(seconds=1.0, freq=440.0, amp=0.2, channels=2):
    count = int(seconds * SR)
    time = np.arange(count) / SR
    mono = amp * np.sin(2.0 * math.pi * freq * time)
    if channels == 1:
        return mono.reshape(-1, 1)
    return np.column_stack((mono, mono))


def _walk_finite(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_finite(item, path + "." + str(key))
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _walk_finite(item, path + "[%d]" % index)
        return
    if isinstance(value, (bool, str)) or value is None:
        return
    if isinstance(value, (int, float, np.floating, np.integer)):
        if not math.isfinite(float(value)):
            raise AssertionError("非有限测量：" + path)
        return
    raise AssertionError("无法判断的测量：" + path + " " + type(value).__name__)


def _params():
    return dict(
        clean_preset="light", master_preset="natural", rvc_model_key="demo", pitch_shift=0,
        f0_method="harvest", index_rate=0.65, generate_midi=False, output_formats=["wav"],
        output_sample_rate=48000, bpm=120,
    )


class PolishAnalysisTests(unittest.TestCase):
    def test_groups_are_finite_on_a_tone_and_flag_bad_input(self):
        report = analyze_polish_buffer(_tone(), SR)
        for name in ("level", "dynamics", "spectrum", "noise", "repair", "stereo", "transient"):
            self.assertIn(name, report)
        for band in ("sub", "bass", "low_mid", "mid", "upper_mid", "presence", "air"):
            self.assertIn(band, report["spectrum"])
        _walk_finite(report["level"])
        _walk_finite(report["dynamics"])
        _walk_finite(report["spectrum"])
        _walk_finite({key: value for key, value in report["noise"].items()})
        _walk_finite(report["stereo"])
        _walk_finite(report["transient"])
        self.assertFalse(report["repair"]["silence"])
        self.assertFalse(report["repair"]["nan_inf"])
        self.assertFalse(report["repair"]["dc_problem"])
        self.assertFalse(report["repair"]["clipping_regions"])

        silence = analyze_polish_buffer(np.zeros((SR, 2)), SR)
        self.assertTrue(silence["repair"]["silence"])
        direct = analyze_polish_buffer(np.full((SR, 2), 0.2), SR)
        self.assertTrue(direct["repair"]["dc_problem"])
        self.assertGreater(direct["repair"]["dc_offset"], 0.005)
        clipped = _tone(amp=0.2)
        clipped[1000:1008] = 1.0
        clipping = analyze_polish_buffer(clipped, SR)
        self.assertTrue(clipping["repair"]["clipping_regions"])
        broken = _tone()
        broken[20, 0] = np.nan
        flagged = analyze_polish_buffer(broken, SR)
        self.assertTrue(flagged["repair"]["nan_inf"])

    def test_analyze_audio_keeps_the_fields_voice_already_reads(self):
        audio = _tone(seconds=3.0, amp=0.25)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tone.wav")
            sf.write(path, audio, SR, subtype="FLOAT")
            report = analyze_audio(path)
        for key in ("duration", "sample_rate", "channels", "integrated_lufs", "true_peak_dbtp", "lra", "bpm_estimate", "key_estimate"):
            self.assertIn(key, report)
        self.assertTrue(math.isfinite(float(report["integrated_lufs"])))
        self.assertTrue(math.isfinite(float(report["bpm_estimate"])))
        self.assertIn(" ", report["key_estimate"])
        self.assertEqual(report["level"]["lufs_i"], report["integrated_lufs"])
        self.assertEqual(report["level"]["true_peak"], report["true_peak_dbtp"])
        _walk_finite(report["level"])
        _walk_finite(report["spectrum"])


class PolishRouterTests(unittest.TestCase):
    def test_clean_tone_bypasses_corrective_stages(self):
        report = analyze_polish_buffer(_tone(), SR)
        decisions = route_polish(report, default_policy("balanced"))
        for name in ("declick", "declip", "denoise", "auto_eq", "dynamic_eq", "multiband", "transient", "stereo", "reference"):
            self.assertFalse(decisions[name]["enabled"], name)
        produced = run_polish_buffers(_tone(), SR, default_policy("balanced"))
        self.assertTrue(np.array_equal(produced["repaired"], produced["source"]))
        self.assertTrue(np.array_equal(produced["tone"], produced["source"]))
        self.assertTrue(np.array_equal(produced["dynamics"], produced["source"]))
        self.assertTrue(np.allclose(produced["stereo"], produced["source"]))


class PolishProcessorTests(unittest.TestCase):
    def test_click_is_repaired_and_kick_is_not(self):
        audio = _tone(amp=0.05)
        audio[10000, :] = 0.9
        kick_at = 20000
        count = int(0.04 * SR)
        time = np.arange(count) / SR
        kick = 0.8 * np.sin(2.0 * math.pi * 60.0 * time) * np.exp(-time / 0.015)
        audio[kick_at:kick_at + count, 0] += kick
        audio[kick_at:kick_at + count, 1] += kick
        bump_at = 30000
        bump = np.full(10, 0.4)
        audio[bump_at:bump_at + bump.size, :] += bump.reshape(-1, 1)
        report = analyze_polish_buffer(audio, SR)
        confidences = {item["confidence"] for item in report["repair"]["click_events"] if item["kind"] == "click"}
        self.assertIn("high", confidences)
        self.assertIn("low", confidences)
        out, info = apply_declick(audio, SR, report, enabled=True)
        self.assertLess(abs(float(out[10000, 0])), abs(float(audio[10000, 0])) * 0.5)
        self.assertTrue(np.allclose(out[kick_at:kick_at + count], audio[kick_at:kick_at + count]))
        self.assertTrue(np.allclose(out[bump_at:bump_at + bump.size], audio[bump_at:bump_at + bump.size]))
        self.assertGreaterEqual(info["reported"], 1)
        disabled, _ = apply_declick(audio, SR, report, enabled=False)
        self.assertTrue(np.array_equal(disabled, audio))

    def test_light_declip_changes_a_short_flat_top_and_severe_stays(self):
        audio = _tone(amp=0.4)
        audio[5000:5002] = 1.0
        severe = int(0.02 * SR)
        audio[8000:8000 + severe] = 1.0
        report = analyze_polish_buffer(audio, SR)
        severities = {item["severity"] for item in report["repair"]["clipping_regions"]}
        self.assertIn("light", severities)
        self.assertIn("severe", severities)
        out, info = apply_declip(audio, SR, report, enabled=True)
        self.assertLess(float(np.max(np.abs(out[5000:5002]))), 0.999)
        self.assertTrue(np.allclose(out[8000:8000 + severe], 1.0))
        self.assertGreater(info["severe"], 0)
        self.assertIn("before_peak", info)
        self.assertGreaterEqual(info["clipped_before"], info["clipped_after"])
        disabled, _ = apply_declip(audio, SR, report, enabled=False)
        self.assertTrue(np.array_equal(disabled, audio))

    def test_denoise_spares_a_cymbal_and_stays_inside_the_cap(self):
        count = SR * 2
        audio = np.zeros((count, 2))
        rng = np.random.default_rng(4)
        hiss = 0.02 * rng.normal(size=(count, 2))
        audio += hiss
        music = _tone(seconds=2.0, amp=0.25)
        audio[0:SR] += music[0:SR]
        audio[int(1.4 * SR):] += music[:count - int(1.4 * SR)]
        burst_at = int(0.2 * SR)
        burst = 0.4 * rng.normal(size=(int(0.02 * SR), 2))
        burst = burst - np.mean(burst, axis=0)
        audio[burst_at:burst_at + burst.shape[0]] += burst
        report = analyze_polish_buffer(audio, SR)
        report["needs"]["denoise"] = True
        report["noise"]["noise_floor"] = -32.0
        report["noise"]["hf_hiss"] = -28.0
        before = audio[burst_at:burst_at + burst.shape[0]]
        out, info = apply_denoise(audio, SR, report, enabled=True, strength=1.0)
        after = out[burst_at:burst_at + burst.shape[0]]
        before_rms = float(np.sqrt(np.mean(before ** 2)))
        after_rms = float(np.sqrt(np.mean(after ** 2)))
        drop = 20.0 * math.log10(max(before_rms, 1e-12) / max(after_rms, 1e-12))
        self.assertLessEqual(drop, 1.25)
        self.assertLessEqual(info.get("amount", 0.0), 3.0)
        disabled, _ = apply_denoise(audio, SR, report, enabled=False)
        self.assertTrue(np.array_equal(disabled, audio))

    def test_denoise_does_not_fill_a_silent_channel(self):
        rng = np.random.default_rng(1)
        audio = np.zeros((SR, 2))
        audio[:, 0] = 0.03 * rng.normal(size=SR)
        report = analyze_polish_buffer(audio, SR)
        report["needs"]["denoise"] = True
        report["noise"]["noise_floor"] = -30.0
        report["noise"]["hf_hiss"] = -25.0
        out, _info = apply_denoise(audio, SR, report, enabled=True, strength=1.0)
        self.assertLess(float(np.sqrt(np.mean(out[:, 1] ** 2))), 1e-8)

    def test_eq_reference_multiband_transient_and_stereo_stay_inside_caps(self):
        sine = _tone(freq=280.0, amp=0.2)
        report = analyze_polish_buffer(sine, SR)
        report["needs"]["auto_eq"] = True
        report["spectrum"]["low_mid"] = report["spectrum"]["mid"] + 30.0
        eq, eq_info = apply_auto_eq(sine, SR, report, enabled=True, strength=1.0)
        for item in eq_info.get("filters") or []:
            self.assertLessEqual(abs(item["gain_db"]), AUTO_EQ_MAX_DB)
        before = float(np.sqrt(np.mean(sine ** 2)))
        after = float(np.sqrt(np.mean(eq ** 2)))
        change = abs(20.0 * math.log10(max(after, 1e-12) / max(before, 1e-12)))
        self.assertLessEqual(change, AUTO_EQ_MAX_DB + 0.2)
        same, same_info = apply_reference_match(sine, SR, report, None, None, enabled=True)
        self.assertTrue(same_info["bypassed"])
        self.assertTrue(np.array_equal(same, sine))
        bright = _tone(freq=8000.0, amp=0.2)
        matched, match_info = apply_reference_match(sine, SR, report, bright, SR, enabled=True, strength=1.0)
        for item in match_info.get("filters") or []:
            self.assertLessEqual(abs(item["gain_db"]), REFERENCE_MATCH_MAX_DB)
        self.assertTrue(np.isfinite(matched).all())
        quiet = _tone(freq=440.0, amp=0.05)
        loud = _tone(freq=440.0, amp=0.5)
        same_shape, same_level = apply_reference_match(quiet, SR, analyze_polish_buffer(quiet, SR), loud, SR, enabled=True, strength=1.0)
        self.assertTrue(same_level["bypassed"])
        self.assertTrue(np.allclose(same_shape, quiet))

        pulses = np.zeros((SR * 2, 2))
        for start in range(0, pulses.shape[0], SR // 4):
            pulses[start:start + 200] = 0.8
        dynamic = analyze_polish_buffer(pulses, SR)
        dynamic["needs"]["multiband"] = True
        dynamic["needs"]["dynamic_eq"] = True
        dynamic["needs"]["transient"] = True
        dynamic["spectrum"]["low_mid"] = dynamic["spectrum"].get("mid", -20) + 8
        dynamic["transient"]["attack_energy"] = -6.0
        dynamic["transient"]["sustain_energy"] = -30.0
        compressed, comp_info = apply_multiband(pulses, SR, dynamic, enabled=True, strength=1.0)
        self.assertGreater(comp_info.get("amount", 0.0), 0.2)
        self.assertLessEqual(comp_info.get("amount", 0.0), 3.0 + 1e-6)
        self.assertTrue(np.isfinite(compressed).all())
        self.assertLess(float(np.max(np.abs(compressed[2000:4000]))), 1e-4)
        off, _ = apply_multiband(pulses, SR, dynamic, enabled=False)
        self.assertTrue(np.array_equal(off, pulses))
        shaped, shape_info = apply_transient(pulses, SR, dynamic, enabled=True, strength=1.0)
        self.assertLessEqual(abs(shape_info.get("amount", 0.0)), TRANSIENT_MAX_RATIO + 1e-9)
        delta = np.abs(shaped - pulses)
        limit = TRANSIENT_MAX_RATIO * np.maximum(np.abs(pulses), 1e-8) + 1e-5
        self.assertTrue(np.all(delta <= limit))
        dyn, dyn_info = apply_dynamic_eq(pulses, SR, dynamic, enabled=True, strength=1.0)
        for band in dyn_info.get("bands") or []:
            self.assertLessEqual(abs(band["gain_db"]), 3.0 + 1e-6)
        self.assertTrue(np.isfinite(dyn).all())

        wide = np.zeros((SR, 2))
        time = np.arange(SR) / SR
        bass = 0.4 * np.sin(2.0 * math.pi * 70.0 * time)
        wide[:, 0] = bass
        wide[:, 1] = -bass
        stereo_report = analyze_polish_buffer(wide, SR)
        stereo_report["needs"]["stereo"] = True
        stereo_report["stereo"]["low_frequency_correlation"] = -0.5
        narrowed, stereo_info = apply_stereo(wide, SR, stereo_report, enabled=True, strength=1.0)
        self.assertLessEqual(abs(stereo_info.get("amount", 0.0)), STEREO_MAX_WIDTH_DELTA + 1e-9)
        before_side = float(np.sqrt(np.mean((wide[:, 0] - wide[:, 1]) ** 2)))
        after_side = float(np.sqrt(np.mean((narrowed[:, 0] - narrowed[:, 1]) ** 2)))
        self.assertLessEqual(abs(after_side / before_side - 1.0), STEREO_MAX_WIDTH_DELTA + 0.02)
        disabled, _ = apply_stereo(wide, SR, stereo_report, enabled=False)
        self.assertTrue(np.array_equal(disabled, wide))

    def test_nan_stops_instead_of_passing_through(self):
        audio = _tone()
        audio[8, 1] = np.inf
        report = analyze_polish_buffer(_tone(), SR)
        with self.assertRaises(NonFiniteAudio):
            apply_auto_eq(audio, SR, report, enabled=False)
        with self.assertRaises(NonFiniteAudio):
            run_polish_buffers(audio, SR, default_policy("balanced"))


class PolishJobSpecTests(unittest.TestCase):
    def test_old_snapshot_validates_and_new_fields_do_not_bust_repair(self):
        spec = freeze(_params(), "polish")
        self.assertEqual(spec["schema_version"], 1)
        for key in ("analysis_policy", "repair", "denoise", "eq", "dynamic_eq", "reference", "multiband", "transient", "stereo", "master"):
            self.assertIn(key, spec["polish"])
        old = {key: value for key, value in spec.items() if key != "polish"}
        old["spec_hash"] = digest(old)
        validate(old, "polish")
        produced = run_polish_buffers(_tone(), SR, resolve_polish_policy(old))
        self.assertTrue(np.isfinite(produced["stereo"]).all())
        self.assertGreater(float(np.max(np.abs(produced["stereo"]))), 1e-4)

        policy = default_policy("balanced")
        other = default_policy("balanced")
        other["eq"]["enabled"] = False
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            first = os.path.join(folder, "a.wav")
            second = os.path.join(folder, "b.wav")
            sf.write(first, _tone(freq=1000.0), SR, subtype="FLOAT")
            sf.write(second, _tone(freq=2000.0, amp=0.4), SR, subtype="FLOAT")
            from core.polish_policy import reference_identity
            left = reference_identity(first)
            right = reference_identity(second)
            self.assertNotEqual(left["sha256"], right["sha256"])
            repair = repair_parameters(policy)
            self.assertNotIn("sha256", repair)
            self.assertNotIn(left["sha256"], str(repair))
            tone_left = tone_parameters(policy, left)
            tone_right = tone_parameters(other, right)
            self.assertEqual(repair_parameters(policy), repair_parameters(other))
            self.assertIn(left["sha256"], tone_left["reference"]["sha256"])
            resolved = str(Path(first).resolve())
            repair_files = fingerprints("Repair", repair)
            tone_files = fingerprints("Tone", tone_left)
            self.assertNotIn(resolved, repair_files)
            self.assertIn(resolved, tone_files)
        from core.cache_fingerprint import enrich
        repair_key = stage_cache_key("Repair", {"input": "same"}, enrich("Repair", repair))
        other_key = stage_cache_key("Repair", {"input": "same"}, enrich("Repair", repair_parameters(other)))
        self.assertEqual(repair_key, other_key)
        self.assertNotEqual(
            stage_cache_key("Tone", {"input": "same"}, enrich("Tone", tone_left)),
            stage_cache_key("Tone", {"input": "same"}, enrich("Tone", tone_right)),
        )
        master = master_stage_parameters({"name": "natural", "target_lufs": -11.0, "true_peak": -1.0})
        for forbidden in ("eq", "dynamic_eq", "multiband", "stereo", "denoise", "declick"):
            self.assertNotIn(forbidden, master)

    def test_frozen_reference_off_stays_a_passthrough(self):
        source = _tone(freq=440.0, amp=0.2)
        with tempfile.TemporaryDirectory() as folder:
            ref = os.path.join(folder, "reference.wav")
            sf.write(ref, _tone(freq=8000.0, amp=0.25), SR, subtype="FLOAT")
            disabled = dict(_params(), polish_reference=ref, polish_options={"reference": {"enabled": False}})
            spec = freeze(disabled, "polish")
            stored = spec["polish"]["reference"]
            self.assertFalse(stored["enabled"])
            self.assertTrue(stored["sha256"])
            self.assertGreater(float(stored["duration"]), 0.0)
            self.assertTrue(stored["analysis_fingerprint"])
            self.assertNotIn(stored["sha256"], str(repair_parameters(spec["polish"])))
            tone = tone_parameters(spec["polish"])
            self.assertFalse(tone["reference"]["enabled"])
            self.assertIsNone(tone["reference"]["sha256"])
            self.assertIsNone(tone["reference"]["duration"])
            self.assertIsNone(tone["reference"]["analysis_fingerprint"])
            self.assertIsNone(tone["reference"]["path"])
            from pathlib import Path
            resolved = str(Path(ref).resolve())
            self.assertNotIn(resolved, fingerprints("Repair", repair_parameters(spec["polish"])))
            self.assertNotIn(resolved, fingerprints("Tone", tone))
            report = analyze_polish_buffer(source, SR)
            decisions = route_polish(report, spec["polish"])
            self.assertFalse(decisions["reference"]["enabled"])
            self.assertEqual(decisions["reference"]["reason"], "已关闭")
            reference_audio, reference_rate = sf.read(ref, always_2d=True, dtype="float32")
            produced = run_polish_buffers(source, SR, spec["polish"], reference_audio, reference_rate)
            self.assertTrue(np.array_equal(produced["tone"], produced["source"]))
            enabled = freeze(dict(_params(), polish_reference=ref), "polish")
            self.assertTrue(enabled["polish"]["reference"]["enabled"])
            self.assertTrue(route_polish(report, enabled["polish"])["reference"]["enabled"])
            changed = run_polish_buffers(source, SR, enabled["polish"], reference_audio, reference_rate)
            self.assertFalse(np.array_equal(changed["tone"], changed["source"]))

    def test_reference_read_failure_does_not_reuse_a_match_cache(self):
        failed = default_policy("balanced")
        fingerprint = {"path": "missing.wav", "sha256": "abc", "duration": 1.0, "analysis_fingerprint": "fp", "enabled": True}
        failed["reference"].update(fingerprint)
        failed["reference"]["enabled"] = False
        failed["reference"]["read_error"] = "参考曲读取失败，已跳过"
        decisions = route_polish(analyze_polish_buffer(_tone(), SR), failed)
        self.assertFalse(decisions["reference"]["enabled"])
        self.assertEqual(decisions["reference"]["reason"], "参考曲读取失败，已跳过")
        from core.cache_fingerprint import enrich
        skipped = tone_parameters(failed, fingerprint)
        opened = default_policy("balanced")
        opened["reference"].update(fingerprint)
        matched = tone_parameters(opened, fingerprint)
        self.assertFalse(skipped["reference"]["enabled"])
        self.assertTrue(matched["reference"]["enabled"])
        self.assertIsNone(skipped["reference"]["sha256"])
        self.assertNotIn("read_error", skipped["reference"])
        self.assertNotEqual(
            stage_cache_key("Tone", {"input": "same"}, enrich("Tone", skipped)),
            stage_cache_key("Tone", {"input": "same"}, enrich("Tone", matched)),
        )

    def test_dc_is_repaired_and_expert_caps_are_real(self):
        offset = _tone(amp=0.2) + 0.02
        report = analyze_polish_buffer(offset, SR)
        decisions = route_polish(report, default_policy("balanced"))
        self.assertTrue(decisions["dc"]["enabled"])
        self.assertFalse(decisions["no_problem"])
        produced = run_polish_buffers(offset, SR, default_policy("balanced"))
        self.assertLess(abs(float(produced["repaired"].mean())), 1e-4)

        policy = default_policy("balanced")
        policy["expert"] = {
            "max correction": "1", "correlation guard": "0.2", "noise threshold": "-30",
            "Q": "1.5", "debug trace": "on",
        }
        clamped = clamp_policy(policy)
        self.assertEqual(clamped["eq"]["max_db"], 1.0)
        self.assertEqual(clamped["reference"]["max_db"], 1.0)
        self.assertEqual(clamped["multiband"]["max_gr_db"], 1.0)
        self.assertEqual(clamped["stereo"]["correlation_guard"], 0.2)
        self.assertEqual(clamped["denoise"]["noise_floor_threshold"], -30.0)
        self.assertEqual(clamped["dynamic_eq"]["q"], 1.5)
        self.assertEqual(clamped["expert"]["debug trace"], "on")
        self.assertEqual(clamp_policy(clamped), clamped)
        four = default_policy("balanced")
        four["expert"] = {"max correction": "4"}
        self.assertEqual(clamp_policy(four)["eq"]["max_db"], 3.0)
        self.assertEqual(clamp_policy(four)["reference"]["max_db"], 3.0)
        bad = default_policy("balanced")
        bad["expert"] = {"max correction": "nope"}
        with self.assertRaises(ValueError):
            clamp_policy(bad)
        noisy = analyze_polish_buffer(_tone(), SR)
        noisy["needs"]["denoise"] = True
        noisy["severity"]["denoise"] = 1.0
        noisy["noise"]["noise_floor"] = -40.0
        blocked = route_polish(noisy, clamped)
        self.assertFalse(blocked["denoise"]["enabled"])
        self.assertEqual(blocked["denoise"]["reason"], "噪声低于门限")
        spec = freeze(dict(_params(), polish_options={"expert": {"max correction": "1"}}), "polish")
        validate(spec, "polish")
        self.assertEqual(spec["polish"]["eq"]["max_db"], 1.0)


class PolishQcTests(unittest.TestCase):
    def test_before_after_findings_and_a_benign_pass(self):
        before = _tone(seconds=2.0, amp=0.2) + _tone(seconds=2.0, freq=10000.0, amp=0.12)
        self.assertEqual(compare_polish_buffers(before, before, SR), [])
        from scipy.signal import butter, sosfilt
        low = sosfilt(butter(6, 800.0, btype="lowpass", fs=SR, output="sos"), before, axis=0)
        kinds = {item["kind"] for item in compare_polish_buffers(before, low, SR)}
        self.assertIn("high_frequency_loss", kinds)
        self.assertTrue(any(item["cause"] == "denoise" for item in compare_polish_buffers(before, low, SR)))
        flat = np.ones_like(before) * 0.2
        gate = np.sin(2 * math.pi * 2 * np.arange(before.shape[0]) / SR) > 0
        bursts = before * (0.2 + 0.8 * gate)[:, None]
        collapse = compare_polish_buffers(bursts, flat, SR)
        self.assertTrue(any(item["kind"] == "dynamic_range_collapse" and item["cause"] == "compression" for item in collapse))
        flipped = before.copy()
        flipped[:, 1] *= -1
        phase = compare_polish_buffers(before, flipped, SR)
        self.assertTrue(any(item["kind"] == "phase_degradation" and item["cause"] == "stereo" for item in phase))
        clipped = np.clip(before * 8.0, -1.0, 1.0)
        clips = compare_polish_buffers(before, clipped, SR)
        self.assertTrue(any(item["kind"] == "clipping" for item in clips))
        pure = _tone(seconds=2.0, freq=440.0, amp=0.2)
        harmonic = pure + _tone(seconds=2.0, freq=1000.0, amp=0.02)
        self.assertFalse(any(item["kind"] == "high_frequency_loss" for item in compare_polish_buffers(pure, harmonic, SR)))

    def test_44100_master_is_compared_with_the_48000_working_file(self):
        from core.polish_qc import comparison_from_files
        tone = _tone(seconds=0.4, freq=440.0, amp=0.2)
        with tempfile.TemporaryDirectory() as folder:
            before = os.path.join(folder, "source.wav")
            after = os.path.join(folder, "master.wav")
            sf.write(before, tone, 48000, subtype="FLOAT")
            sf.write(after, tone, 44100, subtype="FLOAT")
            result = comparison_from_files(before, after)
        self.assertIsInstance(result["findings"], list)
        self.assertIn("high_frequency_loss", result)

    def test_trace_metrics_record_present_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "source.wav")
            sf.write(path, _tone(amp=0.2), SR, subtype="FLOAT")
            rows = {item["role"]: item for item in trace_metrics(folder, path)}
        self.assertTrue(rows["source"]["present"])
        self.assertGreater(rows["master"]["peak"], 0.1)
        self.assertFalse(rows["tone"]["present"])


class PolishSurfaceTests(unittest.TestCase):
    def test_basic_hides_advanced_and_expert_controls(self):
        basic = polish_visible_controls("basic")
        self.assertEqual(basic, set(BASIC_CONTROLS))
        self.assertEqual(basic, {"精修强度", "参考曲", "开始"})
        for name in ADVANCED_CONTROLS + EXPERT_CONTROLS:
            self.assertNotIn(name, basic)
        advanced = polish_visible_controls("高级")
        for name in ADVANCED_CONTROLS:
            self.assertIn(name, advanced)
        for name in EXPERT_CONTROLS:
            self.assertNotIn(name, advanced)
        expert = polish_visible_controls("expert")
        for name in EXPERT_CONTROLS:
            self.assertIn(name, expert)

    def test_withdrawn_app_shows_basic_polish_controls_only(self):
        import tempfile
        import tkinter as tk
        from unittest.mock import patch
        import AI_Music_Processor as ui
        from core import config

        def stop(app):
            try:
                for item in app.tk.splitlist(app.tk.call("after", "info")):
                    app.after_cancel(item)
            except tk.TclError:
                pass
            try:
                app.pipeline.storage.close()
            except Exception:
                pass
            app.destroy()

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config, "CONFIG_DIR", folder), patch.object(ui, "CONFIG_DIR", folder):
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.mode.set("polish")
                    app.polish_tier.set("基础")
                    app.polish_strength.set("推荐")
                    app._apply_polish_visibility()
                    app.update()
                    self.assertEqual(app.polish_row.winfo_manager(), "pack")
                    self.assertEqual(app.polish_advanced.winfo_manager(), "")
                    self.assertEqual(app.polish_expert_frame.winfo_manager(), "")
                    self.assertEqual(app.model_label.winfo_manager(), "")
                    self.assertEqual(app.clean_widget.winfo_manager(), "")
                    self.assertEqual(app.master_widget.winfo_manager(), "")
                    self.assertEqual(app.voice_advanced.winfo_manager(), "")
                    self.assertNotIn("polish_options", app.parameters())
                    app.polish_tier.set("高级")
                    app.update()
                    self.assertEqual(app.polish_advanced.winfo_manager(), "pack")
                    self.assertEqual(app.polish_expert_frame.winfo_manager(), "")
                    self.assertEqual(app.master_widget.winfo_manager(), "pack")
                    self.assertEqual(app.nametowidget(app.master_widget.pack_info()["in"]), app.polish_level_row)
                    app.polish_tier.set("专家")
                    app.update()
                    self.assertEqual(app.polish_expert_frame.winfo_manager(), "pack")
                    app.mode.set("voice_conversion")
                    app.refresh_mode_flow()
                    app.update()
                    self.assertEqual(app.polish_row.winfo_manager(), "")
                    self.assertEqual(app.model_label.winfo_manager(), "pack")
                    self.assertEqual(app.clean_widget.winfo_manager(), "pack")
                    self.assertEqual(app.model_label.cget("text"), "模型")
                finally:
                    stop(app)

    def test_voice_and_transcribe_do_not_gain_polish_stages(self):
        self.assertEqual(mode_stages("polish"), ["Ingest", "Analyze", "Repair", "Tone", "Dynamics", "Stereo", "Master", "QC"])
        for mode in ("voice_conversion", "transcribe"):
            stages = mode_stages(mode)
            self.assertIn("Separate", stages)
            self.assertIn("Clean", stages)
            for name in ("Repair", "Tone", "Dynamics", "Stereo"):
                self.assertNotIn(name, stages)

    def test_listening_status_stays_external_without_both_records(self):
        self.assertEqual(polish_quality_status(None, None), EXTERNAL)
        self.assertEqual(polish_quality_status({"inference_ran": True, "artifact": "master.wav"}, None), EXTERNAL)
        self.assertEqual(polish_quality_status(None, {"listener": "a", "date": "2026-10-07", "notes": "听过"}), EXTERNAL)
        accepted = polish_quality_status(
            {"inference_ran": True, "artifact": "master.wav"},
            {"listener": "a", "date": "2026-10-07", "notes": "听过"},
        )
        self.assertEqual(accepted, "QUALITY_ACCEPTED")
        from core.polish_listening import ab_manifest
        manifest = ab_manifest({"source": "missing.wav"}, {"inference_ran": True, "artifact": "master.wav"}, None)
        self.assertEqual(manifest["quality_acceptance"], EXTERNAL)
        self.assertEqual(manifest["listening_quality"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
