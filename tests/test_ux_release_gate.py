import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pipeline import AudioPipeline
from core.pipeline_support import _output_paths
from core.release_gate import (
    EXTERNAL,
    acceptance_matrix, compare_golden, hashes_match, listening_record,
    local_release_gate, package_hashes, workspace_golden_status,
)
from core.storage import canonical_json, sha256_file
from core.job_spec import digest


FIXTURE = ROOT / "tests" / "fixtures" / "voice_chain_before.json"


def _put_audio(path, amplitude=.1, duration=1.0):
    rate = 48000
    time = np.arange(round(rate * duration), dtype=np.float32) / rate
    data = amplitude * np.sin(2 * np.pi * 440 * time)
    sf.write(path, np.column_stack((data, data)), rate, subtype="FLOAT")
    return str(path)


def _file_ref(path):
    return {"_stage_input_file": True, "path": str(Path(path).resolve()), "sha256": sha256_file(path)}


def _stage(job_id, name, inputs, outputs, *, status="DONE", execution="executed"):
    input_manifest = AudioPipeline._input_manifest(inputs)
    hashes = {role: sha256_file(path) for role, path in _output_paths(outputs).items()}
    return {
        "job_id": job_id,
        "stage_name": name,
        "status": status,
        "input_hash": canonical_json(AudioPipeline._input_hash_data(input_manifest)),
        "outputs": outputs,
        "output_hashes": hashes,
        "parameters": {"_input_manifest": input_manifest, "_eta_execution_kind": execution},
    }


def _make_evidence(root, *, result_amplitude=.1):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))["baseline_project"]
    baseline_project = json.loads((ROOT / baseline["path"]).read_text(encoding="utf-8"))
    source = root / "source.wav"
    shutil.copyfile(baseline_project["source_path"], source)
    normalized = Path(_put_audio(root / "01_standard.wav", .1))
    vocal = Path(_put_audio(root / "vocal.wav", .12))
    instrumental = Path(_put_audio(root / "instrumental.wav", .15))
    cleaned = Path(_put_audio(root / "cleaned.wav", .12))
    rvc = Path(_put_audio(root / "rvc.wav", .11))
    aligned = Path(_put_audio(root / "aligned.wav", .11))
    mixed = Path(_put_audio(root / "mixed.wav", result_amplitude))
    (root / "alignment.json").write_text("{}", encoding="utf-8")
    job_id = "job_current_001"
    stages = [
        _stage(job_id, "Ingest", {"input": str(source)}, {"standard_wav": str(normalized)}),
        _stage(job_id, "Separate", {"input": str(normalized)},
               {"vocal": str(vocal), "instrumental": str(instrumental)}),
        _stage(job_id, "Clean", {"input": str(vocal)}, {"cleaned": str(cleaned)}),
        _stage(job_id, "RVC", {"input": str(cleaned)}, {"rvc": str(rvc)}),
        _stage(job_id, "Align", {"reference": str(vocal), "rvc": str(rvc)},
               {"aligned": str(aligned), "alignment_json": str(root / "alignment.json")}),
        _stage(job_id, "Mix", {"vocal": str(aligned), "instrumental": str(instrumental)},
               {"mixed": str(mixed)}),
    ]
    spec = copy.deepcopy(baseline_project["job_spec"])
    project = {
        "job_id": job_id,
        "status": "DONE",
        "mode": "voice_conversion",
        "source_path": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "job_spec": spec,
        "job_spec_sha256": digest(spec),
        "stage_records": stages,
    }
    project_path = root / "project.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    artifact = {
        "schema_version": 1,
        "kind": "workspace_golden_rerun",
        "project_manifest": {"path": str(project_path.resolve()), "sha256": sha256_file(project_path)},
        "job_id": job_id,
        "result_role": "Mix.mixed",
    }
    artifact_path = root / "rerun.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["points"]["mix_output"].update(
        sample_peak=.1, clipped_samples=0, duration_seconds=1.0,
    )
    fixture_path = root / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    return project_path, artifact_path, fixture_path, project, artifact


