"""View models for the desktop wizard and advanced settings.

Hidden fields stay in the frozen parameter set. The quick self-test is the
one caller that runs a short isolated chain. It does not write job history.
"""
import copy
import math
import os


def first_run_preferences(output_directory, cache_quota_gb):
    """Validate explicitly completed onboarding settings; skipping writes none."""
    if not isinstance(output_directory, str) or not output_directory.strip():
        raise ValueError('请选择成品输出目录')
    output = os.path.abspath(os.path.expanduser(output_directory.strip()))
    if os.path.exists(output) and not os.path.isdir(output):
        raise ValueError('成品输出路径不是目录')
    text = str(cache_quota_gb).strip()
    try:
        quota = None if not text else float(text)
    except ValueError:
        raise ValueError('缓存上限必须是正数 GiB，留空表示不限')
    if quota is not None and (not math.isfinite(quota) or quota <= 0 or not math.isfinite(quota*1024**3) or int(quota*1024**3) < 1):
        raise ValueError('缓存上限必须是正数 GiB，留空表示不限')
    return {'custom_output_dir':output, 'quota_bytes':None if quota is None else int(quota*1024**3)}

from core.config import CLEAN_PRESETS, MASTER_PRESETS, _validate_model_profile
from core.f0_method import rmvpe_acceptance
from core.separator_routing import dual_profile, hp2_profile, plan_separator_routes, validate_separator_profile

F0_CHOICES = ("harvest", "pm", "crepe", "rmvpe")
EXPERIENCE = ("basic", "advanced", "expert")
_HELP = {
    "f0": "F0 支持 harvest、pm、crepe 和 RMVPE 适配器。权重缺失时会在任务快照中记录 harvest 回退；这不代表音质验收通过。",
    "index": "Index 是任务参数。Profile 里的 index 不会替换任务值。",
    "protect": "Protect 来自已冻结的模型 Profile，搜索不会另造第二个 protect。",
    "separator": "新换声任务默认使用 Kim Vocal 2 人声 + Inst HQ 3 伴奏；CPU ONNX 推理。可显式切换到 HP2。",
}


def rvc_settings(draft):
    """Validate a profile draft with the existing profile rules."""
    profile = _validate_model_profile(draft)
    return {
        "profile": profile,
        "f0_choices": list(F0_CHOICES),
        "device_choices": ["auto"],
        "help": {**_HELP, "f0": _f0_help()},
    }


def _f0_help():
    state = rmvpe_acceptance()
    if state["runnable"]:
        return "F0 支持 harvest、pm、crepe 和 RMVPE 适配器；本机 rmvpe.pt 已探测。任务会冻结实际调用方式，听审仍未做。"
    if state['weight_present']:
        return '本机 RMVPE 权重校验失败，适配器不可选；请修复为受信任的官方权重。'
    return "F0 支持 harvest、pm、crepe 和 RMVPE 适配器；本机缺 rmvpe.pt 时会明确记录 harvest 回退。"


def queue_flow_options(mode, parameters, frozen=False):
    """Mirror optional stage slots from this queue item's immutable policy."""
    voice=mode=='voice_conversion'
    midi=voice and bool(parameters.get('generate_midi',False))
    return dict(generate_midi=midi,
        source_preclean=bool(parameters.get('source_preclean_enabled',False)),
        vocal_restore=voice and (not frozen or 'vocal_restore_profile' in parameters or
                                isinstance(parameters.get('effective_vocal_restoration'),dict)),
        parameter_search=voice and bool(parameters.get('parameter_search_enabled',False)),
        pitch_correction=voice and (not frozen or 'pitch_correction_mode' in parameters),
        midi_cleanup=(mode=='transcribe' or midi) and (not frozen or 'midi_cleanup' in parameters),
        mix_ducking=(bool(parameters.get('mix_ducking',False)) if voice and
                     (not frozen or 'mix_ducking' in parameters) else None))


def separator_settings(selected="dual", weight_dir=None):
    if selected not in ("dual", "hp2"):
        raise ValueError("未知分离路线")
    dual = plan_separator_routes("kim_vocal_2", "inst_hq_3", weight_dir=weight_dir)
    return {
        "selected": selected,
        "default": "dual",
        "choices": ["dual", "hp2"],
        "dual_available": bool(dual.get("available")),
        "dual_reason": dual.get("reason", ""),
        "unavailable": [],
        "help": _HELP["separator"],
    }


def master_settings(preset_name, overrides=None):
    if preset_name not in MASTER_PRESETS:
        raise ValueError("未知母带预设")
    preset = dict(MASTER_PRESETS[preset_name])
    if overrides:
        for key, value in overrides.items():
            if key not in ("target_lufs", "true_peak"):
                raise ValueError("不增加未实现的母带字段")
            if type(value) is bool or type(value) not in (int, float):
                raise ValueError(key + " 无效")
            preset[key] = value
    if not -40 <= preset["target_lufs"] <= -6:
        raise ValueError("LUFS 超出可冻结范围")
    return {"preset": preset_name, "effective_master": preset, "extra_eq": False}


