"""Evidence-bound release checks for measured local pipeline runs."""

import hashlib
import json
import math
import os

from core.storage import canonical_json, sha256_file

EXTERNAL = "READY_FOR_EXTERNAL_VALIDATION"
DISCOVER = "python -m unittest discover -s tests -p test_*.py"
BASELINE_SCHEMA_VERSION = 1
PEAK_TOLERANCE = 0.01
DURATION_TOLERANCE_SECONDS = 0.03
CLIPPED_SAMPLE_TOLERANCE = 0
UNSEEDED_ENGINEERING_POLICY = 'unseeded-legacy-envelope-v1'
UNSEEDED_ENGINEERING_PASS = 'PASS_ENGINEERING_WITH_UNSEEDED_BASELINE'
_UNSEEDED_SOURCE_SHA = '11def6865a1474e9f2922422f83b758d8a42d584d2853f0947160fbfe86606cd'
_UNSEEDED_SPEC_SHA = 'cb6202b86c130e610b20abc4c84436c4f6a2a3f3369a31ad668616d6ca57932c'
_UNSEEDED_PROJECT_SHA = '42c67b873d6db16d8463fd3e8fb8fc5b416350665a77ce6ca34905411043e279'
_UNSEEDED_UPSTREAM = (
    ('Separate', 'vocal', 'stems/01_vocal_original.wav', 'f046441a5f02b3b0bdd8303d87dfa1f519fd728d9de69be7720d11cee6769a6a'),
    ('Clean', 'cleaned', 'stems/02_vocal_cleaned.wav', '51839f091601c7790c9e4e63405a7f8ef7012ef8357a0f697751495c2563b98a'),
    ('Separate', 'instrumental', 'stems/04_instrumental.wav', '45fa23c6934b484de24a046c227dd83d4b91acc105c680b4415b7456462c583f'),
)


class BaselineIncompatibility(ValueError):
    """The current run no longer matches the pinned historical baseline."""


def external_item(item_id, missing):
    return {"id": item_id, "status": EXTERNAL, "missing_evidence": list(missing)}


def acceptance_matrix():
    return [
        external_item("VCQ-002", ["人工听审记录"]),
        external_item("VCQ-013", ["人工听审"]),
        external_item("REL-009", ["听审人、日期、片段和选择"]),
    ]


def scope_notes():
    return [
        {"id": "REL-001", "status": "NOT_VERIFIED", "scope": "另一台未预装依赖的 Windows"},
        {"id": "REL-002", "status": "NOT_VERIFIED", "scope": "物理无 GPU 机器"},
        {"id": "REL-003", "status": "NOT_VERIFIED", "scope": "其他 NVIDIA GPU 型号"},
    ]


def package_hashes(root, relative_paths):
    rows = []
    for relative in relative_paths:
        path = os.path.join(root, relative.replace("/", os.sep))
        rows.append({"path": relative, "sha256": sha256_file(path), "bytes": os.path.getsize(path)})
    return rows


def hashes_match(listed, recomputed):
    by_path = {item["path"]: item["sha256"] for item in recomputed}
    return all(by_path.get(item["path"]) == item["sha256"] for item in listed)


def compare_golden(baseline, current):
    """Apply fixed technical tolerances; callers cannot widen them."""
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        return {"passed": False, "status": EXTERNAL, "evidence_kind": "not_run",
                "missing_evidence": ["本次短样本重跑"]}
    keys = ("peak", "clipped_samples", "duration")
    if any(key not in baseline or key not in current for key in keys):
        return {"passed": False, "status": EXTERNAL, "evidence_kind": "incomplete",
                "missing_evidence": ["peak、clipped_samples、duration 测量值"]}
    try:
        values = {key: (float(baseline[key]), float(current[key])) for key in keys}
    except (TypeError, ValueError, OverflowError):
        return {"passed": False, "status": "NOT_READY", "evidence_kind": "invalid_metrics"}
    if any(not math.isfinite(value) for pair in values.values() for value in pair):
        return {"passed": False, "status": "NOT_READY", "evidence_kind": "invalid_metrics"}
    tolerances = {
        "peak": PEAK_TOLERANCE,
        "clipped_samples": CLIPPED_SAMPLE_TOLERANCE,
        "duration": DURATION_TOLERANCE_SECONDS,
    }
    for key, (expected, actual) in values.items():
        if abs(actual - expected) > tolerances[key]:
            return {"passed": False, "status": "FAIL", "evidence_kind": "measured_project_run",
                    "key": key, "expected": expected, "actual": actual,
                    "tolerance": tolerances[key]}
    return {"passed": True, "status": "PASS", "evidence_kind": "measured_project_run",
            "tolerances": tolerances}