def _rewrite_project(project_path, artifact_path, artifact, project):
    project_path.write_text(json.dumps(project, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    artifact["project_manifest"]["sha256"] = sha256_file(project_path)
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")


class ReleaseGateTests(unittest.TestCase):
    def test_local_gate_lists_external_evidence_without_claiming_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "piece.bin"
            path.write_bytes(b"pkg")
            listed = package_hashes(folder, ["piece.bin"])
            self.assertTrue(hashes_match(listed, package_hashes(folder, ["piece.bin"])))
            path.write_bytes(b"changed")
            self.assertFalse(hashes_match(listed, package_hashes(folder, ["piece.bin"])))
        self.assertEqual(compare_golden({"peak": .1, "clipped_samples": 0, "duration": 1}, None)["status"], EXTERNAL)
        missing = compare_golden({"peak": .1, "clipped_samples": 0, "duration": 1}, None)
        self.assertFalse(missing["passed"])
        mismatch = compare_golden(
            {"peak": .1, "clipped_samples": 0, "duration": 1},
            {"peak": .2, "clipped_samples": 0, "duration": 1},
        )
        self.assertEqual(mismatch["status"], "FAIL")
        with self.assertRaises(TypeError):
            compare_golden({"peak": .1, "clipped_samples": 0, "duration": 1},
                           {"peak": .2, "clipped_samples": 0, "duration": 1}, tolerance=.25)
        gate = local_release_gate(True, {"status": "PASS"}, [{"id": "x", "implemented": False, "label": "可用"}])
        self.assertFalse(gate["local_ready"])
        self.assertFalse(gate["verify_runtime_called"])
        self.assertIsNone(gate["automated_tests"]["claimed_result"])
        self.assertEqual(listening_record()["status"], "待人工听审")
        self.assertEqual({item["id"] for item in acceptance_matrix()}, {"VCQ-002", "VCQ-013", "REL-009"})

    def test_only_same_project_manifest_and_verified_stage_chain_can_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, _project, _artifact = _make_evidence(folder)
            result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["rerun_performed"])
            self.assertEqual(result["evidence_kind"], "same_project_manifest_and_stage_records")
            self.assertEqual(result["result_role"], "Mix.mixed")
            self.assertIn("Separate", result["executed_stages"])

    def test_handwritten_artifact_summaries_without_project_authority_do_not_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            _project, _artifact_path, fixture_path, _project_data, _artifact_data = _make_evidence(folder)
            forged = Path(folder) / "forged.json"
            forged.write_text(json.dumps({
                "schema_version": 1, "kind": "workspace_golden_rerun",
                "executed_stages": ["Separate", "Clean", "RVC", "Mix"],
                "parameters": {"mode": "voice_conversion", "clean_preset": "light"},
                "result": {"path": "fake.wav", "sha256": "0" * 64},
            }), encoding="utf-8")
            status = workspace_golden_status(str(fixture_path), str(forged), root=folder)
            self.assertNotEqual(status["status"], "PASS")

    def test_same_run_identity_job_spec_source_and_result_role_are_required(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, project, artifact = _make_evidence(folder)
            artifact["job_id"] = "another-job"
            _rewrite_project(project_path, artifact_path, artifact, project)
            self.assertNotEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)["status"], "PASS")

            artifact["job_id"] = project["job_id"]
            artifact["result_role"] = "Master.wav"
            _rewrite_project(project_path, artifact_path, artifact, project)
            self.assertNotEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)["status"], "PASS")

            artifact["result_role"] = "Mix.mixed"
            project["job_spec_sha256"] = "0" * 64
            _rewrite_project(project_path, artifact_path, artifact, project)
            self.assertNotEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)["status"], "PASS")

    def test_cache_hit_or_reused_critical_stage_cannot_satisfy_fresh_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, project, artifact = _make_evidence(folder)
            separate = next(stage for stage in project["stage_records"] if stage["stage_name"] == "Separate")
            separate["parameters"]["_eta_execution_kind"] = "cache_hit"
            _rewrite_project(project_path, artifact_path, artifact, project)
            result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
            self.assertNotEqual(result["status"], "PASS")

    def test_input_hash_output_hash_and_sourcechain_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, project, artifact = _make_evidence(folder)
            clean = next(stage for stage in project["stage_records"] if stage["stage_name"] == "Clean")
            clean["input_hash"] = "{}"
            _rewrite_project(project_path, artifact_path, artifact, project)
            self.assertNotEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)["status"], "PASS")

            project_path, artifact_path, fixture_path, project, artifact = _make_evidence(Path(folder) / "second")
            mix = next(stage for stage in project["stage_records"] if stage["stage_name"] == "Mix")
            mix["parameters"]["_input_manifest"]["vocal"] = _file_ref(Path(folder) / "second" / "instrumental.wav")
            mix["input_hash"] = canonical_json(AudioPipeline._input_hash_data(mix["parameters"]["_input_manifest"]))
            _rewrite_project(project_path, artifact_path, artifact, project)
            self.assertNotEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=str(Path(folder) / "second"))["status"], "PASS")

    def test_caller_tolerance_is_rejected_and_metric_policy_is_fixed(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, _project, artifact = _make_evidence(folder)
            artifact["metric_tolerance"] = .25
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
            self.assertNotEqual(result["status"], "PASS")

            _project_path, _artifact_path, _fixture_path, _project, _artifact = _make_evidence(Path(folder) / "changed")
            changed = workspace_golden_status(str(_fixture_path), str(_artifact_path), root=str(Path(folder) / "changed"))
            self.assertEqual(changed["status"], "PASS")
            # A source metric change beyond the fixed peak threshold fails regardless of artifact contents.
            mixed_path = Path(folder) / "changed" / "mixed.wav"
            _put_audio(mixed_path, .2)
            project_path = Path(folder) / "changed" / "project.json"
            project = json.loads(project_path.read_text(encoding="utf-8"))
            mix = next(stage for stage in project["stage_records"] if stage["stage_name"] == "Mix")
            mix["output_hashes"]["mixed"] = sha256_file(mixed_path)
            _rewrite_project(project_path, Path(folder) / "changed" / "rerun.json", _artifact, project)
            comparison = workspace_golden_status(str(_fixture_path), str(Path(folder) / "changed" / "rerun.json"), root=str(Path(folder) / "changed"))
            self.assertEqual(comparison["status"], "FAIL")

    def test_changed_fixture_version_is_not_treated_as_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            _project, artifact_path, fixture_path, _project_data, _artifact = _make_evidence(folder)
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture["schema_version"] = 2
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            self.assertEqual(workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)["status"], "INCOMPATIBLE_FIXTURE")

    def test_new_route_or_float_precision_is_incompatible_with_pinned_baseline(self):
        for mutate in (
            lambda spec: spec["effective_rvc"].update(output_precision="float32"),
            lambda spec: spec.update(separator_profile={"mode": "dual"}),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as folder:
                project_path, artifact_path, fixture_path, project, artifact = _make_evidence(folder)
                mutate(project["job_spec"])
                project["job_spec"]["spec_hash"] = digest(project["job_spec"])
                project["job_spec_sha256"] = digest(project["job_spec"])
                _rewrite_project(project_path, artifact_path, artifact, project)
                result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
                self.assertEqual(result["status"], "INCOMPATIBLE_BASELINE")

    def test_full_canonical_spec_must_match_beyond_display_context(self):
        with tempfile.TemporaryDirectory() as folder:
            project_path, artifact_path, fixture_path, project, artifact = _make_evidence(folder)
            project["job_spec"]["effective_master"]["true_peak"] = -2.0
            project["job_spec"]["spec_hash"] = digest(project["job_spec"])
            project["job_spec_sha256"] = digest(project["job_spec"])
            _rewrite_project(project_path, artifact_path, artifact, project)
            result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
            self.assertEqual(result["status"], "INCOMPATIBLE_BASELINE")

    def test_baseline_project_sha_must_stay_pinned(self):
        with tempfile.TemporaryDirectory() as folder:
            _project, artifact_path, fixture_path, _project_data, _artifact = _make_evidence(folder)
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture["baseline_project"]["sha256"] = "0" * 64
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            result = workspace_golden_status(str(fixture_path), str(artifact_path), root=folder)
            self.assertEqual(result["status"], "INCOMPATIBLE_BASELINE")


if __name__ == "__main__":
    unittest.main()