def clean_settings(preset_name, skip_vocal_eq=False):
    if preset_name not in CLEAN_PRESETS:
        raise ValueError("未知清理预设")
    if type(skip_vocal_eq) is not bool:
        raise ValueError("skip_vocal_eq 只能是布尔值")
    return {
        "preset": preset_name,
        "presets": list(CLEAN_PRESETS),
        "skip_vocal_eq": skip_vocal_eq,
        "custom_filter": False,
    }


def experience_view(level, parameters):
    """Fold the display. The returned parameters stay complete."""
    if level not in EXPERIENCE:
        raise ValueError("未知界面层级")
    if not isinstance(parameters, dict):
        raise ValueError("参数无效")
    required = ("mode", "clean_preset", "master_preset", "rvc_model_key", "pitch_shift", "f0_method")
    missing = [key for key in required if key not in parameters]
    if missing:
        raise ValueError("隐藏不等于删除参数")
    visible = ["mode", "rvc_model_key", "clean_preset", "master_preset"]
    if level != "basic":
        visible.extend(["pitch_shift", "f0_method", "index_rate", "protect"])
    return {"level": level, "visible": visible, "parameters": dict(parameters)}


def help_text(key):
    if key not in _HELP:
        raise ValueError("没有这项帮助")
    return _f0_help() if key == "f0" else _HELP[key]


def tutorial_state(skipped=False, reopen=False):
    return {
        "steps": ["三种模式", "质量档", "输出", "试听"],
        "skippable": True,
        "skipped": bool(skipped),
        "reopen": bool(reopen),
        "resets_jobs": False,
        "forces_advanced": False,
    }


def quick_test_plan(mode_status, missing_ids):
    """Stop before a GPU worker when the mode is already blocked."""
    blocked = mode_status != "AVAILABLE" or bool(missing_ids)
    return {
        "mode_status": mode_status,
        "missing": list(missing_ids),
        "start_gpu": False if blocked else False,
        "preflight_only": True,
        "writes_history": False,
        "stopped": blocked,
    }


def benchmark_view(elapsed_seconds=None):
    if elapsed_seconds is None:
        return {"ran": False, "elapsed_seconds": None, "changes_preset": False}
    if type(elapsed_seconds) is bool or type(elapsed_seconds) not in (int, float) or elapsed_seconds < 0:
        raise ValueError("基准耗时无效")
    return {"ran": True, "elapsed_seconds": float(elapsed_seconds), "changes_preset": False}


def preflight_decision(report, mode):
    """Block a start from capability data. This does not create a job."""
    modes = report.get("modes") if isinstance(report, dict) else None
    info = modes.get(mode) if isinstance(modes, dict) else None
    if not isinstance(info, dict):
        return {"allowed": False, "creates_done_job": False, "message": "没有该模式的组件报告"}
    if info.get("status") != "AVAILABLE":
        missing = info.get("missing") or []
        return {
            "allowed": False,
            "creates_done_job": False,
            "missing": list(missing),
            "message": "预检未通过：" + ", ".join(missing),
        }
    return {"allowed": True, "creates_done_job": False, "missing": [], "message": "预检通过"}