def workspace_golden_status(fixture_path, artifact_path=None, root=None):
    """Validate a rerun artifact only through its same-run ``project.json``."""
    if not os.path.isfile(fixture_path):
        return _golden_status("NOT_READY", "基线 fixture 不存在", fixture_present=False)
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, ValueError) as exc:
        return _golden_status("NOT_READY", "基线 fixture 无法读取: " + str(exc), fixture_present=True)
    try:
        baseline = _validate_fixture(fixture)
    except BaselineIncompatibility as exc:
        return _golden_status("INCOMPATIBLE_BASELINE", str(exc), fixture_present=True)
    except (TypeError, ValueError, KeyError) as exc:
        return _golden_status("INCOMPATIBLE_FIXTURE", str(exc), fixture_present=True)
    if artifact_path is None:
        return _golden_status("NOT_RUN", "没有提供本次运行 artifact", fixture_present=True)
    if not os.path.isfile(artifact_path):
        return _golden_status("NOT_RUN", "指定的本次运行 artifact 不存在", fixture_present=True,
                              artifact_path=os.path.abspath(artifact_path))
    try:
        with open(artifact_path, "r", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, ValueError) as exc:
        return _golden_status("NOT_READY", "本次运行 artifact 无法读取: " + str(exc), fixture_present=True,
                              artifact_path=os.path.abspath(artifact_path))
    try:
        project, project_path, stage_map = _validate_artifact(
            fixture, artifact, root or os.getcwd(), baseline)
    except BaselineIncompatibility as exc:
        return _golden_status("INCOMPATIBLE_BASELINE", str(exc), fixture_present=True,
                              artifact_path=os.path.abspath(artifact_path))
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return _golden_status("NOT_READY", "本次运行证据不完整: " + str(exc), fixture_present=True,
                              artifact_path=os.path.abspath(artifact_path))

    mix_stage = stage_map["Mix"]
    result_path = _output_role_path(mix_stage, "mixed")
    try:
        from core.analyzer import probe_metadata
        from core.qc import sample_metrics
        actual_metrics = sample_metrics(result_path)
        duration = float(probe_metadata(result_path)["duration"])
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return _golden_status("NOT_READY", "Mix result 音频无法测量: " + str(exc), fixture_present=True,
                              artifact_path=os.path.abspath(artifact_path))
    baseline_metrics = fixture["points"]["mix_output"]
    comparison = compare_golden(
        {"peak": baseline_metrics["sample_peak"], "clipped_samples": baseline_metrics["clipped_samples"],
         "duration": baseline_metrics["duration_seconds"]},
        {"peak": actual_metrics["sample_peak"], "clipped_samples": actual_metrics["clipped_samples"],
         "duration": duration},
    )
    engineering = _legacy_engineering_envelope(fixture, baseline, project, stage_map)
    return {
        "fixture_present": True,
        "rerun_performed": True,
        "status": comparison["status"],
        "strict_status": comparison["status"],
        "engineering_status": engineering['status'],
        "engineering_comparison": engineering,
        "quality_acceptance": EXTERNAL,
        "evidence_kind": "same_project_manifest_and_stage_records",
        "artifact_path": os.path.abspath(artifact_path),
        "project_manifest_path": project_path,
        "project_manifest_sha256": artifact["project_manifest"]["sha256"],
        "job_id": project["job_id"],
        "source_sha256": project["source_sha256"],
        "job_spec_sha256": project["job_spec_sha256"],
        "baseline_project_path": baseline["project_path"],
        "baseline_project_sha256": baseline["project_sha256"],
        "baseline_job_spec_sha256": baseline["job_spec_sha256"],
        "baseline_separator_route": baseline["separator_route"],
        "baseline_rvc_output_precision": baseline["rvc_output_precision"],
        "result_role": "Mix.mixed",
        "result_path": result_path,
        "result_sha256": mix_stage["output_hashes"]["mixed"],
        "executed_stages": [name for name, stage in stage_map.items()
                             if stage.get("parameters", {}).get("_eta_execution_kind") == "executed"],
        "comparison": comparison,
        "listening_quality": "not_assessed",
        "missing_evidence": [] if comparison["passed"] else ["Golden 数值对比通过"],
    }


