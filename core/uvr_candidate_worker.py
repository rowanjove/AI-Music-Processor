"""Isolated HP2 experiment worker with measured per-band ISTFT lengths.

This candidate path never changes the legacy UVR worker or its output policy.
The trusted ``_audio_pre_._path_audio_`` code is cloned inside this process;
only the clone's ``spec_utils`` global points at a local exact-length proxy.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
RVC_ROOT = os.path.join(PROJECT_ROOT, "tools", "RVC", "RVC-beta-v2-0618")


def run_candidate(input_audio, vocal_out, inst_out, model_path, work_root, evidence_path,
                  *, agg=10, device="auto", window_size=512, tta=False):
    if type(tta) is not bool:raise ValueError('候选 TTA 必须为布尔值')
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    if RVC_ROOT not in sys.path:
        sys.path.insert(0, RVC_ROOT)
    os.chdir(RVC_ROOT)

    import numpy as np
    import soundfile as sf
    import torch

    from core.uvr_exact_length import build_exact_length_spec_proxy, clone_method_with_spec_proxy
    from core.worker_runtime import clear_cuda, resolve_device
    from infer_uvr5 import _audio_pre_, spec_utils

    input_audio = _regular_file(input_audio, "候选 HP2 输入")
    model_path = _regular_file(model_path, "候选 HP2 权重")
    work_root = _existing_work_directory(work_root)
    vocal_out = _output_path(vocal_out, work_root)
    inst_out = _output_path(inst_out, work_root)
    evidence_path = _output_path(evidence_path, work_root)
    _assert_distinct_paths(
        input_audio, model_path, (vocal_out, inst_out, evidence_path)
    )
    _preserve_previous_failed_attempt(evidence_path, (vocal_out, inst_out))
    for path in (vocal_out, inst_out, evidence_path):
        if os.path.lexists(path):
            raise FileExistsError("candidate HP2 output exists without a retryable failed-attempt record: " + path)
    if os.path.normcase(vocal_out) == os.path.normcase(inst_out):
        raise ValueError("候选 HP2 两个输出路径必须不同")
    input_sha_before = _sha256_file(input_audio)
    model_sha_before = _sha256_file(model_path)
    work = tempfile.mkdtemp(prefix="uvr-candidate-stage-", dir=work_root)
    raw_vocal_root = os.path.join(work, "vocal")
    raw_inst_root = os.path.join(work, "instrumental")
    evidence = {
        "schema_version": 1,
        "worker": "uvr_candidate_exact_length",
        "status": "RUNNING",
        "input": {"path": input_audio, "sha256_before": input_sha_before},
        "model": {"path": model_path, "sha256_before": model_sha_before},
        "parameters": {"model_name": "HP2_all_vocals.pth", "agg": int(agg),
                       "device_request": device, "is_half": False,
                       "tta": tta,
                       "window_size": int(window_size), "output_subtype": "PCM_16"},
        "length_adapter": None,
        "outputs": {},
        "errors": [],
        "quality_acceptance": "NOT_ASSESSED",
        "listening_quality": "not_assessed",
    }
    _write_json(evidence_path, evidence)
    try:
        actual_device = resolve_device(device, torch)
        is_half = False
        model_path = os.path.abspath(model_path)
        from core.uvr_trusted_weights import create_trusted_uvr_model
        pre_fun=create_trusted_uvr_model(_audio_pre_,model_path,agg=int(agg),device=actual_device,is_half=is_half)
        evidence['model_guard']=pre_fun.authenticated_checkpoint
        if int(window_size) <= 2 * int(pre_fun.model.offset) or int(window_size) % 32:
            raise ValueError("UVR window_size 必须大于模型双侧裁剪宽度且为32的倍数")
        pre_fun.data["window_size"] = int(window_size)
        pre_fun.data['tta'] = tta
        predict_calls=[0]
        original_predict=pre_fun.model.predict
        def counted_predict(*args,**kwargs):
            predict_calls[0]+=1
            return original_predict(*args,**kwargs)
        pre_fun.model.predict=counted_predict
        proxy, tracker = build_exact_length_spec_proxy(spec_utils, pre_fun.mp, np)
        pre_fun._path_audio_ = clone_method_with_spec_proxy(pre_fun._path_audio_, proxy)
        pre_fun._path_audio_(
            input_audio,
            vocal_root=raw_vocal_root,
            ins_root=raw_inst_root,
            format="wav",
        )
        length_evidence = tracker.finish()
        high_band = max(pre_fun.mp.param["band"])
        expected_frames = next(
            row["source_samples"] for row in length_evidence["band_source_lengths"]
            if row["band"] == high_band
        )
        if expected_frames <= 0:
            raise RuntimeError("HP2 high band 未产生有效 source length")
        vocal_raw = _one_wav(raw_vocal_root, "vocal_")
        inst_raw = _one_wav(raw_inst_root, "instrument_")
        vocal_info = _validate_output(vocal_raw, expected_frames, int(pre_fun.mp.param["sr"]), sf, np)
        inst_info = _validate_output(inst_raw, expected_frames, int(pre_fun.mp.param["sr"]), sf, np)
        _copy_atomic(vocal_raw, vocal_out)
        _copy_atomic(inst_raw, inst_out)
        evidence["status"] = "EXECUTED"
        evidence["selected_device"] = str(actual_device)
        evidence["input"]["sha256_after"] = _sha256_file(input_audio)
        evidence["model"]["sha256_after"] = _sha256_file(model_path)
        if evidence["input"]["sha256_after"] != input_sha_before:
            raise RuntimeError("候选 HP2 执行期间输入文件发生变化")
        if evidence["model"]["sha256_after"] != model_sha_before:
            raise RuntimeError("候选 HP2 执行期间模型权重发生变化")
        evidence["length_adapter"] = length_evidence
        evidence['band_initialization']='zero_initialized_candidate_local'
        evidence['model_predict_calls']=predict_calls[0]
        evidence["output_length_contract"] = {
            "sample_rate": int(pre_fun.mp.param["sr"]),
            "frames": expected_frames,
            "source_band": int(high_band),
            "source_band_samples": expected_frames,
            "padding_or_trimming_after_istft": False,
        }
        evidence["outputs"] = {
            "vocal": dict(vocal_info, path=vocal_out, sha256=_sha256_file(vocal_out)),
            "instrumental": dict(inst_info, path=inst_out, sha256=_sha256_file(inst_out)),
        }
        _write_json(evidence_path, evidence)
        return evidence
    except Exception as exc:
        evidence["status"] = "FAILED"
        evidence["failed_work_dir"] = work
        evidence["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        evidence["input"].setdefault("sha256_after", _sha256_file(input_audio))
        evidence["model"].setdefault("sha256_after", _sha256_file(model_path))
        try:
            evidence["length_adapter"] = tracker.partial_evidence()
        except Exception:
            pass
        _write_json(evidence_path, evidence)
        raise
    finally:
        try:
            del pre_fun
        except UnboundLocalError:
            pass
        clear_cuda(torch)
        if evidence.get("status") == "EXECUTED":
            shutil.rmtree(work, ignore_errors=True)


def _validate_output(path, expected_frames, expected_rate, sf, numpy_module):
    info = sf.info(path)
    if info.format != "WAV" or info.channels != 2 or info.samplerate != expected_rate:
        raise RuntimeError("candidate HP2 输出必须是双声道 WAV @{} Hz".format(expected_rate))
    if info.frames != expected_frames:
        raise RuntimeError("candidate HP2 输出帧数不符：{} != {}".format(info.frames, expected_frames))
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    if rate != expected_rate or data.shape != (expected_frames, 2) or not numpy_module.isfinite(data).all():
        raise RuntimeError("candidate HP2 输出音频内容无效")
    return {
        "frames": int(info.frames),
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "subtype": info.subtype,
        "finite": True,
    }


def _one_wav(folder, prefix):
    matches = [os.path.join(folder, name) for name in os.listdir(folder)
               if name.lower().startswith(prefix) and name.lower().endswith(".wav")]
    if len(matches) != 1:
        raise RuntimeError("candidate HP2 未得到唯一 {} WAV".format(prefix))
    return _regular_file(matches[0], "candidate HP2 临时输出")


def _regular_file(path, label):
    target = os.path.abspath(os.fspath(path))
    if os.path.islink(target) or not os.path.isfile(target):
        raise FileNotFoundError(label + " 不存在或不是普通文件：" + target)
    return target


def _existing_work_directory(path):
    target = os.path.abspath(os.fspath(path))
    if os.path.islink(target) or not os.path.isdir(target):
        raise FileNotFoundError("candidate HP2 work directory 不存在或不是普通目录：" + target)
    return target


def _output_path(path, work_root):
    target = os.path.abspath(os.fspath(path))
    if os.path.islink(target):
        raise ValueError("candidate HP2 输出不能是符号链接")
    try:
        if os.path.commonpath((work_root, target)) != work_root:
            raise ValueError("candidate HP2 输出必须位于本次 work directory")
    except ValueError:
        raise ValueError("candidate HP2 输出必须位于本次 work directory")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise FileNotFoundError("candidate HP2 输出目录不存在：" + parent)
    return target


def _assert_distinct_paths(input_audio, model_path, output_paths):
    named = [("input", input_audio), ("model", model_path)] + [
        ("output_{}".format(index), path) for index, path in enumerate(output_paths)
    ]
    for index, (left_label, left_path) in enumerate(named):
        left = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(left_path))))
        for right_label, right_path in named[index + 1:]:
            right = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(right_path))))
            aliased = left == right
            if not aliased:
                try:
                    aliased = os.path.samefile(left_path, right_path)
                except OSError:
                    aliased = False
            if aliased:
                raise ValueError(
                    "candidate HP2 input/model/output paths must be distinct: {} and {}".format(
                        left_label, right_label
                    )
                )


def _preserve_previous_failed_attempt(evidence_path, output_paths):
    if not os.path.lexists(evidence_path):
        return
    if os.path.islink(evidence_path) or not os.path.isfile(evidence_path):
        raise FileExistsError("candidate HP2 retry evidence is not a regular file")
    try:
        with open(evidence_path, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError) as exc:
        raise FileExistsError("candidate HP2 refuses to overwrite unreadable retry evidence") from exc
    if previous.get("worker") != "uvr_candidate_exact_length" or previous.get("status") not in ("FAILED", "RUNNING"):
        raise FileExistsError("candidate HP2 retry evidence does not authorize a new attempt")
    attempt_tag = "previous-{}".format(os.getpid())
    for path in tuple(output_paths) + (evidence_path,):
        if not os.path.lexists(path):
            continue
        if os.path.islink(path) or not os.path.isfile(path):
            raise FileExistsError("candidate HP2 refuses to move a non-file retry artifact")
        backup = path + "." + attempt_tag
        if os.path.lexists(backup):
            raise FileExistsError("candidate HP2 retry evidence archive already exists: " + backup)
        os.replace(path, backup)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomic(source, target):
    temporary = target + ".part"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _write_json(path, value):
    temporary = path + ".part"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--vocal-out", required=True)
    parser.add_argument("--inst-out", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--agg", type=int, default=10)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--window-size", type=int, default=512)
    parser.add_argument('--tta',action='store_true',help='Independent candidate time-shift averaging; never promotes a Job backend')
    args = parser.parse_args(argv)
    try:
        result = run_candidate(
            args.input, args.vocal_out, args.inst_out, args.model_path,
            args.work_root, args.evidence, agg=args.agg, device=args.device,
            window_size=args.window_size, tta=args.tta,
        )
        print(json.dumps({"worker": result["worker"], "status": result["status"],
                          "selected_device": result.get("selected_device")}, ensure_ascii=False), flush=True)
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