def route_capability_report(registry, mode, separator_profile=None):
    """Evaluate a mode against the selected separator while retaining all other dependencies."""
    from core.component_platform import capability_report

    if mode != "voice_conversion" or separator_profile is None:
        return capability_report(registry)
    checked = validate_separator_profile(separator_profile)
    catalog = copy.deepcopy(registry.get("catalog"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("components"), list):
        raise ValueError("组件注册表缺少可检查的目录")
    mode_component = next((item for item in catalog["components"]
                           if item.get("id") == "mode.voice_conversion"), None)
    if mode_component is None:
        raise ValueError("组件目录缺少换声模式定义")
    other = [item for item in mode_component.get("dependencies", [])
             if not str(item).startswith("engine.separator.")]
    selected = [checked[side]["component_id"] for side in ("vocal", "instrumental")]
    if checked["mode"] == "dual":
        selected.append("engine.separator.mdx")
    mode_component["dependencies"] = list(dict.fromkeys(other + selected))
    routed = dict(registry)
    routed["catalog"] = catalog
    return capability_report(routed)


def mode_health(report):
    rows = []
    modes = report.get("modes") if isinstance(report, dict) else {}
    for name in ("polish", "voice_conversion", "transcribe"):
        info = modes.get(name) or {}
        rows.append({
            "mode": name,
            "status": info.get("status", "BLOCKED"),
            "missing": list(info.get("missing") or []),
        })
    return rows


def repair_request(missing_ids, cancel=False, unimplemented=()):
    """Hand missing managed ids to repair. Cancel changes nothing."""
    blocked = set(unimplemented)
    chosen = []
    for component_id in missing_ids:
        if component_id in blocked or component_id.startswith("model.rvc.user"):
            continue
        if component_id not in chosen:
            chosen.append(component_id)
    if cancel:
        return {"cancel": True, "disk_changed": False, "ids": chosen}
    return {"cancel": False, "disk_changed": False, "ids": chosen, "note": "只提交修复计划，不在这个函数里写盘"}


def wizard_lines(plan, hardware, center_rows):
    """Text for the first-run window. GPU text follows the scan."""
    gpu = hardware.get("gpu") if isinstance(hardware, dict) else None
    gpu_status = gpu.get("status") if isinstance(gpu, dict) else "NOT_PROBED"
    if gpu_status == "NOT_PROBED":
        gpu_line = "GPU：未探测"
    else:
        gpu_line = "GPU：" + str(gpu_status)
    ready = [row["id"] for row in center_rows if row["label"] == "可用"]
    install_candidates = [row["id"] for row in plan["installable"] if row["id"] not in set(ready)]
    lines = [
        "欢迎",
        "系统检测使用注册表和硬件扫描的结果。",
        gpu_line,
        "安装方式：同版本离线包 / 按模式计划 / 自定义计划；未配置网络来源",
        "已就绪：" + ", ".join(ready),
        "离线修复候选：" + ", ".join(install_candidates),
    ]
    unavailable = [row["id"] for row in center_rows if row["label"] == "不可用"]
    lines.append("不可用（不是未安装）：" + ", ".join(unavailable))
    text = "\n".join(lines)
    if "CUDA 可用" in text:
        raise ValueError("未探测不能写成 CUDA 可用")
    return text


def wizard_report(registry, hardware):
    from core.install_platform import build_install_plan, component_center_rows
    modes = ["polish", "voice_conversion", "transcribe"]
    plan = build_install_plan(registry["catalog"], modes)
    rows = component_center_rows(registry)
    return wizard_lines(plan, hardware, rows)


def mode_stages(mode, generate_midi=False, source_preclean=False,
                vocal_restore=False, parameter_search=False, pitch_correction=False,
                midi_cleanup=False, mix_ducking=None):
    """Stage names AudioPipeline passes to _run_stage. Not a decorative diagram."""
    if mode == "polish":
        stages = ["Ingest"]
        if source_preclean:
            stages.append("SourceClean")
        return stages + ["Analyze", "Repair", "Tone", "Dynamics", "Stereo", "Master", "QC"]
    if mode == "transcribe":
        stages = ["Ingest"]
        if source_preclean:
            stages.append("SourceClean")
        stages += ["Analyze", "Separate", "Clean"] + (["VocalRestore"] if vocal_restore else []) + ["Transcribe"]
        if midi_cleanup:
            stages.append("MIDICleanup")
        return stages + ["Master", "QC"]
    if mode == "voice_conversion":
        stages = ["Ingest"]
        if source_preclean:
            stages.append("SourceClean")
        stages.extend(["Analyze", "Separate", "Clean"])
        if vocal_restore:
            stages.append("VocalRestore")
        if pitch_correction:
            stages.append("PitchCorrection")
        if generate_midi:
            stages.append("Transcribe")
            if midi_cleanup:
                stages.append("MIDICleanup")
        stages.extend(["RVC"])
        if parameter_search:
            stages.append("RVCSearch")
        stages.extend(["RVCPost", "Align"])
        if mix_ducking is not None:
            stages.append("Ducking")
        stages.extend(["Mix", "Master", "QC"])
        return stages
    raise ValueError("未知模式")


def onboarding_flags(settings):
    """Decide which intro opens. Missing flags mean this install has not finished it."""
    if not isinstance(settings, dict):
        raise ValueError("设置必须是对象")
    completed = settings.get("first_run_completed") is True
    dismissed = settings.get("tutorial_dismissed") is True
    return {
        "open_wizard": not completed,
        "open_tutorial": completed and not dismissed,
        "resets_jobs": False,
    }


def remember_onboarding(settings, first_run_completed=None, tutorial_dismissed=None):
    """Copy settings and set the intro flags. This does not delete jobs."""
    if not isinstance(settings, dict):
        raise ValueError("设置必须是对象")
    updated = dict(settings)
    if first_run_completed is not None:
        if type(first_run_completed) is not bool:
            raise ValueError("首次向导标记只能是布尔值")
        updated["first_run_completed"] = first_run_completed
    if tutorial_dismissed is not None:
        if type(tutorial_dismissed) is not bool:
            raise ValueError("教程标记只能是布尔值")
        updated["tutorial_dismissed"] = tutorial_dismissed
    return updated


def tutorial_steps(mode):
    """Tutorial pages. The target-singer page exists only for voice conversion."""
    if mode not in ("polish", "voice_conversion", "transcribe"):
        raise ValueError("未知模式")
    steps = list(tutorial_state()["steps"])
    if mode == "voice_conversion":
        steps.insert(2, "目标歌手")
    return steps


def run_quick_self_test(registry, profile=None, runner=None, chain=None, work_dir=None, source_wav=None, weight_dir=None):
    """Run the selected chain when its weights and loader exist. Do not write history."""
    from core.separator_routing import dual_profile, hp2_profile, plan_separator_routes, validate_separator_profile
    components = registry.get("components") if isinstance(registry, dict) else None
    if not isinstance(components, dict):
        raise ValueError("自检必须读取组件注册表")
    if chain is None and profile is not None:
        checked = validate_separator_profile(profile)
        chain = "dual" if checked["mode"] == "dual" else "voice_conversion"
    record = {
        "chain": chain,
        "status": "NOT_RUN",
        "gpu_pass": False,
        "inference_ran": False,
        "start_gpu": False,
        "writes_history": False,
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
        "missing": [],
        "stages_ran": [],
        "outputs": {},
        "reason": "",
    }
    if chain is None:
        record["reason"] = "请先选择精修、换声或扒谱"
        return record
    if chain == "dual":
        return _self_test_dual(record, components, runner, work_dir, source_wav, weight_dir, dual_profile, plan_separator_routes, validate_separator_profile)
    if chain == "voice_conversion":
        return _self_test_installed(record, registry, "voice_conversion", runner, work_dir, source_wav, hp2_profile)
    if chain == "polish":
        return _self_test_installed(record, registry, "polish", runner, work_dir, source_wav, None)
    if chain == "transcribe":
        return _self_test_installed(record, registry, "transcribe", runner, work_dir, source_wav, None)
    record["reason"] = "未知自检链"
    return record


def _component_ready(components, component_id):
    row = components.get(component_id)
    return isinstance(row, dict) and row.get("implemented") is True and row.get("status") == "PRESENT"


def _self_test_dual(record, components, runner, work_dir, source_wav, weight_dir, dual_profile, plan_separator_routes, validate_separator_profile):
    checked = validate_separator_profile(dual_profile())
    missing = []
    for side in ("vocal", "instrumental"):
        component_id = checked[side]["component_id"]
        if not _component_ready(components, component_id):
            missing.append(component_id)
    route = plan_separator_routes(checked["vocal"]["id"], checked["instrumental"]["id"], weight_dir=weight_dir)
    record["missing"] = missing
    record["reason"] = route["reason"]
    record["status"] = "READY_FOR_EXTERNAL_VALIDATION"
    if missing or route.get("available") is not True:
        return record
    return _finish_self_test(record, _call_chain(runner, checked, lambda: _execute_dual(checked, _prepare_work(work_dir, source_wav))))


def _self_test_installed(record, registry, chain, runner, work_dir, source_wav, hp2_profile):
    components = registry.get("components") or {}
    if chain == "voice_conversion" and not _component_ready(components, "engine.separator.hp2"):
        record["missing"] = ["engine.separator.hp2"]
        record["status"] = "READY_FOR_EXTERNAL_VALIDATION"
        record["reason"] = "HP2 权重不在注册表的可用状态"
        return record
    profile = hp2_profile() if hp2_profile is not None else {"mode": chain}
    if isinstance(registry.get("catalog"), dict):
        info = route_capability_report(registry, chain, profile if chain == "voice_conversion" else None)["modes"].get(chain) or {}
        if info.get("status") != "AVAILABLE":
            record["missing"] = list(info.get("missing") or [chain])
            record["status"] = "READY_FOR_EXTERNAL_VALIDATION"
            record["reason"] = "这个模式的组件没有齐：" + ", ".join(record["missing"])
            return record
    if source_wav is None:
        source_wav = _default_self_test_source(chain)
    return _finish_self_test(record, _call_chain(runner, profile, lambda: _execute_chain(chain, _prepare_work(work_dir, source_wav))))


def _call_chain(runner, profile, builtin):
    if runner is not None:
        produced = runner(profile)
        if not isinstance(produced, dict):
            produced = {"ok": bool(produced)}
        produced.setdefault("stages", [])
        produced.setdefault("outputs", {})
        return produced
    return builtin()


def _finish_self_test(record, produced):
    ok = bool(isinstance(produced, dict) and produced.get("ok") is True)
    produced = produced if isinstance(produced, dict) else {}
    device = produced.get("inference_device") or produced.get("selected_device") or produced.get("device")
    evidence_valid, evidence_device, evidence = _validate_device_evidence(produced)
    if evidence_valid:
        device = evidence_device
    gpu = evidence_valid and str(device).lower() == "cuda"
    record["inference_ran"] = bool(produced.get("inference_ran") is True or produced.get("stages"))
    record["start_gpu"] = gpu
    record["gpu_pass"] = bool(gpu and ok and record["inference_ran"])
    record["status"] = "MEASURED" if ok and record["inference_ran"] else "READY_FOR_EXTERNAL_VALIDATION"
    record["quality_acceptance"] = "NOT_ASSESSED"
    record["listening_quality"] = "not_assessed"
    record["writes_history"] = False
    record["stages_ran"] = list(produced.get("stages") or [])
    record["outputs"] = dict(produced.get("outputs") or {})
    record["device_evidence"] = evidence
    if produced.get("reason"):
        record["reason"] = produced["reason"]
    elif gpu:
        record["reason"] = "短自检已在 CUDA 设备完成推理。听审未做，质量验收仍是 NOT_ASSESSED。"
    elif record["inference_ran"] and ok:
        record["reason"] = "短自检已完成推理，但本次没有在 CUDA 设备执行。听审未做，质量验收仍是 NOT_ASSESSED。"
    elif not record["inference_ran"]:
        record["reason"] = "短自检没有提供实际推理证据，保持待外部验证。"
    elif record["inference_ran"]:
        record["reason"] = "短自检已完成实际推理；本次设备为 CPU 或未确认 CUDA。听审未做，质量验收仍是 NOT_ASSESSED。"
    else:
        record["reason"] = "精修短自检已跑 Repair、Tone、Dynamics、Stereo、Master、QC。这不是 GPU 通过，听审未做。"
    return record


def _validate_device_evidence(produced):
    """Require an actual worker report and produced files before reporting CUDA."""
    import hashlib
    import json
    import os

    produced=produced if isinstance(produced,dict) else {}
    declaration=produced.get('device_evidence')
    if not isinstance(declaration,dict):
        return False,None,{'validated':False,'reason':'缺少实际 worker 设备证据'}
    report_path=declaration.get('worker_report_path')
    if not isinstance(report_path,str) or not os.path.isfile(report_path):
        return False,None,{'validated':False,'reason':'worker 设备报告不存在'}
    try:
        with open(report_path,'r',encoding='utf-8') as handle:
            report=json.load(handle)
    except (OSError,ValueError):
        return False,None,{'validated':False,'reason':'worker 设备报告无效'}
    output_paths=[]
    if declaration.get('kind')=='dual_mdx':
        execution=report.get('worker_execution') if isinstance(report,dict) else None
        models=(execution or {}).get('models_execution') if isinstance(execution,dict) else None
        expected=('kim_vocal_2','inst_hq_3')
        if (not isinstance(execution,dict) or execution.get('status')!='complete' or
                not isinstance(models,dict) or any(name not in models for name in expected)):
            return False,None,{'validated':False,'reason':'双模型worker缺少两侧执行记录'}
        rows=[models[name] for name in expected]
        if any(not isinstance(row,dict) or row.get('inference_completed') is not True or
               row.get('selected_provider') not in ('CUDAExecutionProvider','CPUExecutionProvider')
               for row in rows):
            return False,None,{'validated':False,'reason':'双模型worker未证明两侧推理完成/provider'}
        device=('cuda' if all(row.get('inference_device')=='cuda' and
                              row.get('selected_provider')=='CUDAExecutionProvider' for row in rows)
                else 'cpu')
        if any(row.get('selected_provider')=='CPUExecutionProvider' for row in rows):
            device='cpu'
        output_paths=[value for value in (produced.get('outputs') or {}).values()
                      if isinstance(value,str) and value.lower().endswith('.wav')]
    elif declaration.get('kind')=='worker_recovery':
        attempts=report.get('recovery_attempts') if isinstance(report,dict) else None
        paths=report.get('output_paths') if isinstance(report,dict) else None
        if (not isinstance(report,dict) or report.get('status')!='complete' or
                not isinstance(attempts,list) or not attempts or
                not isinstance(attempts[-1],dict) or attempts[-1].get('success') is not True or
                not isinstance(paths,list) or not paths):
            return False,None,{'validated':False,'reason':'worker未证明完整完成和输出提交'}
        output_paths=[os.path.abspath(value) for value in paths if isinstance(value,str)]
        expected=[os.path.abspath(value) for value in declaration.get('expected_outputs',[]) if isinstance(value,str)]
        if expected and not set(expected).issubset(set(output_paths)):
            return False,None,{'validated':False,'reason':'worker输出路径与自检产物不一致'}
        device=str(report.get('selected_device') or attempts[-1].get('device') or '').lower()
        if device not in ('cuda','cpu'):
            return False,None,{'validated':False,'reason':'worker没有记录实际设备'}
    else:
        return False,None,{'validated':False,'reason':'未知worker证据类型'}

    artifacts=[]
    for path in output_paths:
        if not os.path.isfile(path) or os.path.getsize(path)<=0:
            return False,None,{'validated':False,'reason':'worker报告的输出文件缺失或为空'}
        digest=hashlib.sha256()
        with open(path,'rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):
                digest.update(chunk)
        artifacts.append({'path':os.path.abspath(path),'bytes':os.path.getsize(path),
                          'sha256':digest.hexdigest()})
    if not artifacts:
        return False,None,{'validated':False,'reason':'没有可校验的worker产物'}
    return True,device,{'validated':True,'kind':declaration.get('kind'),
                        'worker_report_path':os.path.abspath(report_path),
                        'worker_report_status':report.get('status') if isinstance(report,dict) else 'complete',
                        'artifact_hashes':artifacts}


def _default_self_test_source(chain):
    """A pure tone separates to a silent vocal stem. Voice and transcribe use the existing short sample."""
    if chain not in ("voice_conversion", "transcribe"):
        return None
    import os
    from core.config import BASE_DIR
    candidate = os.path.join(BASE_DIR, "temp", "验收 中文短样本.wav")
    if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
        return candidate
    return None


def _prepare_work(work_dir, source_wav):
    import os
    import tempfile
    from core.config import TEMP_DIR
    folder = work_dir or tempfile.mkdtemp(prefix="quick-self-test-", dir=TEMP_DIR)
    os.makedirs(folder, exist_ok=True)
    if source_wav is None:
        source_wav = _write_self_test_tone(os.path.join(folder, "tone.wav"))
    return folder, source_wav


def _write_self_test_tone(path, seconds=3.0, rate=48000):
    import math
    import struct
    import wave
    count = int(seconds * rate)
    with wave.open(path, "w") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(count):
            sample = int(6000 * math.sin(2 * math.pi * 440 * index / rate))
            frames += struct.pack("<hh", sample, sample)
        handle.writeframes(frames)
    return path


def _execute_chain(chain, prepared):
    folder, source_wav = prepared
    try:
        if chain == "polish":
            return _execute_polish(source_wav, folder)
        if chain == "voice_conversion":
            return _execute_voice(source_wav, folder)
        if chain == "transcribe":
            return _execute_transcribe(source_wav, folder)
        raise ValueError("未知自检链")
    except Exception as exc:
        return {"ok": False, "stages": [], "outputs": {}, "reason": "短自检已进入处理，但没有完成：" + str(exc)}


def _execute_polish(source_wav, folder):
    import os
    from core.config import MASTER_PRESETS
    from core.polish_chain import render_polish_files
    from core.stage_operations import StageOperations
    trace = render_polish_files(source_wav, os.path.join(folder, "polish"))
    master_dir = os.path.join(folder, "master")
    reports = os.path.join(folder, "reports")
    stems = os.path.join(folder, "stems")
    os.makedirs(master_dir, exist_ok=True)
    os.makedirs(reports, exist_ok=True)
    os.makedirs(stems, exist_ok=True)
    master = StageOperations()._master(trace["stereo"], master_dir, "selftest", {"wav"}, MASTER_PRESETS["natural"], 48000)
    qc_error = ""
    try:
        StageOperations()._qc(master["wav"], None, "selftest.wav", "polish", "N/A", 0.0, None, MASTER_PRESETS["natural"], None, reports, stems, None, polish_before=trace["source"])
    except RuntimeError as exc:
        qc_error = str(exc)
    master_wav = master["wav"]
    ok = os.path.isfile(master_wav) and os.path.getsize(master_wav) > 0 and not qc_error
    reason = qc_error or "精修短自检已跑 Repair、Tone、Dynamics、Stereo、Master、QC。这不是 GPU 通过，听审未做。"
    return {"ok": ok, "stages": ["Repair", "Tone", "Dynamics", "Stereo", "Master", "QC"], "outputs": {"master": master_wav, "source": trace["source"], "repaired": trace["repaired"], "tone": trace["tone"], "dynamics": trace["dynamics"], "stereo": trace["stereo"]}, "reason": reason}


def _execute_voice(source_wav, folder):
    """HP2 separator, then the installed RVC model, then mix. No job history."""
    import os
    from core.config import get_available_rvc_models
    from core.mixer import mix_stems
    from core.rvc_engine import convert_voice
    from core.separator import separate_audio
    vocal = os.path.join(folder, "vocal.wav")
    instrumental = os.path.join(folder, "instrumental.wav")
    converted = os.path.join(folder, "rvc.wav")
    mixed = os.path.join(folder, "mix.wav")
    separate_audio(source_wav, vocal, instrumental, model_name="HP2_all_vocals.pth", agg=10)
    outputs = {"vocal": vocal, "instrumental": instrumental}
    models = get_available_rvc_models()
    if not models:
        return {"ok": False, "stages": ["Separate"], "outputs": outputs, "reason": "HP2 已跑完，但没有可用的 RVC 模型。听审未做。"}
    try:
        convert_voice(vocal, converted, model_key=next(iter(models)), pitch_shift=0, f0_method="harvest", index_rate=0.65, temp_dir=folder)
    except Exception as exc:
        return {"ok": False, "stages": ["Separate"], "outputs": outputs, "reason": "HP2 已跑完，RVC 没有完成：" + str(exc)}
    outputs["rvc"] = converted
    try:
        mix_stems(converted, instrumental, mixed, add_reverb=False)
    except Exception as exc:
        return {"ok": False, "stages": ["Separate", "RVC"], "outputs": outputs, "reason": "RVC 已跑完，Mix 没有完成：" + str(exc)}
    ok = os.path.isfile(mixed) and os.path.getsize(mixed) > 0
    selected_device = _worker_device(converted + ".worker.json")
    return {
        "ok": ok,
        "inference_ran": True,
        "selected_device": selected_device,
        "device_evidence": {"kind":"worker_recovery",
                            "worker_report_path":converted+".worker.json",
                            "expected_outputs":[converted]},
        "stages": ["Separate", "RVC", "Mix"],
        "outputs": {"vocal": vocal, "instrumental": instrumental, "rvc": converted, "mix": mixed},
        "reason": "换声短自检已跑 Separator、RVC、Mix。听审未做，质量验收仍是 NOT_ASSESSED。",
    }


def _execute_transcribe(source_wav, folder):
    """HTDemucs four-stem separation, then Basic Pitch. No job history."""
    import os
    from core.pipeline_support import _transcribe
    from core.separator import separate_four_stems
    stems = separate_four_stems(source_wav, os.path.join(folder, "separate"))
    vocal = stems.get("vocal") or source_wav
    midi = os.path.join(folder, "notes.mid")
    _transcribe(vocal, midi, bpm=120)
    ok = os.path.isfile(midi) and os.path.getsize(midi) > 0
    selected_device = _worker_device(os.path.join(folder, "separate", "worker.json"))
    return {
        "ok": ok,
        "inference_ran": True,
        "selected_device": selected_device,
        "device_evidence": {"kind":"worker_recovery",
                            "worker_report_path":os.path.join(folder,"separate","worker.json"),
                            "expected_outputs":[value for value in stems.values() if isinstance(value,str)]},
        "stages": ["Separate", "Transcribe"],
        "outputs": {"vocal": vocal, "midi": midi},
        "reason": "扒谱短自检已跑 HTDemucs 和 Basic Pitch。听审未做，质量验收仍是 NOT_ASSESSED。",
    }


def _execute_dual(profile, prepared):
    """Run both frozen MDX models and retain their actual worker outputs."""
    import os
    folder, source_wav = prepared
    from core.separator import separate_dual_audio
    outputs = separate_dual_audio(source_wav, os.path.join(folder, "dual"), device="auto")
    vocal = outputs["kim_vocal"]
    instrumental = outputs["insthq3_instrumental"]
    ok = all(os.path.isfile(outputs[key]) and os.path.getsize(outputs[key]) > 0
             for key in ("kim_vocal", "kim_instrumental", "insthq3_vocal", "insthq3_instrumental"))
    return {
        "ok": ok,
        "inference_ran": True,
        "inference_device": outputs.get("inference_device"),
        "onnx_provider": outputs.get("onnx_provider"),
        "models_execution": outputs.get("models_execution"),
        "device_evidence": {"kind":"dual_mdx","worker_report_path":outputs.get("worker_path")},
        "stages": ["Separate"],
        "outputs": {"vocal": vocal, "instrumental": instrumental, **outputs},
        "reason": "双模型短自检已完成两次 MDX 推理；实际 provider 见 worker 报告。听审未做，质量验收仍是 NOT_ASSESSED。",
    }


def _worker_device(path):
    import json
    import os

    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload.get("selected_device") if isinstance(payload, dict) else None


class FirstRunWizard:
    """Operable first-run steps. Skip does not touch stored jobs."""

    STEPS = ("欢迎", "硬件检测", "三种模式", "组件与安装", "分离路线", "快速自检", "完成")

    def __init__(self, registry, hardware):
        if not isinstance(registry, dict) or not isinstance(hardware, dict):
            raise ValueError("向导必须读取组件注册表和硬件扫描")
        self.registry = registry
        self.hardware = hardware
        self.index = 0
        self.mode = None
        self.separator = "dual"
        self.skipped = False
        self.finished = False
        self.resets_jobs = False
        self.self_test = None

    def next(self):
        if self.index < len(self.STEPS) - 1:
            self.index += 1
        return self.title()

    def back(self):
        if self.index > 0:
            self.index -= 1
        return self.title()

    def skip(self):
        self.skipped = True
        self.resets_jobs = False
        return self.skipped

    def finish(self):
        self.finished = True
        self.resets_jobs = False
        return self.mode

    def select_mode(self, mode):
        if mode not in ("polish", "voice_conversion", "transcribe"):
            raise ValueError("未知模式")
        self.mode = mode
        self.resets_jobs = False
        return self.mode

    def select_separator(self, choice):
        if choice not in ("hp2", "dual"):
            raise ValueError("未知分离路线")
        self.separator = choice
        self.resets_jobs = False
        return self.separator

    def selected_chain(self):
        if self.mode == "voice_conversion" and self.separator == "dual":
            return "dual"
        return self.mode

    def title(self):
        return self.STEPS[self.index]

    def body(self):
        step = self.title()
        if step == "欢迎":
            return "欢迎使用 AI Music Processor。\n它可以整曲精修、AI 换歌手、扒谱分轨。\n下一步查看这台机器的组件注册表。"
        if step == "硬件检测":
            return wizard_report(self.registry, self.hardware)
        if step == "三种模式":
            lines = ["主界面三个模式使用这些真实 Stage。请选一个，快速自检会跑这一条："]
            for mode, label in (("polish", "精修"), ("voice_conversion", "换声"), ("transcribe", "扒谱")):
                mark = "（当前）" if self.mode == mode else ""
                lines.append(label + mark + "：" + " → ".join(mode_stages(mode)))
            if self.mode is None:
                lines.append("还没有选择模式。")
            return "\n".join(lines)
        if step == "组件与安装":
            return (
                "当前没有在线组件源。可以导入同版本 .ampkg 组件包，或选择同版本完整离线目录；由活动托管清单校验后安装或修复，不覆盖原音频或用户模型，不会伪报下载成功。\n"
                + wizard_report(self.registry, self.hardware)
            )
        if step == "分离路线":
            return (
                "新建换声任务默认分别运行 Kim Vocal 2（人声）和 UVR-MDX-NET Inst HQ 3（伴奏）；CPU / CUDA 以执行策略和实际 worker receipt 为准。\n"
                "也可显式选择快速 HP2；旧任务快照继续按历史 HP2 路线处理。\n"
                "双模型权重或模型 metadata 无法验证时，任务停在 Separate，不会自动改跑 HP2。"
            )
        if step == "快速自检":
            if self.self_test is None:
                return "快速自检读取同一份组件注册表。缺权重时不会报 GPU 通过。"
            return "快速自检：" + self.self_test["status"] + "\nGPU 通过：" + ("是" if self.self_test["gpu_pass"] else "否") + "\n" + self.self_test["reason"]
        return "可以打开新手教程，或回到主界面。跳过不会重置已有任务，也不会再强制打开向导。"

    def run_self_test(self, runner=None, work_dir=None, source_wav=None, weight_dir=None):
        self.self_test = run_quick_self_test(
            self.registry,
            chain=self.selected_chain(),
            runner=runner,
            work_dir=work_dir,
            source_wav=source_wav,
            weight_dir=weight_dir,
        )
        self.resets_jobs = False
        return self.self_test


class TutorialSession:
    """Highlight a main-window control. Skip and reopen do not reset jobs."""

    def __init__(self, mode="polish", target_for=None):
        if mode not in ("polish", "voice_conversion", "transcribe"):
            raise ValueError("未知模式")
        self.mode = mode
        self.target_for = target_for
        self.steps = tutorial_steps(mode)
        self.index = 0
        self.skipped = False
        self.completed = False
        self.reopened = False
        self.resets_jobs = False
        self.forces_advanced = False
        self.skippable = True
        self.highlighted = None
        self._marked = None
        self._marked_text = None

    def next(self):
        if self.index < len(self.steps) - 1:
            self.index += 1
        return self.title()

    def back(self):
        if self.index > 0:
            self.index -= 1
        return self.title()

    def skip(self):
        self.skipped = True
        self.resets_jobs = False
        self.clear_highlight()
        return self.skipped

    def complete(self):
        self.completed = True
        self.resets_jobs = False
        self.clear_highlight()
        return self.completed

    def reopen(self):
        self.reopened = True
        self.skipped = False
        self.completed = False
        self.index = 0
        self.resets_jobs = False
        return self.title()

    def set_mode(self, mode):
        title = self.title()
        self.mode = mode
        self.steps = tutorial_steps(mode)
        if title in self.steps:
            self.index = self.steps.index(title)
        elif self.index >= len(self.steps):
            self.index = len(self.steps) - 1
        self.resets_jobs = False
        self.apply_highlight()
        return list(self.steps)

    def title(self):
        return self.steps[self.index]

    def body(self):
        text = {
            "三种模式": "高亮的是当前模式按钮。下面一行会换成该模式真正会跑的 Stage。",
            "质量档": "高亮的是清理预设。换声分离按当前路线设置运行；新建任务默认为 Kim Vocal 2 + Inst HQ 3。",
            "目标歌手": "这一步只在换歌手时出现。高亮的是目标歌手模型。",
            "输出": "高亮的是输出目录。已有任务的目录不会被教程清掉。",
            "试听": "高亮的是试听母带。质量诊断打开已经写出的阶段文件，不把未听审写成验收通过。",
        }
        if self.title() not in text:
            raise ValueError("没有这一步教程")
        return text[self.title()]

    def _widget_for(self, title):
        source = self.target_for
        if source is None:
            return None
        if callable(source):
            return source(title)
        return source.get(title)

    def apply_highlight(self):
        self.clear_highlight()
        widget = self._widget_for(self.title())
        if widget is None:
            self.highlighted = None
            return None
        try:
            current = widget.cget("text")
        except Exception:
            self.highlighted = None
            return None
        self._marked = widget
        self._marked_text = current
        widget.configure(text="● " + current)
        self.highlighted = self.title()
        return widget

    def clear_highlight(self):
        widget = self._marked
        if widget is not None:
            try:
                widget.configure(text=self._marked_text)
            except Exception:
                pass
        self._marked = None
        self._marked_text = None
        self.highlighted = None