def _compare_unseeded_audio(expected, current):
    """Fixed level/headroom invariants, never stochastic sample-peak equality."""
    if expected['sample_rate'] != current['sample_rate'] or expected['channels'] != current['channels']:
        raise ValueError('Sample rate/channel count changed')
    if abs(expected['duration'] - current['duration']) > .02:
        raise ValueError('Audio duration changed beyond 20 ms')
    if not expected['finite'] or not current['finite']:
        raise ValueError('Nonfinite audio')
    peak = float(current['sample_peak'])
    if not math.isfinite(peak) or peak < 0 or peak > min(1., expected['sample_peak'] + .01):
        raise ValueError('Peak increased beyond fixed headroom policy')
    if current['clipped_samples'] > expected['clipped_samples']:
        raise ValueError('Additional clipped samples')
    left, right = expected['channel_rms'], current['channel_rms']
    if len(left) != expected['channels'] or len(right) != expected['channels']:
        raise ValueError('RMS channel count changed')
    deltas = []
    for before, after in zip(left, right):
        if not math.isfinite(before) or not math.isfinite(after) or before <= 1e-9 or after < 0:
            raise ValueError('RMS baseline/current level cannot be validated')
        relative = abs(after - before) / before
        if relative > .02:
            raise ValueError('RMS level changed beyond fixed 2 percent policy')
        deltas.append(relative)
    return {'rms_relative_deltas': deltas, 'peak_delta': peak - expected['sample_peak'],
            'duration_delta': current['duration'] - expected['duration']}


def _assert_unseeded_worker(worker, spec, captured):
    terminal = worker.get('terminal_worker_result')
    if (not isinstance(terminal, dict) or terminal.get('worker') != 'rvc' or
            terminal.get('status') != 'complete' or 'validation_seed' not in terminal or
            terminal['validation_seed'] is not None or terminal.get('output_precision') != 'legacy_int16' or
            terminal.get('actual_f0') != spec['f0_method'] or
            (terminal.get('model_guard') or {}).get('complete_weight_shapes_verified') is not True):
        raise ValueError('No explicit successful unseeded legacy worker identity')
    if not any(canonical_json(row) == canonical_json(worker) for row in captured.values()):
        raise ValueError('Worker receipt is not captured by this project manifest')


def _legacy_engineering_envelope(fixture, baseline, project, stages):
    """A separately named assay for one pinned, unseeded historical observation.

    The symmetric legacy comparison stays visible and unchanged. This does not
    accept new algorithms, seeded runs, incompatible jobs, or caller thresholds.
    """
    result = {'status': 'NOT_APPLICABLE', 'policy': UNSEEDED_ENGINEERING_POLICY,
              'quality_acceptance': EXTERNAL, 'listening_quality': 'not_assessed'}
    if (fixture.get('engineering_policy') != {'id': UNSEEDED_ENGINEERING_POLICY,
                                            'baseline_kind': 'unseeded_legacy_observation'} or
            baseline['source_sha256'] != _UNSEEDED_SOURCE_SHA or
            baseline['job_spec_sha256'] != _UNSEEDED_SPEC_SHA or
            baseline['project_sha256'] != _UNSEEDED_PROJECT_SHA):
        result['reason'] = 'Only the pinned historical unseeded case is eligible'
        return result
    try:
        if baseline['separator_route'] != 'hp2' or baseline['rvc_output_precision'] != 'legacy_int16':
            raise ValueError('Historical execution route/precision changed')
        spec = project['job_spec']
        params = stages['RVC']['parameters']
        if (canonical_json(params.get('effective_rvc')) != canonical_json(spec['effective_rvc']) or
                params.get('f0_method') != spec['f0_method'] or
                params.get('pitch_shift') != spec['pitch_shift'] or
                params.get('index_rate') != spec['index_rate'] or
                stages['Mix']['parameters'].get('reverb') is not True):
            raise ValueError('Executed RVC/Mix parameters differ from the historical baseline')
        sidecar = _output_role_path(stages['RVC'], 'worker_path')
        with open(sidecar, encoding='utf-8') as handle:
            worker = json.load(handle)
        _assert_unseeded_worker(worker, spec, project.get('worker_execution', {}))
        parent = os.path.dirname(baseline['project_path'])
        for name, role, relative, expected_sha in _UNSEEDED_UPSTREAM:
            path, digest = _output_role(stages[name], role)
            if digest != expected_sha or _sha256(path) != expected_sha or _sha256(os.path.join(parent, relative)) != expected_sha:
                raise ValueError('Deterministic upstream stem changed: ' + role)
        from core.analyzer import probe_metadata
        from core.qc import sample_metrics
        comparisons = {}
        for name, role, point, relative in (
                ('RVC', 'rvc', 'rvc_output', 'stems/03_vocal_rvc_zhoujielun.wav'),
                ('Mix', 'mixed', 'mix_output', '.work/06_mixed.wav')):
            before = os.path.join(parent, relative)
            if _sha256(before) != fixture['points'][point]['sha256']:
                raise ValueError('Pinned baseline audio bytes changed: ' + name)
            after = _output_role_path(stages[name], role)
            expected = dict(sample_metrics(before), **probe_metadata(before))
            current = dict(sample_metrics(after), **probe_metadata(after))
            comparisons[name] = _compare_unseeded_audio(expected, current)
        result.update(status=UNSEEDED_ENGINEERING_PASS, comparisons=comparisons,
                      worker_seed=None, deterministic_upstream_sha256_verified=True,
                      rms_relative_limit=.02, peak_increase_limit=.01, duration_limit_seconds=.02,
                      note='Stable level/headroom and execution invariants only; strict numerical result is retained separately.')
    except (OSError, TypeError, ValueError, KeyError, RuntimeError) as exc:
        result.update(status='FAIL', reason=str(exc))
    return result


def _validate_fixture(fixture):
    if not isinstance(fixture, dict) or fixture.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ValueError("不支持的 Golden fixture schema_version")
    if fixture.get("kind") != "voice_chain_observation" or fixture.get("role") != "before":
        raise ValueError("Golden fixture 必须是已冻结的 before observation")
    context = fixture.get("context")
    points = fixture.get("points")
    if not isinstance(context, dict) or not isinstance(points, dict):
        raise ValueError("fixture context/points 格式无效")
    if not all(key in context for key in ("clean_preset", "f0_method", "mode", "pitch_shift", "rvc_model")):
        raise ValueError("fixture context 缺少冻结参数")
    if context["mode"] != "voice_conversion":
        raise ValueError("fixture mode 与 voice_conversion release gate 不兼容")
    if not all(key in points for key in ("pre_separation", "mix_output")):
        raise ValueError("fixture 缺少 pre_separation 或 mix_output 点位")
    if not points["pre_separation"].get("sha256"):
        raise ValueError("fixture 缺少源 WAV SHA-256")
    output = points["mix_output"]
    for key in ("sample_peak", "clipped_samples", "duration_seconds"):
        if type(output.get(key)) not in (int, float) or not math.isfinite(float(output[key])):
            raise ValueError("fixture mix_output 指标无效：" + key)
    baseline_ref = fixture.get("baseline_project")
    if (not isinstance(baseline_ref, dict) or not baseline_ref.get("path") or
            not baseline_ref.get("sha256")):
        raise BaselineIncompatibility("fixture 未固定 historical baseline project 路径和 SHA-256")
    from core.config import BASE_DIR
    baseline_path = _evidence_path(baseline_ref["path"], BASE_DIR)
    workspace_root = os.path.normcase(os.path.abspath(BASE_DIR))
    try:
        if os.path.commonpath((workspace_root, os.path.normcase(baseline_path))) != workspace_root:
            raise BaselineIncompatibility("baseline project 必须位于当前 workspace 内")
    except ValueError as exc:
        raise BaselineIncompatibility("baseline project 路径不属于当前 workspace") from exc
    try:
        project_sha = _sha256(baseline_path)
        if project_sha != baseline_ref["sha256"]:
            raise BaselineIncompatibility("pinned baseline project SHA-256 已变化")
        with open(baseline_path, "r", encoding="utf-8") as handle:
            baseline_project = json.load(handle)
    except (OSError, ValueError) as exc:
        if isinstance(exc, BaselineIncompatibility):
            raise
        raise BaselineIncompatibility("pinned baseline project 无法读取：" + str(exc)) from exc
    if not isinstance(baseline_project, dict) or baseline_project.get("status") != "DONE":
        raise BaselineIncompatibility("pinned baseline project 不是 DONE 项目")
    baseline_spec = baseline_project.get("job_spec")
    if not isinstance(baseline_spec, dict):
        raise BaselineIncompatibility("pinned baseline project 缺少冻结 JobSpec")
    from core.job_spec import digest
    baseline_spec_sha = digest(baseline_spec)
    if baseline_spec.get("spec_hash") != baseline_spec_sha:
        raise BaselineIncompatibility("pinned baseline JobSpec SHA-256 无效")
    if baseline_spec_sha != baseline_ref.get("job_spec_sha256"):
        raise BaselineIncompatibility("fixture baseline JobSpec SHA-256 与 project 不一致")
    baseline_source_path = _evidence_path(baseline_project.get("source_path"), BASE_DIR)
    source_sha = _sha256(baseline_source_path)
    if (source_sha != baseline_ref.get("source_sha256") or
            source_sha != points["pre_separation"].get("sha256")):
        raise BaselineIncompatibility("fixture source SHA-256 与 pinned baseline project 不一致")
    model = baseline_spec.get("effective_rvc")
    if not isinstance(model, dict):
        raise BaselineIncompatibility("pinned baseline JobSpec 缺少 effective RVC identity")
    artifact_hashes = model.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise BaselineIncompatibility("pinned baseline JobSpec 缺少 RVC artifact hashes")
    for key in ("pth", "index"):
        expected = artifact_hashes.get(key)
        path = model.get(key)
        if expected:
            if not path or not os.path.isfile(path) or _sha256(path) != expected:
                raise BaselineIncompatibility("pinned baseline RVC " + key + " snapshot 无法验证")
    route = _frozen_separator_route(baseline_spec)
    precision = model.get("output_precision", "legacy_int16")
    if precision not in ("float32", "legacy_int16"):
        raise BaselineIncompatibility("pinned baseline RVC output precision 无效")
    effective_identity = {
        "mode": baseline_project.get("mode"),
        "clean_preset": baseline_spec.get("clean_preset"),
        "f0_method": (baseline_spec.get("effective_f0") or {}).get(
            "executed", baseline_spec.get("f0_method")),
        "pitch_shift": baseline_spec.get("pitch_shift"),
        "rvc_model_key": baseline_spec.get("rvc_model_key"),
        "rvc_artifact_hashes": artifact_hashes,
        "rvc_profile": model.get("profile"),
        "separator_route": route,
        "rvc_output_precision": precision,
    }
    expected_identity = baseline_ref.get("effective_identity")
    if not isinstance(expected_identity, dict) or canonical_json(expected_identity) != canonical_json(effective_identity):
        raise BaselineIncompatibility("fixture effective identity 与 pinned baseline JobSpec 不一致")
    expected_context = {
        "mode": baseline_project.get("mode"),
        "clean_preset": baseline_spec.get("clean_preset"),
        "f0_method": effective_identity["f0_method"],
        "pitch_shift": baseline_spec.get("pitch_shift"),
        "rvc_model": baseline_spec.get("rvc_model_key"),
    }
    if {key: context.get(key) for key in expected_context} != expected_context:
        raise BaselineIncompatibility("fixture context 与 pinned baseline JobSpec 不一致")
    return {
        "project_path": baseline_path,
        "project_sha256": project_sha,
        "job_spec": baseline_spec,
        "job_spec_sha256": baseline_spec_sha,
        "source_path": baseline_source_path,
        "source_sha256": source_sha,
        "separator_route": route,
        "rvc_output_precision": precision,
        "effective_identity": effective_identity,
    }


def _validate_artifact(fixture, artifact, root, baseline):
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ValueError("artifact schema_version 必须为 1")
    if artifact.get("kind") != "workspace_golden_rerun":
        raise ValueError("artifact kind 必须为 workspace_golden_rerun")
    if "metric_tolerance" in artifact:
        raise ValueError("artifact 不允许提供 metric_tolerance；比较阈值由固定 policy 管理")
    project_ref = artifact.get("project_manifest")
    if not isinstance(project_ref, dict) or not project_ref.get("path") or not project_ref.get("sha256"):
        raise ValueError("artifact 必须引用同次 project.json 路径和 SHA-256")
    project_path = _evidence_path(project_ref["path"], root)
    actual_project_sha = _sha256(project_path)
    if actual_project_sha != project_ref["sha256"]:
        raise ValueError("project.json SHA-256 与 artifact 引用不一致")
    with open(project_path, "r", encoding="utf-8") as handle:
        project = json.load(handle)
    if not isinstance(project, dict) or project.get("status") != "DONE":
        raise ValueError("project.json 不是已完成项目")
    job_id = project.get("job_id")
    if not job_id or artifact.get("job_id") != job_id:
        raise ValueError("artifact job_id 与 project.json 不一致")
    spec = project.get("job_spec")
    if not isinstance(spec, dict):
        raise ValueError("project.json 缺少冻结 JobSpec")
    from core.job_spec import digest
    actual_spec_sha = digest(spec)
    if (not spec.get("spec_hash") or spec.get("spec_hash") != actual_spec_sha or
            project.get("job_spec_sha256") != actual_spec_sha):
        raise ValueError("project.json JobSpec SHA-256 不匹配")
    if canonical_json(spec) != canonical_json(baseline["job_spec"]):
        raise BaselineIncompatibility(
            "当前 frozen JobSpec 与 pinned historical baseline 不兼容；"
            "separator route、RVC precision 或其他冻结参数已变化")
    expected_parameters = {
        "mode": project.get("mode"),
        "clean_preset": spec.get("clean_preset"),
        "f0_method": spec.get("f0_method"),
        "pitch_shift": spec.get("pitch_shift"),
        "rvc_model": spec.get("rvc_model_key"),
    }
    context = fixture["context"]
    if expected_parameters != {key: context[key] for key in expected_parameters}:
        raise ValueError("project JobSpec 与 baseline fixture 参数不一致")
    source_path = _evidence_path(project.get("source_path"), os.path.dirname(project_path))
    actual_source_sha = _sha256(source_path)
    if actual_source_sha != project.get("source_sha256"):
        raise ValueError("project.json source SHA-256 与源文件不一致")
    if actual_source_sha != fixture["points"]["pre_separation"]["sha256"]:
        raise ValueError("当前任务源 SHA-256 与 baseline fixture 不一致")

    stages = project.get("stage_records")
    if not isinstance(stages, list):
        raise ValueError("project.json 缺少权威 stage_records")
    stage_map = {}
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("stage_name") in stage_map:
            raise ValueError("stage_records 存在无效或重复 stage")
        if stage.get("job_id") != job_id:
            raise ValueError("stage record 不属于 project.json job_id")
        _verify_stage_record(stage)
        stage_map[stage["stage_name"]] = stage
    required = ("Ingest", "Separate", "Clean", "RVC", "Align", "Mix")
    for name in required:
        stage = stage_map.get(name)
        if (not stage or stage.get("status") != "DONE" or
                (name in ("Separate", "Clean", "RVC", "Mix") and
                 (stage.get("parameters") or {}).get("_eta_execution_kind") != "executed")):
            raise ValueError(name + " stage 必须是本任务实际执行且输出已核验的 DONE")

    ingest = stage_map["Ingest"]
    _assert_input_role(ingest, "input", source_path, actual_source_sha, "Ingest 输入必须来自本次源音频")
    normalized = _output_role(ingest, "standard_wav")
    clean_source_stage = stage_map.get("SourceClean")
    if clean_source_stage and clean_source_stage.get("status") == "DONE":
        normalized = _output_role(clean_source_stage, "cleaned")
    separate = stage_map["Separate"]
    _assert_input_role(separate, "input", normalized[0], normalized[1],
                       "Separate 必须使用 Ingest/SourceClean 的实际音频输出")
    vocal = _output_role(separate, "vocal")
    clean = stage_map["Clean"]
    _assert_input_role(clean, "input", vocal[0], vocal[1], "Clean 输入必须来自 Separate.vocal")
    cleaned = _output_role(clean, "cleaned")
    expected_rvc_input = cleaned
    restoration = stage_map.get("VocalRestore")
    if restoration and restoration.get("status") == "DONE":
        expected_rvc_input = _output_role(restoration, "restored")
    correction = stage_map.get("PitchCorrection")
    if correction and correction.get("status") == "DONE":
        expected_rvc_input = _output_role(correction, "corrected")
    _assert_input_role(stage_map["RVC"], "input", expected_rvc_input[0], expected_rvc_input[1],
                       "RVC 输入必须来自本任务 VocalRestore/PitchCorrection/Clean 链")

    aligned = _output_role(stage_map["Align"], "aligned")
    mix = stage_map["Mix"]
    _assert_input_role(mix, "vocal", aligned[0], aligned[1], "Mix.vocal 必须来自本任务 Align.aligned")
    instrumental = _output_role(separate, "instrumental", allow_alternate="other")
    ducking = stage_map.get("Ducking")
    if ducking and ducking.get("status") == "DONE":
        _assert_input_role(ducking, "vocal", aligned[0], aligned[1], "Ducking.vocal 必须来自 Align.aligned")
        instrumental = _output_role(ducking, "instrumental")
    _assert_input_role(mix, "instrumental", instrumental[0], instrumental[1],
                       "Mix.instrumental 必须来自本任务 Separate/Ducking 产物")
    if artifact.get("result_role") != "Mix.mixed":
        raise ValueError("artifact result_role 必须指向权威 Mix.mixed 输出")
    _output_role(mix, "mixed")
    return project, project_path, stage_map


def _frozen_separator_route(spec):
    profile = spec.get("separator_profile")
    if profile is not None:
        try:
            from core.separator_routing import validate_separator_profile
            return validate_separator_profile(profile)["mode"]
        except (TypeError, ValueError):
            return None
    backend = spec.get("separator_backend")
    # Before separator routing was explicit, frozen jobs used the original HP2
    # worker. Preserve that historical interpretation for old snapshots.
    if backend in (None, "hp2"):
        return "hp2"
    return "unknown:" + str(backend)


def _verify_stage_record(stage):
    name = stage.get("stage_name")
    if not isinstance(name, str) or not name:
        raise ValueError("stage record 缺少 stage_name")
    parameters = stage.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(name + " 缺少 stage parameters")
    input_manifest = parameters.get("_input_manifest")
    if input_manifest is None:
        raise ValueError(name + " 缺少逐项输入文件证据")
    actual_input_hash = canonical_json(_hashable_input_manifest(input_manifest))
    if stage.get("input_hash") != actual_input_hash:
        raise ValueError(name + " input_hash 与输入文件 SHA-256 不匹配")
    outputs = stage.get("outputs")
    hashes = stage.get("output_hashes")
    if not isinstance(outputs, dict) or not isinstance(hashes, dict):
        raise ValueError(name + " outputs/output_hashes 格式无效")
    from core.pipeline_support import _output_paths
    paths = _output_paths(outputs)
    if set(paths) != set(hashes):
        raise ValueError(name + " output role 与 output_hashes 不匹配")
    for role, path in paths.items():
        if not os.path.isfile(path) or _sha256(path) != hashes.get(role):
            raise ValueError(name + "." + role + " 输出文件 hash 校验失败")


def _hashable_input_manifest(value):
    if isinstance(value, dict):
        if value.get("_stage_input_file") is True and set(value) == {"_stage_input_file", "path", "sha256"}:
            path = _evidence_path(value["path"], os.getcwd())
            if not os.path.isfile(path) or _sha256(path) != value.get("sha256"):
                raise ValueError("stage 输入文件 SHA-256 校验失败")
            return {"sha256": value["sha256"], "kind": "file"}
        return {key: _hashable_input_manifest(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_hashable_input_manifest(item) for item in value]
    return value


def _assert_input_role(stage, role, expected_path, expected_sha, message):
    manifest = (stage.get("parameters") or {}).get("_input_manifest")
    item = manifest.get(role) if isinstance(manifest, dict) else None
    if not isinstance(item, dict) or item.get("_stage_input_file") is not True:
        raise ValueError(message)
    actual_path = _evidence_path(item.get("path"), os.getcwd())
    if os.path.normcase(os.path.abspath(actual_path)) != os.path.normcase(os.path.abspath(expected_path)):
        raise ValueError(message)
    if item.get("sha256") != expected_sha:
        raise ValueError(message)


def _output_role(stage, role, allow_alternate=None):
    outputs = stage.get("outputs") or {}
    hashes = stage.get("output_hashes") or {}
    selected = role
    if selected not in outputs and allow_alternate and allow_alternate in outputs:
        selected = allow_alternate
    path = outputs.get(selected)
    if not isinstance(path, str) or not path or not hashes.get(selected):
        raise ValueError(stage.get("stage_name") + " 缺少实际输出 role: " + role)
    path = _evidence_path(path, os.getcwd())
    if not os.path.isfile(path) or _sha256(path) != hashes[selected]:
        raise ValueError(stage.get("stage_name") + "." + selected + " 输出身份无效")
    return path, hashes[selected]


def _output_role_path(stage, role):
    return _output_role(stage, role)[0]


def _evidence_path(value, root):
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise ValueError("证据路径无效")
    path = os.fspath(value)
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))


def _sha256(path):
    return sha256_file(path)


def _golden_status(status, missing, **details):
    return {"fixture_present": details.pop("fixture_present", False), "rerun_performed": False,
            "status": status, "listening_quality": "not_assessed", "missing_evidence": [missing], **details}


def listening_record():
    return {"status": "待人工听审", "listener": None, "date": None,
            "clip": None, "choice": None, "listening_quality": "not_assessed"}


def local_release_gate(package_ok, runtime_report=None, component_rows=None,
                       golden_fixture_path=None, golden_artifact_path=None, golden_root=None):
    runtime_status = None if not isinstance(runtime_report, dict) else runtime_report.get("status")
    unimplemented = []
    if isinstance(component_rows, list):
        unimplemented = [row["id"] for row in component_rows
                         if row.get("implemented") is False and row.get("label") == "可用"]
    automated = {"command": DISCOVER, "claimed_result": None}
    golden_report = (workspace_golden_status(golden_fixture_path, golden_artifact_path, golden_root)
                     if golden_fixture_path else {"status": "NOT_RUN", "rerun_performed": False,
                                                  "missing_evidence": ["本次短样本重跑 artifact"]})
    golden_ready = ((golden_report.get("status") == "PASS" or
                     golden_report.get('engineering_status') == UNSEEDED_ENGINEERING_PASS) and
                    golden_report.get("rerun_performed") is True and
                    golden_report.get("evidence_kind") == "same_project_manifest_and_stage_records")
    ready = bool(package_ok) and runtime_report_ready(runtime_report) and not unimplemented and golden_ready
    return {
        "package_hashes_ok": bool(package_ok), "runtime_status": runtime_status,
        "unimplemented_marked_available": unimplemented, "automated_tests": automated,
        "golden_rerun": golden_report, "verify_runtime_called": False, "local_ready": ready,
        "external": acceptance_matrix(), "scope_notes": scope_notes(),
    }


def runtime_report_ready(report):
    if not isinstance(report, dict):
        return False
    if report.get("status") == "PASS":
        return True
    if report.get("status") != "PASS_WITH_WARNINGS":
        return False
    checks = report.get("checks")
    if not isinstance(checks, dict) or not checks:
        return False
    for name, row in checks.items():
        if not isinstance(row, dict):
            return False
        status = row.get("status")
        if name == "ffmpeg" and status is None:
            if set(row) != {"ffmpeg.exe", "ffprobe.exe"} or any(
                    not isinstance(value, dict) or value.get("status") != "PASS" for value in row.values()):
                return False
        elif status == "WARN" and name == "cuda" and row.get("available") is False:
            continue
        elif status == "N/A" and name == "database":
            continue
        elif status != "PASS":
            return False
    return True


def write_gate_record(path, gate):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(gate, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path
