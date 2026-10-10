"""Durable, resumable audio production pipeline.

The pipeline owns orchestration and durable state. DSP and external tools remain
in their small modules, which keeps every stage replaceable in tests or addons.
"""

from __future__ import annotations

import datetime
import hashlib
import threading
from contextlib import ExitStack
from functools import wraps
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from core.analyzer import probe_metadata, standardize_to_48k_float
from core.config import ADDONS_DIR, CLEAN_PRESETS, FFMPEG_EXE, OUTPUT_DIR, MASTER_PRESETS, RVC_ROOT, UVR_ROOT, APP_VERSION, get_available_rvc_models
from core.mixer import mix_stems
from core.rvc_engine import convert_voice
from core.storage import JobBusyError, JobOwnershipError, JobStore, canonical_json, sha256_file, sha256_path, stage_cache_key

try:
    from core.processes import ProcessCancelled, process_scope, run as process_run
except ImportError:
    class ProcessCancelled(RuntimeError):
        """Raised when the user cancels a running job."""

    class _NoopScope:
        def __init__(self, _control: Any = None) -> None:
            pass

        def __enter__(self) -> "_NoopScope":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    def process_scope(control: Any = None) -> _NoopScope:
        return _NoopScope(control)

    def process_run(cmd: list[str], **kwargs: Any) -> Any:
        return subprocess.run(cmd, check=kwargs.pop("check", True), **kwargs)


if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


from core.pipeline_support import (sanitize_filename, safe_log, _json_file, _existing_path,
    _output_paths, _hash_inputs, _validate_outputs, _hash_outputs, _infer_project_root,
    _relocate_cached_outputs, _copy_if_file, _normalise_stems, separate_four_stems, _transcribe, _control_check)

from core.stage_operations import StageOperations

_CPU_ETA_STAGES = {
    "Ingest", "SourceClean", "Analyze", "Clean", "Repair", "Tone", "Dynamics",
    "Stereo", "Align", "Mix", "Master", "QC", "PitchCorrection", "MIDICleanup",
}

class AudioPipeline(StageOperations):
    def __init__(
        self,
        progress_callback: Optional[Callable[[str, float, str], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        storage: Optional[JobStore] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self.progress_callback = progress_callback or (lambda stage, pct, msg: None)
        self.log_callback = log_callback or safe_log
        self.storage = storage or JobStore(db_path)
        self._active_control: Any = None
        self._execution_lock = threading.Lock()
        self._completed_progress = 0.0
        self._eta_audio_duration: Optional[float] = None
        self._eta_runtime_sha256: Optional[str] = None
        self._eta_runtime_checked = False
        self._eta_history_index: Optional[dict[tuple[str, str, str], list[dict[str, Any]]]] = None

    def log(self, msg: str) -> None:
        self.log_callback(msg)

    def set_progress(self, stage: str, pct: float, msg: str) -> None:
        self.log(f"[{stage}] ({int(pct * 100)}%) {msg}")
        self.progress_callback(stage, pct, msg)

    @staticmethod
    def _cache_parameters(stage_name: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        from core.cache_fingerprint import enrich
        return enrich(stage_name, parameters)

    @staticmethod
    def _without_eta_metadata(parameters: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in parameters.items()
                if not str(key).startswith("_eta_") and key != "_input_manifest"}

    @staticmethod
    def _input_manifest(values: Any) -> Any:
        """Retain verifiable file references alongside the cache input hashes."""
        if isinstance(values, Mapping):
            return {str(key): AudioPipeline._input_manifest(item)
                    for key, item in sorted(values.items(), key=lambda pair: str(pair[0]))}
        if isinstance(values, (list, tuple)):
            return [AudioPipeline._input_manifest(item) for item in values]
        path = _existing_path(values)
        if path:
            return {"_stage_input_file": True, "path": path, "sha256": sha256_path(path)}
        return values

    @staticmethod
    def _input_hash_data(manifest: Any) -> Any:
        if isinstance(manifest, Mapping):
            if manifest.get("_stage_input_file") is True and set(manifest) == {"_stage_input_file", "path", "sha256"}:
                return {"sha256": manifest["sha256"], "kind": "file"}
            return {str(key): AudioPipeline._input_hash_data(item)
                    for key, item in sorted(manifest.items(), key=lambda pair: str(pair[0]))}
        if isinstance(manifest, (list, tuple)):
            return [AudioPipeline._input_hash_data(item) for item in manifest]
        return manifest

    def _eta_identity(self, stage_name: str, parameters: Mapping[str, Any]) -> Optional[dict[str, str]]:
        if stage_name not in _CPU_ETA_STAGES:
            return None
        dependencies = parameters.get("dependency_hashes")
        cpu = __import__("platform").processor().strip()
        if not isinstance(dependencies, dict) or not dependencies or not cpu or not os.path.isfile(sys.executable):
            return None
        if not self._eta_runtime_checked:
            self._eta_runtime_checked = True
            try:
                self._eta_runtime_sha256 = sha256_file(sys.executable)
            except OSError:
                self._eta_runtime_sha256 = None
        runtime_sha = self._eta_runtime_sha256
        if not runtime_sha:
            return None
        dependency_sha = hashlib.sha256(canonical_json(dependencies).encode("utf-8")).hexdigest()
        profile_sha = hashlib.sha256(canonical_json(dict(parameters)).encode("utf-8")).hexdigest()
        return {
            "backend": "pipeline-%s:%s" % (stage_name, dependency_sha),
            "hardware": "CPU:%s:%s" % (__import__("platform").machine(), cpu),
            "model_sha256": "none",
            "runtime_sha256": runtime_sha,
            "profile_sha256": profile_sha,
        }

    def _eta_history(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        history = []
        for job in self.storage.list_jobs(1000):
            if mode is not None and job.get("mode") != mode:
                continue
            try:
                stages = self.storage.list_stages(job["id"])
            except Exception:
                continue
            for stage in stages:
                parameters = stage.get("parameters") or {}
                identity = parameters.get("_eta_identity")
                if not isinstance(identity, dict):
                    continue
                history.append({
                    "status": stage.get("status"), "mode": job.get("mode"),
                    "stage_name": stage.get("stage_name"),
                    "audio_duration": parameters.get("_eta_audio_duration"),
                    "started_at": stage.get("started_at"),
                    "finished_at": stage.get("finished_at"),
                    "execution_kind": parameters.get("_eta_execution_kind"),
                    "cache_hit": parameters.get("_eta_execution_kind") == "cache_hit",
                    "identity": identity,
                })
        return history

    def _eta_history_for(self, mode: str, stage_name: str,
                         identity: Optional[dict[str, str]]) -> list[dict[str, Any]]:
        if not isinstance(identity, dict):
            return []
        if self._eta_history_index is None:
            index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for row in self._eta_history(mode):
                row_identity = row.get("identity")
                if (row.get("status") != "DONE" or row.get("execution_kind") != "executed" or
                        not isinstance(row_identity, dict) or row.get("stage_name") not in _CPU_ETA_STAGES):
                    continue
                key = (str(row.get("mode")), str(row.get("stage_name")), canonical_json(row_identity))
                index.setdefault(key, []).append(row)
            self._eta_history_index = index
        key = (mode, stage_name, canonical_json(identity))
        return self._eta_history_index.get(key, [])

    def _remember_eta_sample(self, mode: Optional[str], stage_name: str,
                             parameters: Mapping[str, Any], started_at: str,
                             finished_at: str) -> None:
        if self._eta_history_index is None or not isinstance(mode, str):
            return
        identity = parameters.get("_eta_identity")
        if not isinstance(identity, dict) or parameters.get("_eta_execution_kind") != "executed":
            return
        row = {
            "status": "DONE", "mode": mode, "stage_name": stage_name,
            "audio_duration": parameters.get("_eta_audio_duration"),
            "started_at": started_at, "finished_at": finished_at,
            "execution_kind": "executed", "cache_hit": False,
            "identity": identity,
        }
        key = (mode, stage_name, canonical_json(identity))
        self._eta_history_index.setdefault(key, []).append(row)

    def _begin_eta_run(self) -> None:
        # Runtime identity and prior timings are scoped to one process_file run:
        # changes installed between jobs are observed on the next run.
        self._eta_runtime_sha256 = None
        self._eta_runtime_checked = False
        self._eta_history_index = None

    @staticmethod
    def _eta_suffix(result: Mapping[str, Any]) -> str:
        seconds = result.get("seconds")
        if seconds is None:
            return ""
        bounds = result.get("range_seconds") or [seconds, seconds]
        return "；本阶段预计剩余约 %.0f 秒（实测范围 %.0f–%.0f 秒，%d 个匹配样本）" % (
            seconds, bounds[0], bounds[1], result.get("sample_count", 0))

    def _record_skipped_stage(self, job_id: str, stage_name: str, inputs: Any,
                              parameters: Mapping[str, Any], reason: str,
                              progress: float) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A skipped stage needs a concrete reason")
        self.storage.assert_owner(job_id)
        effective_parameters = self._cache_parameters(stage_name, parameters)
        input_manifest = self._input_manifest(inputs)
        input_hash = canonical_json(self._input_hash_data(input_manifest))
        existing = self.storage.get_stage(job_id, stage_name)
        existing_parameters = self._without_eta_metadata((existing or {}).get("parameters") or {})
        expected_parameters = dict(effective_parameters, skip_reason=reason)
        if (existing and existing.get("status") == "SKIPPED" and
                existing.get("input_hash") == input_hash and
                existing_parameters == expected_parameters and existing.get("log") == reason):
            self.set_progress(stage_name, progress, "跳过：" + reason)
            return {"skipped": True, "reason": reason}
        attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
        recorded = dict(effective_parameters, skip_reason=reason, _input_manifest=input_manifest,
                        _eta_execution_kind="skipped")
        finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.storage.upsert_stage(
            job_id, stage_name, status="SKIPPED", input_hash=input_hash,
            parameters=recorded, log=reason, finished_at=finished, attempt=attempt,
        )
        self.set_progress(stage_name, progress, "跳过：" + reason)
        self._completed_progress = max(self._completed_progress, progress)
        return {"skipped": True, "reason": reason}

    def _run_stage(
        self,
        job_id: str,
        stage_name: str,
        inputs: Any,
        parameters: Mapping[str, Any],
        runner: Callable[[], Any],
        progress: float,
    ) -> Any:
        self.storage.assert_owner(job_id)
        effective_parameters = self._cache_parameters(stage_name, parameters)
        input_manifest = self._input_manifest(inputs)
        input_hash_data = self._input_hash_data(input_manifest)
        job_record = self.storage.get_job(job_id) or {}
        eta_duration = self._eta_audio_duration
        eta_identity = self._eta_identity(stage_name, effective_parameters)
        eta_mode = job_record.get("mode")
        eta_history = (self._eta_history_for(eta_mode, stage_name, eta_identity)
                       if eta_identity and eta_duration and eta_mode else [])

        def _stage_record_parameters(execution_kind):
            recorded = dict(effective_parameters, _input_manifest=input_manifest)
            recorded["_eta_execution_kind"] = execution_kind
            if eta_identity:
                recorded["_eta_identity"] = eta_identity
            if type(eta_duration) in (int, float) and math.isfinite(eta_duration) and eta_duration > 0:
                recorded["_eta_audio_duration"] = float(eta_duration)
            return recorded

        def _eta_suffix(elapsed, fraction=None):
            from core.eta_estimator import estimate_remaining
            result = estimate_remaining(
                eta_history, eta_duration if eta_duration else 1.0,
                eta_mode or "unknown", stage_name, eta_identity,
                elapsed_seconds=max(0.0, elapsed), stage_fraction=fraction,
            )
            if not eta_duration:
                result["seconds"] = None
                result["basis"] = "duration_unknown"
            return self._eta_suffix(result)

        if stage_name in {"Align", "QC"} and job_record.get("project_dir"):
            # Their JSON/report payloads contain project-local paths and must be
            # regenerated for a different project even when audio hashes match.
            effective_parameters["project_dir"] = os.path.abspath(job_record["project_dir"])
        input_hash = canonical_json(input_hash_data)
        cache_key = stage_cache_key(stage_name, input_hash_data, effective_parameters)
        try:
            _control_check(self._active_control)
        except ProcessCancelled:
            self.storage.upsert_stage(job_id, stage_name, status="CANCELLED", input_hash=input_hash, parameters=effective_parameters, error="cancelled")
            self.storage.update_job(job_id, status="CANCELLED", current_stage=stage_name, error="cancelled")
            raise
        existing = self.storage.get_stage(job_id, stage_name)
        if existing and existing.get("status") == "DONE" and existing.get("input_hash") == input_hash and self._without_eta_metadata(existing.get("parameters") or {}) == effective_parameters and _validate_outputs(existing.get("outputs"), existing.get("output_hashes")):
            with self.storage.transaction():
                self.storage.touch_stage(job_id,stage_name)
                for key,output_path in _output_paths(existing['outputs']).items():
                    self.storage.register_output(job_id,output_path,kind=stage_name,file_hash=existing['output_hashes'].get(key))
            self.set_progress(stage_name, progress, "恢复已完成 Stage")
            self._completed_progress = progress
            return existing["outputs"]
        outputs = None
        # Metadata files embed job-specific paths; rebuild these for a new project.
        cached = self.storage.get_cache(cache_key) if stage_name not in {'Analyze', 'Align', 'QC', 'RVCPost', 'RVCSearch'} else None
        if cached:
            source_root = _infer_project_root(list(_output_paths(cached.get("outputs")).values()))
            source_job = self.storage.project_job_id(source_root) if source_root else None
            try:
                with ExitStack() as cache_claim:
                    if source_job and source_job != job_id:
                        cache_claim.enter_context(self.storage.job_lock(source_job))
                    if _validate_outputs(cached.get("outputs"), cached.get("output_hashes")):
                        hashes = cached["output_hashes"]
                        outputs = _relocate_cached_outputs(cached["outputs"], source_root, job_record.get("project_dir"))
                        if not _validate_outputs(outputs, hashes): outputs = None
                        if outputs is not None: self.storage.touch_cache(cache_key)
            except JobBusyError:
                outputs = None  # Active source project is never borrowed or collected.
            except (OSError, ValueError) as error:
                outputs = None
                self.log('Stage 缓存复制失败，重新执行本阶段：' + str(error))
        if cached and outputs is not None:
            with self.storage.transaction():
                self.storage.upsert_stage(job_id, stage_name, status="DONE", input_hash=input_hash, outputs=outputs, output_hashes=hashes, parameters=_stage_record_parameters("cache_hit"), log="cache hit", finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                for key, output_path in _output_paths(outputs).items():
                    self.storage.register_output(job_id, output_path, kind=stage_name, file_hash=hashes.get(key))
            self.set_progress(stage_name, progress, "命中 Stage 缓存")
            self._completed_progress = progress
            return outputs

        attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        started_monotonic = time.monotonic()
        recorded_parameters = _stage_record_parameters("executed")
        self.storage.upsert_stage(job_id, stage_name, status="RUNNING", input_hash=input_hash, parameters=recorded_parameters, started_at=started, attempt=attempt)
        stage_start = min(progress, self._completed_progress)
        self.storage.update_job(job_id, status="RUNNING", current_stage=stage_name, progress=stage_start, error=None)
        self.set_progress(stage_name, stage_start, "开始处理" + _eta_suffix(0.0))
        try:
            _control_check(self._active_control)
            from core.processes import worker_progress_scope
            def worker_progress(event):
                fraction = event["fraction"]
                overall = stage_start + (progress - stage_start) * fraction
                count = ""
                if type(event.get("processed")) is int and type(event.get("total")) is int and event["total"] > 0:
                    count = f"（{event['processed']}/{event['total']}）"
                elapsed = max(0.0, time.monotonic() - started_monotonic)
                message = f"阶段 {fraction * 100:.1f}%{count} {event.get('message', '')}"
                self.set_progress(stage_name, overall, message + _eta_suffix(elapsed, fraction))
            with worker_progress_scope(worker_progress):
                outputs = runner()
            # A worker must not publish cache entries under pre-run hashes if
            # its source, model, implementation or catalog changed mid-stage.
            current_parameters = self._cache_parameters(stage_name, parameters)
            if stage_name in {"Align", "QC"} and job_record.get("project_dir"):
                current_parameters["project_dir"] = os.path.abspath(job_record["project_dir"])
            if (current_parameters != effective_parameters or
                    self._input_hash_data(self._input_manifest(inputs)) != input_hash_data):
                raise RuntimeError(f"Stage {stage_name} 执行期间输入或依赖发生变化，拒绝提交缓存")
            output_hashes = _hash_outputs(outputs)
            if not _validate_outputs(outputs, output_hashes):
                raise RuntimeError(f"Stage {stage_name} 未生成完整产物")

            is_degraded = bool(isinstance(outputs, dict) and (outputs.get("degraded") or outputs.get("status") == "BYPASSED_BY_ERROR"))
            stage_warning = outputs.get("warning") if isinstance(outputs, dict) else None
            if is_degraded:
                self.log(f"Stage {stage_name} 降级运行（已保留原音频，警告: {stage_warning}）")
            elif stage_warning:
                self.log(f"Stage {stage_name} 产生警告: {stage_warning}")

            finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with self.storage.transaction():
                self.storage.upsert_stage(job_id, stage_name, status="DONE", input_hash=input_hash, outputs=outputs, output_hashes=output_hashes, parameters=recorded_parameters, started_at=started, finished_at=finished, attempt=attempt)
                if stage_name != 'RVCSearch' and not is_degraded:
                    self.storage.put_cache(cache_key, stage_name, input_hash, effective_parameters, outputs, output_hashes)
                for key, output_path in _output_paths(outputs).items():
                    self.storage.register_output(job_id, output_path, kind=stage_name, file_hash=output_hashes.get(key))
            self._remember_eta_sample(eta_mode, stage_name, recorded_parameters, started, finished)
            self.set_progress(stage_name, progress, "完成；阶段 100%")
            self._completed_progress = progress
            return outputs
        except ProcessCancelled:
            self.storage.upsert_stage(job_id, stage_name, status="CANCELLED", input_hash=input_hash, parameters=recorded_parameters, started_at=started, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), error="cancelled", attempt=attempt)
            self.storage.update_job(job_id, status="CANCELLED", current_stage=stage_name, progress=progress, error="cancelled")
            raise
        except Exception as exc:
            self.storage.upsert_stage(job_id, stage_name, status="FAILED", input_hash=input_hash, parameters=recorded_parameters, started_at=started, finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), error=f"{type(exc).__name__}: {exc}", attempt=attempt)
            self.storage.update_job(job_id, status="FAILED", current_stage=stage_name, progress=progress, error=f"{type(exc).__name__}: {exc}")
            raise

    @staticmethod
    def _normalise_formats(output_formats: Optional[list[str] | tuple[str, ...] | str]) -> set[str]:
        if output_formats is None:
            formats = {"wav", "mp3"}
        elif isinstance(output_formats, str):
            formats = {item.strip().lower().lstrip(".") for item in output_formats.split(",") if item.strip()}
        else:
            formats = {str(item).lower().lstrip(".") for item in output_formats}
        formats.add("wav")
        return formats

    @staticmethod
    def _validate_parameters(mode: str, clean_preset: str, master_preset: str, rvc_model_key: str, pitch_shift: int, f0_method: str, index_rate: float, formats: set[str], output_sample_rate: int, bpm: int, check_model: bool = True) -> None:
        if mode not in {"polish", "voice_conversion", "transcribe"}:
            raise ValueError(f"不支持的处理模式: {mode}")
        if clean_preset not in CLEAN_PRESETS:
            raise ValueError(f"不支持的清理预设: {clean_preset}")
        if master_preset not in MASTER_PRESETS:
            raise ValueError(f"不支持的母带预设: {master_preset}")
        if formats - {"wav", "mp3", "flac"}:
            raise ValueError(f"不支持的输出格式: {sorted(formats - {'wav', 'mp3', 'flac'})}")
        if output_sample_rate not in {44100, 48000}:
            raise ValueError("输出采样率仅支持 44100 或 48000 Hz")
        if not -24 <= int(pitch_shift) <= 24:
            raise ValueError("音高偏移必须在 -24 到 24 半音之间")
        if not 0.0 <= float(index_rate) <= 1.0:
            raise ValueError("index_rate 必须在 0 到 1 之间")
        from core.f0_method import resolve_f0_method
        resolve_f0_method(f0_method)
        if not 30 <= float(bpm) <= 300:
            raise ValueError("BPM 必须在 30 到 300 之间")
        if mode == "voice_conversion" and not str(rvc_model_key).strip():
            raise ValueError("换声模式必须选择 RVC 模型")
        if mode == "voice_conversion" and check_model:
            try:
                available = get_available_rvc_models()
                if rvc_model_key not in available:
                    raise ValueError(f"未找到 RVC 模型: {rvc_model_key}")
            except ValueError:
                raise
            except Exception:
                pass

    @staticmethod
    def _disk_preflight(input_file: str, output_root: str) -> None:
        os.makedirs(output_root, exist_ok=True)
        try:
            metadata = probe_metadata(input_file)
        except Exception:
            # Encrypted Suno M4A is decoded by Ingest; an ffprobe failure here
            # must not reject it before the decryptor gets a chance to run.
            metadata = {"duration": 0.0}
        duration = float(metadata.get("duration") or 0.0)
        estimate = max(os.path.getsize(input_file) * 12, duration * 48000 * 2 * 4 * 12, 64 * 1024 * 1024)
        free = shutil.disk_usage(output_root).free
        if free < estimate:
            raise OSError(f"输出磁盘空间不足：至少需要约 {estimate / 1024 ** 3:.2f} GB，可用 {free / 1024 ** 3:.2f} GB")

    def _cleanup_generated_midi(self, job_id: str, midi_path: str, midi_dir: str,
                               reports_dir: str, params: Mapping[str, Any],
                               progress: float) -> dict[str, Any]:
        # Older frozen jobs did not carry a cleanup policy; keep their
        # historical no-stage behavior. New jobs freeze an explicit policy,
        # so an Off choice is persisted as SKIPPED rather than disappearing.
        if "midi_cleanup" not in params:
            return {"midi": midi_path}
        from core.midi_cleanup_options import normalize_midi_cleanup
        options = normalize_midi_cleanup(params.get("midi_cleanup"))
        if not options["enabled"]:
            self._record_skipped_stage(
                job_id, "MIDICleanup", {"midi": midi_path}, options,
                "用户在任务参数中关闭了 MIDI cleanup", progress,
            )
            return {"midi": midi_path}
        output_path = os.path.join(midi_dir, os.path.splitext(os.path.basename(midi_path))[0] + "_cleaned.mid")
        report_path = os.path.join(reports_dir, "midi_cleanup.json")
        stage_parameters = {key: value for key, value in options.items() if key != "enabled"}

        def _run_cleanup():
            from core.midi_cleanup import cleanup_midi_file
            result = cleanup_midi_file(
                midi_path, output_path,
                quantize=options["quantize"],
                grid_division=options["grid_division"],
                merge_overlaps=options["merge_overlaps"],
                velocity_range=options["velocity_range"],
                report_path=report_path,
                cancel=lambda: _control_check(self._active_control),
            )
            return {"midi": result["midi"], "decision_path": result["decision"]}

        return self._run_stage(
            job_id, "MIDICleanup", {"midi": midi_path}, stage_parameters,
            _run_cleanup, progress,
        )

    @staticmethod
    def _select_transcribe_stem(separate: Mapping[str, Any], stem: str) -> str:
        """Return a real Separate output, never a vocal-clean fallback."""
        from core.job_spec import validate_transcribe_stem
        try:
            validate_transcribe_stem(stem)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        selected = _existing_path(separate.get(stem))
        if not selected:
            raise RuntimeError(
                "扒谱所需的真实分轨缺失：%s；拒绝改用人声或混合音轨" % stem
            )
        return selected

    def _process_file(
        self,
        input_file: str,
        mode: str = "voice_conversion",
        clean_preset: str = "light",
        master_preset: str = "natural",
        rvc_model_key: str = "zhoujielun",
        pitch_shift: int = 0,
        f0_method: str = "harvest",
        index_rate: float = 0.65,
        custom_output_dir: Optional[str] = None,
        resume_job_id: Optional[str] = None,
        generate_midi: bool = False,
        output_formats: Optional[list[str] | tuple[str, ...] | str] = None,
        output_sample_rate: int = 48000,
        control: Any = None,
        bpm: int = 120,
        dispatch_id: Optional[str] = None,
        rvc_input_gain_db: Optional[float] = None,
        rvc_input_headroom_strategy: Optional[str] = None,
        skip_vocal_eq: Optional[bool] = None,
        rvc_postprocess: Optional[bool] = None,
        skip_mix_compressor: Optional[bool] = None,
        quality_assist: Optional[bool] = None,
        quality_retry_limit: Optional[int] = None,
        parameter_search_enabled: Optional[bool] = None,
        parameter_search_budget: Optional[str] = None,
        vocal_restoration_effect: Optional[str] = None,
        vocal_restore_profile: Optional[dict] = None,
        pitch_correction_mode: Optional[str] = None,
        pitch_correction_key: Optional[str] = None,
        source_hint: Optional[str] = None,
        source_preclean_enabled: Optional[bool] = None,
        separator_backend: Optional[str] = None,
        separator_profile: Optional[dict] = None,
        polish_profile: Optional[str] = None,
        polish_reference: Optional[str] = None,
        polish_options: Optional[dict] = None,
        midi_cleanup: Optional[dict] = None,
        mix_ducking: Optional[bool] = None,
        mix_reverb: Optional[bool] = None,
        device: str = "auto",
        performance_profile: str = "balanced",
        rvc_postprocess_profile: str = "off",
        adaptive_mix: bool = False,
        mix_bus_profile: str = "off",
        input_channel_mode: str = "auto",
        output_channels: int = 2,
        detailed_analysis: bool = True,
        mix_vocal_gain_db: float = 0.5,
        mix_instrumental_gain_db: float = -1.0,
        transcribe_stem: str = "vocal",
    ) -> dict[str, Any]:
        input_file = os.path.abspath(os.fspath(input_file))
        if not os.path.isfile(input_file) and not resume_job_id:
            raise FileNotFoundError(input_file)
        if mode not in {"polish", "voice_conversion", "transcribe"}:
            raise ValueError(f"不支持的处理模式: {mode}")
        from core.job_spec import validate_mix_gain_db, validate_transcribe_stem
        validate_mix_gain_db(mix_vocal_gain_db, "mix_vocal_gain_db")
        validate_mix_gain_db(mix_instrumental_gain_db, "mix_instrumental_gain_db")
        validate_transcribe_stem(transcribe_stem)
        start_time = time.time()
        self._active_control = control
        self._completed_progress = 0.0
        self._eta_audio_duration = None
        job_id: Optional[str] = None
        claims = ExitStack()
        try:
            with process_scope(control):
                if resume_job_id:
                    job = self.storage.get_job(resume_job_id)
                    if not job:
                        raise KeyError(f"找不到可恢复任务: {resume_job_id}")
                    claims.enter_context(self.storage.execution(resume_job_id))
                    job_id = resume_job_id
                    project_dir = job.get("project_dir")
                    if not project_dir:
                        raise RuntimeError(f"任务 {resume_job_id} 缺少项目目录，无法恢复")
                    project_dir = os.path.abspath(project_dir)
                    from core.job_spec import validate
                    saved = validate(job.get("parameters"), job["mode"])
                    params = saved
                    mode = job["mode"]
                    input_file = os.path.abspath(job["source_path"])
                    source_project = _infer_project_root([input_file])
                    if source_project and os.path.normcase(source_project) != os.path.normcase(project_dir):
                        claims.enter_context(self.storage.project_lock(source_project))
                    if job.get("source_hash") and sha256_path(input_file) != job["source_hash"]:
                        self.storage.update_job(resume_job_id, status="FAILED", error="源文件 hash 已变化，拒绝恢复旧任务")
                        raise RuntimeError("源文件 hash 已变化，不能恢复旧任务；请创建新任务")
                    clean_preset = saved.get("clean_preset", clean_preset)
                    master_preset = saved.get("master_preset", master_preset)
                    rvc_model_key = saved.get("rvc_model_key", rvc_model_key)
                    pitch_shift = saved.get("pitch_shift", pitch_shift)
                    f0_method = saved.get("f0_method", f0_method)
                    index_rate = saved.get("index_rate", index_rate)
                    generate_midi = saved.get("generate_midi", generate_midi)
                    output_formats = saved.get("output_formats", output_formats)
                    output_sample_rate = saved.get("output_sample_rate", output_sample_rate)
                    bpm = saved.get("bpm", bpm)
                    # Do not insert these defaults into an old snapshot.  They
                    # are local compatibility values until a new rerun is made.
                    mix_vocal_gain_db = saved.get("mix_vocal_gain_db", 0.5)
                    mix_instrumental_gain_db = saved.get("mix_instrumental_gain_db", -1.0)
                    transcribe_stem = saved.get("transcribe_stem", "vocal")
                else:
                    source_project = _infer_project_root([input_file])
                    if source_project:
                        claims.enter_context(self.storage.project_lock(source_project))
                    if not os.path.isfile(input_file): raise FileNotFoundError(input_file)
                    raw_name = os.path.splitext(os.path.basename(input_file))[0]
                    clean_title = re.sub(r"_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", "", raw_name, flags=re.I)
                    clean_title = re.sub(r"_[0-9a-f]{8}$", "", clean_title, flags=re.I)
                    clean_title = sanitize_filename(clean_title) or f"AI_Music_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    output_root = os.path.abspath(custom_output_dir or OUTPUT_DIR)
                    project_dir = os.path.join(output_root, f"{clean_title}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}")
                    formats = self._normalise_formats(output_formats)
                    params = {"clean_preset": clean_preset, "master_preset": master_preset, "rvc_model_key": rvc_model_key, "pitch_shift": pitch_shift, "f0_method": f0_method, "index_rate": index_rate, "generate_midi": generate_midi, "output_formats": sorted(formats), "output_sample_rate": output_sample_rate, "bpm": bpm}
                    params["device"] = device
                    params["performance_profile"] = performance_profile
                    params.update(input_channel_mode=input_channel_mode,output_channels=output_channels,
                                  detailed_analysis=detailed_analysis)
                    if mode == 'voice_conversion':
                        from core.job_spec import validate_mix_gain_db
                        params['mix_vocal_gain_db'] = validate_mix_gain_db(
                            mix_vocal_gain_db, 'mix_vocal_gain_db')
                        params['mix_instrumental_gain_db'] = validate_mix_gain_db(
                            mix_instrumental_gain_db, 'mix_instrumental_gain_db')
                    if mode in ('voice_conversion', 'transcribe'):
                        from core.job_spec import validate_transcribe_stem
                        params['transcribe_stem'] = validate_transcribe_stem(transcribe_stem)
                    from core.job_spec import validate_audio_options
                    validate_audio_options(params)
                    if mode == 'voice_conversion':
                        from core.bounded_voice_dsp import validate_profile
                        params['rvc_postprocess_profile'] = validate_profile(rvc_postprocess_profile)
                        params['mix_bus_profile'] = validate_profile(mix_bus_profile)
                        if type(adaptive_mix) is not bool:
                            raise ValueError('adaptive_mix 必须为布尔值')
                        params['adaptive_mix'] = adaptive_mix
                    if source_hint is not None:
                        from core.source_policy import HINTS
                        if source_hint not in HINTS:
                            raise ValueError("未知来源提示")
                        params["source_hint"] = source_hint
                    if source_preclean_enabled is not None:
                        if type(source_preclean_enabled) is not bool:
                            raise ValueError("source_preclean_enabled 只能是布尔值")
                        params["source_preclean_enabled"] = source_preclean_enabled
                    if mode == "voice_conversion":
                        from core.rvc_headroom import validate_rvc_headroom_strategy, validate_rvc_input_gain_db
                        if rvc_input_gain_db is not None:
                            params["rvc_input_gain_db"] = validate_rvc_input_gain_db(rvc_input_gain_db)
                            params["rvc_input_headroom_strategy"] = validate_rvc_headroom_strategy(rvc_input_headroom_strategy or "fixed")
                        elif rvc_input_headroom_strategy is not None:
                            params["rvc_input_headroom_strategy"] = validate_rvc_headroom_strategy(rvc_input_headroom_strategy)
                        if skip_vocal_eq is not None:
                            from core.vocal_eq import validate_skip_vocal_eq
                            params["skip_vocal_eq"] = validate_skip_vocal_eq(skip_vocal_eq)
                        if rvc_postprocess is not None:
                            from core.rvc_postprocess import validate_rvc_postprocess
                            params["rvc_postprocess"] = validate_rvc_postprocess(rvc_postprocess)
                        if skip_mix_compressor is not None:
                            from core.mix_compressor import validate_skip_mix_compressor
                            params["skip_mix_compressor"] = validate_skip_mix_compressor(skip_mix_compressor)
                        if mix_ducking is not None:
                            if type(mix_ducking) is not bool:
                                raise ValueError("mix_ducking 只能是布尔值")
                            params["mix_ducking"] = mix_ducking
                        if mix_reverb is not None:
                            if type(mix_reverb) is not bool:
                                raise ValueError('mix_reverb 只能是布尔值')
                            params['mix_reverb'] = mix_reverb
                        if quality_assist is not None:
                            from core.voice_quality_flow import validate_quality_assist
                            params["quality_assist"] = validate_quality_assist(quality_assist)
                        if quality_retry_limit is not None:
                            from core.quality_retry import validate_quality_retry_limit
                            params["quality_retry_limit"] = validate_quality_retry_limit(quality_retry_limit)
                        if parameter_search_enabled is not None:
                            if type(parameter_search_enabled) is not bool:
                                raise ValueError("parameter_search_enabled 只能是布尔值")
                            params["parameter_search_enabled"] = parameter_search_enabled
                        if parameter_search_budget is not None:
                            from core.budgeted_search import budget_limit
                            budget_limit(parameter_search_budget)
                            params["parameter_search_budget"] = parameter_search_budget
                        if vocal_restoration_effect is not None:
                            params["vocal_restoration_effect"] = vocal_restoration_effect
                        if vocal_restore_profile is not None:
                            from core.vocal_restore_profile import normalize_profile
                            params["vocal_restore_profile"] = normalize_profile(vocal_restore_profile)
                        if pitch_correction_mode is not None:
                            params["pitch_correction_mode"] = pitch_correction_mode
                        if pitch_correction_key is not None:
                            params["pitch_correction_key"] = pitch_correction_key
                        if separator_backend is not None:
                            from core.separator_choice import validate_separator_backend
                            params["separator_backend"] = validate_separator_backend(separator_backend)
                        if separator_profile is not None:
                            from core.separator_routing import validate_separator_profile
                            params["separator_profile"] = validate_separator_profile(separator_profile)
                    if mode == "polish":
                        if polish_profile is not None:
                            params["polish_profile"] = polish_profile
                        if polish_reference:
                            params["polish_reference"] = polish_reference
                        if polish_options:
                            params["polish_options"] = polish_options
                    if midi_cleanup is not None:
                        from core.midi_cleanup_options import normalize_midi_cleanup
                        params["midi_cleanup"] = normalize_midi_cleanup(midi_cleanup)
                    if dispatch_id:
                        params['dispatch_id'] = dispatch_id
                formats = self._normalise_formats(output_formats)
                self._validate_parameters(mode, clean_preset, master_preset, rvc_model_key, pitch_shift, f0_method, index_rate, formats, output_sample_rate, bpm, check_model=not bool(resume_job_id))
                if not resume_job_id:
                    from core.job_spec import freeze
                    params = freeze(params, mode, artifact_root=os.path.join(os.path.dirname(self.storage.db_path),'model-snapshots'))
                output_root = os.path.abspath(os.path.dirname(project_dir))
                from core.cache_manager import CacheManager
                from core.config import load_settings
                settings = load_settings()
                cache_policy = settings.get('cache_policy', {})
                if cache_policy.get('enabled', True):
                    try:
                        gc_result = CacheManager(self.storage,output_root).collect(
                            quota_bytes=cache_policy.get('quota_bytes',20*1024**3),
                            ttl_days=cache_policy.get('ttl_days',30),protected_paths=[input_file])
                        if gc_result['reclaimed_bytes'] or gc_result['errors'] or not gc_result['quota_met']:
                            self.log('缓存回收：'+canonical_json(gc_result))
                    except JobBusyError:
                        self.log('缓存回收被占用或源项目正在使用，已跳过本次自动回收；继续检查磁盘预算')
                self._disk_preflight(input_file, output_root)
                from core.resource_limits import require_processing_budget
                try:pre_meta=probe_metadata(input_file)
                except Exception:pre_meta=None
                if isinstance(pre_meta, dict):
                    try:
                        candidate_duration=float(pre_meta.get('duration'))
                        if math.isfinite(candidate_duration) and candidate_duration>0:
                            self._eta_audio_duration=candidate_duration
                    except (TypeError,ValueError):
                        pass
                if pre_meta:require_processing_budget(float(pre_meta['duration']),mode,settings.get('processing_limits'))
                if job_id is None:
                    job_id = self.storage.create_job(input_file, mode, params, source_hash=sha256_path(input_file), project_dir=project_dir)
                    claims.enter_context(self.storage.execution(job_id))
                master_dir = os.path.join(project_dir, "master")
                stems_dir = os.path.join(project_dir, "stems")
                reports_dir = os.path.join(project_dir, "reports")
                midi_dir = os.path.join(project_dir, "midi")
                job_temp = os.path.join(project_dir, ".work")
                for directory in (master_dir, stems_dir, reports_dir, midi_dir, job_temp):
                    os.makedirs(directory, exist_ok=True)
                self.log(f"\n========================================================")
                self.log(f"开始处理: 【{os.path.basename(project_dir)}】 (模式: {mode})")
                self.log(f"========================================================")
                ingest_parameters={"mode":mode}
                if 'input_channel_mode' in params:ingest_parameters['input_channel_mode']=params['input_channel_mode']
                ingest = self._run_stage(job_id, "Ingest", {"input": input_file}, ingest_parameters, lambda: self._ingest(input_file, job_temp,mode=mode,policy=settings.get('processing_limits'),**({'input_channel_mode':params['input_channel_mode']} if params.get('input_channel_mode','auto')!='auto' else {})), 0.05)
                try:
                    ingest_duration=float((ingest.get('metadata') or {}).get('duration'))
                    if math.isfinite(ingest_duration) and ingest_duration>0:
                        self._eta_audio_duration=ingest_duration
                except (AttributeError,TypeError,ValueError):
                    self._eta_audio_duration=None
                standard_wav = ingest["standard_wav"]
                self._disk_preflight(standard_wav, output_root)
                from core.resource_limits import require_processing_budget
                require_processing_budget(float(ingest['metadata']['duration']),mode,settings.get('processing_limits'))
                processing_wav = standard_wav
                source_policy_record = None
                if params.get("source_preclean_enabled") is True:
                    source_hint_value = params.get("source_hint", "auto")
                    source_output = os.path.join(job_temp, "01_source_preclean.wav")
                    source_report_path = os.path.join(reports_dir, "source_policy.json")
                    source_parameters = {
                        "source_hint": source_hint_value,
                        "source_preclean_enabled": True,
                    }
                    source_result = self._run_stage(
                        job_id, "SourceClean", {"input": standard_wav}, source_parameters,
                        lambda: self._preclean_source(
                            standard_wav, source_output, source_report_path,
                            source_hint=source_hint_value, enabled=True,
                        ), 0.08,
                    )
                    processing_wav = source_result["cleaned"]
                    try:
                        with open(source_result["report_path"], encoding="utf-8") as handle:
                            source_policy_record = json.load(handle)
                    except (OSError, ValueError) as exc:
                        raise RuntimeError("SourcePolicy 阶段报告缺失或损坏") from exc
                analysis_parameters={'detailed_analysis':params['detailed_analysis']} if 'detailed_analysis' in params else {}
                analysis = self._run_stage(job_id, "Analyze", {"input": processing_wav}, analysis_parameters, lambda: self._analyze(processing_wav, reports_dir,**({'detailed_analysis':False} if params.get('detailed_analysis',True) is False else {})), 0.10)
                meta = analysis.get("metadata") or ingest.get("metadata") or probe_metadata(standard_wav)
                sync_offset_ms = 0.0
                alignment_path: Optional[str] = None
                rvc_raw = None
                f0_quality = None
                artifact_record = None
                quality_record = None
                model_display_name = "N/A"
                cleaned_vocal: Optional[str] = None
                raw_inst: Optional[str] = None
                polish_before = None
                if mode == "polish":
                    from core.polish_chain import render_stage
                    from core.polish_dsp import NonFiniteAudio
                    from core.polish_policy import (
                        dynamics_parameters, repair_parameters, resolve_polish_policy,
                        route_polish, stereo_parameters, tone_parameters,
                    )
                    polish_policy = resolve_polish_policy(params)
                    polish_report = analysis.get("analysis") or {}
                    if polish_report.get("repair", {}).get("nan_inf"):
                        raise NonFiniteAudio("音频包含 NaN/Inf")
                    trace_dir = os.path.join(stems_dir, "polish")
                    os.makedirs(trace_dir, exist_ok=True)
                    source_trace = os.path.join(trace_dir, "source.wav")
                    shutil.copyfile(processing_wav, source_trace)
                    polish_decisions = route_polish(polish_report, polish_policy)
                    repaired_wav = os.path.join(trace_dir, "repaired.wav")
                    tone_wav = os.path.join(trace_dir, "tone.wav")
                    dynamics_wav = os.path.join(trace_dir, "dynamics.wav")
                    stereo_wav = os.path.join(trace_dir, "stereo.wav")
                    reference_audio = None
                    reference_rate = None
                    identity = polish_policy.get("reference") if polish_policy.get("reference", {}).get("sha256") else None
                    tone_policy = polish_policy
                    reference_enabled = bool(polish_decisions.get("reference", {}).get("enabled"))
                    if reference_enabled and (not identity or not identity.get("path")):
                        raise RuntimeError("Reference Match 已启用，但冻结参考曲身份缺少路径")
                    if reference_enabled:
                        from core.polish_reference_runtime import prepare_reference
                        tone_policy, reference_audio, reference_rate = prepare_reference(polish_policy, identity)
                        reference_enabled = bool(tone_policy['reference']['enabled'])
                        if not reference_enabled:
                            warning = tone_policy['reference']['read_error']
                            self.log(warning)
                            polish_decisions['reference'].update(enabled=False, reason=warning)
                    _json_file(os.path.join(trace_dir, "polish_decision.json"), {"decisions": polish_decisions, "no_problem": polish_decisions.get("no_problem"), "profile": polish_decisions.get("profile")})
                    # Every processor consumes the original analysis, even when
                    # an earlier stage bypasses and leaves identical WAV bytes.
                    analysis_identity = analysis.get("report") or polish_report
                    repaired = self._run_stage(job_id, "Repair", {"input": processing_wav, "analysis": analysis_identity}, repair_parameters(polish_policy), lambda: render_stage("repair", processing_wav, repaired_wav, polish_report, polish_policy), 0.28)
                    tone_inputs = {"input": repaired["wav"], "analysis": analysis_identity}
                    if reference_enabled:
                        tone_inputs["reference"] = identity["path"]

                    def _run_tone():
                        return render_stage("tone", repaired["wav"], tone_wav, polish_report,
                                            tone_policy, reference_audio, reference_rate)

                    tone_cache_parameters = tone_parameters(tone_policy, identity)
                    if identity:
                        tone_cache_parameters['reference_runtime_sha256'] = sha256_file(
                            os.path.join(os.path.dirname(__file__), 'polish_reference_runtime.py'))
                    if tone_policy.get('reference', {}).get('read_error'):
                        tone_cache_parameters['reference']['read_error'] = tone_policy['reference']['read_error']
                    tone = self._run_stage(job_id, "Tone", tone_inputs,
                                           tone_cache_parameters, _run_tone, 0.42)
                    dynamics = self._run_stage(job_id, "Dynamics", {"input": tone["wav"], "analysis": analysis_identity}, dynamics_parameters(polish_policy), lambda: render_stage("dynamics", tone["wav"], dynamics_wav, polish_report, polish_policy), 0.58)
                    stereo = self._run_stage(job_id, "Stereo", {"input": dynamics["wav"], "analysis": analysis_identity}, stereo_parameters(polish_policy), lambda: render_stage("stereo", dynamics["wav"], stereo_wav, polish_report, polish_policy), 0.74)
                    master_input = stereo["wav"]
                    polish_before = source_trace
                    stages_results = {
                        "repair": repaired,
                        "tone": tone,
                        "dynamics": dynamics,
                        "stereo": stereo,
                    }
                    degraded_stages = [
                        name for name, res in stages_results.items()
                        if isinstance(res, dict) and (res.get("degraded") or res.get("status") == "BYPASSED_BY_ERROR")
                    ]
                    _json_file(os.path.join(trace_dir, "polish_decision.json"), {
                        "decisions": polish_decisions,
                        "no_problem": polish_decisions.get("no_problem"),
                        "profile": polish_decisions.get("profile"),
                        "has_degradation": bool(degraded_stages),
                        "degraded_stages": degraded_stages,
                        "stage_statuses": {name: res.get("status") for name, res in stages_results.items() if isinstance(res, dict)},
                    })
                    if degraded_stages:
                        self.log(f"警告：Polish 阶段存在异常降级：{', '.join(degraded_stages)}（已保留原音频，可重试）")
                else:
                    separate_parameters = {"model": "four-stem-addon-or-HP2", "mode": mode}
                    separate_runner = lambda: self._separate_and_copy(processing_wav, job_temp, stems_dir, mode, **({"execution_policy": params["execution_policy"]} if "execution_policy" in params else {}))
                    if mode == "voice_conversion":
                        from core.separator_choice import resolve_separator_request
                        resolve_separator_request(params.get("separator_backend", "hp2"))
                        from core.separator_routing import voice_separate_binding
                        binding = voice_separate_binding(params.get("separator_profile"), mode)
                        if binding["execution"] == "single_pass":
                            separate_parameters = {"model": "four-stem-addon-or-HP2", "mode": mode}
                        elif binding["execution"] == "split":
                            separate_parameters = dict(binding["parameters"])
                            separate_parameters["effective_separator"] = params.get("effective_separator")
                            separate_runner = lambda: self._separate_dual_and_copy(
                                processing_wav, job_temp, stems_dir, mode,
                                profile=params.get("separator_profile"), artifacts=params.get("effective_separator"), **({"execution_policy": params["execution_policy"]} if "execution_policy" in params else {}))
                        else:
                            separate_parameters = binding["parameters"]
                            refused = binding
                            def separate_runner(plan=refused):
                                from core.separator_routing import refuse_unexecuted_route
                                refuse_unexecuted_route(plan)
                    if "execution_policy" in params:
                        separate_parameters["execution_policy"] = params["execution_policy"]
                        separate_parameters["execution_policy_sha256"] = sha256_file(os.path.join(os.path.dirname(__file__), "execution_policy.py"))
                    separate = self._run_stage(job_id, "Separate", {"input": processing_wav}, separate_parameters, separate_runner, 0.20 if mode == "voice_conversion" else 0.30)
                    raw_vocal = _existing_path(separate.get("vocal"))
                    raw_inst = separate.get("instrumental") or separate.get("other")
                    transcribe_requested = mode == "transcribe" or (mode == "voice_conversion" and generate_midi)
                    selected_transcribe_stem = params.get("transcribe_stem", "vocal") if transcribe_requested else "vocal"
                    transcribe_source = None
                    if transcribe_requested:
                        # Selection happens directly against Separate outputs.
                        # A missing bass/other stem must stop the job instead of
                        # silently feeding the vocal-clean path.
                        transcribe_source = self._select_transcribe_stem(
                            separate, selected_transcribe_stem)
                    if mode == "voice_conversion" or selected_transcribe_stem == "vocal":
                        if not raw_vocal:
                            raise RuntimeError("分轨未生成 vocal.wav")
                        from core.vocal_eq import apply_vocal_extras
                        vocal_extras = apply_vocal_extras(mode, params)
                        clean_parameters = {"preset": clean_preset, "effective_clean": params["effective_clean"], "vocal": vocal_extras}
                        if not vocal_extras:
                            clean_parameters["skip_vocal_eq"] = True
                        cleaned_result = self._run_stage(job_id, "Clean", {"input": raw_vocal}, clean_parameters, lambda: {"cleaned": self._clean(raw_vocal, os.path.join(stems_dir, "02_vocal_cleaned.wav"), clean_preset, vocal=vocal_extras, effective=params["effective_clean"])}, 0.40 if mode == "voice_conversion" else 0.60)
                        cleaned_vocal = cleaned_result["cleaned"]
                    else:
                        # Non-vocal transcription has no vocal-clean stage.
                        cleaned_vocal = None
                    restoration = params.get("effective_vocal_restoration")
                    restore_profile = (params.get("vocal_restore_profile")
                                       if mode == "voice_conversion" and
                                       "vocal_restore_profile" in params else None)
                    if mode == "voice_conversion" and (isinstance(restoration, dict) or restore_profile is not None):
                        from core.vocal_restore_profile import (
                            apply_vocal_restore_profile, evaluate_profile, normalize_profile,
                        )
                        if restore_profile is not None:
                            restore_profile = normalize_profile(restore_profile)
                            profile_evaluation = evaluate_profile(cleaned_vocal, restore_profile)
                        else:
                            profile_evaluation = None
                        profile_report_path = os.path.join(reports_dir, "vocal_restore_profile.json")
                        restoration_parameters = {}
                        if profile_evaluation is not None:
                            restoration_parameters.update({
                                "profile": restore_profile,
                                "profile_source_sha256": profile_evaluation["source_sha256"],
                                "profile_measurements": profile_evaluation["measurements"],
                                "profile_applied": profile_evaluation["applied"],
                                "profile_report_path": profile_report_path,
                            })
                        if isinstance(restoration, dict):
                            restoration_parameters.update({
                                "effect_id": restoration["effect_id"],
                                "weight_path": restoration.get("path"),
                                "weight_sha256": restoration["sha256"],
                                "agg": restoration.get("agg", 10),
                                "backend": restoration.get("backend"),
                                "algorithm": restoration.get("algorithm"),
                            })

                        if (profile_evaluation is not None and profile_evaluation["skip"] and
                                not isinstance(restoration, dict)):
                            report_record = apply_vocal_restore_profile(
                                cleaned_vocal, os.path.join(stems_dir, "03_vocal_profile.wav"),
                                restore_profile, report_path=profile_report_path,
                                cancel=lambda: _control_check(self._active_control),
                                evaluation=profile_evaluation,
                            )
                            if sha256_file(cleaned_vocal) != profile_evaluation["source_sha256"]:
                                raise RuntimeError("VocalRestore 跳过判断期间输入身份发生变化")
                            restoration_parameters["skip_report_sha256"] = sha256_file(profile_report_path)
                            self._record_skipped_stage(
                                job_id, "VocalRestore", {"input": cleaned_vocal},
                                restoration_parameters, profile_evaluation["skip_reason"], 0.46,
                            )
                        else:
                            def _run_restoration():
                                profile_input = cleaned_vocal
                                outputs = {}
                                if profile_evaluation is not None:
                                    profile_result = apply_vocal_restore_profile(
                                        cleaned_vocal, os.path.join(stems_dir, "03_vocal_profile.wav"),
                                        restore_profile, report_path=profile_report_path,
                                        cancel=lambda: _control_check(self._active_control),
                                        evaluation=profile_evaluation,
                                    )
                                    profile_input = profile_result["output"]
                                    if profile_result.get("report_path"):
                                        outputs["profile_report_path"] = profile_result["report_path"]
                                if isinstance(restoration, dict):
                                    from core.vocal_restoration import run_effect
                                    record = run_effect(
                                        profile_input, restoration["effect_id"],
                                        os.path.join(stems_dir, "vocal_restoration"),
                                        agg=restoration.get("agg", 10),
                                        weight_dir=(restoration.get("weight_dir") or
                                                    (os.path.dirname(restoration["path"]) if restoration.get("path") else None)),
                                        expected_identity=restoration,
                                    )
                                    if record.get("benchmark_status") != "MEASURED":
                                        raise RuntimeError("VocalRestore 未完成：" + str(record.get("error") or record.get("reason") or "未知错误"))
                                    outputs.update({
                                        "restored": record["restored_path"],
                                        "residual": record["residual_path"],
                                        "report_path": record["report_path"],
                                        **({"worker_report_path": record["worker_report_path"]}
                                           if record.get("worker_report_path") else {}),
                                    })
                                else:
                                    outputs["restored"] = profile_input
                                return outputs

                            restored = self._run_stage(
                                job_id, "VocalRestore", {"input": cleaned_vocal},
                                restoration_parameters, _run_restoration, 0.46,
                            )
                            if restored.get("restored"):
                                cleaned_vocal = restored["restored"]
                    correction_mode = params.get("pitch_correction_mode", "off")
                    if mode == "voice_conversion" and "pitch_correction_mode" in params:
                        correction_key = params.get("pitch_correction_key")
                        correction_parameters = {
                            "mode": correction_mode,
                            "key": correction_key,
                        }
                        if correction_mode == "off":
                            self._record_skipped_stage(
                                job_id, "PitchCorrection", {"input": cleaned_vocal},
                                correction_parameters, "用户选择 PitchCorrection Off", 0.49,
                            )
                        else:
                            corrected_path = os.path.join(stems_dir, "03_vocal_pitch_corrected.wav")
                            correction_report = os.path.join(reports_dir, "pitch_correction.json")

                            def _run_pitch_correction():
                                from core.pitch_correction import correct_pitch_file
                                record = correct_pitch_file(
                                    cleaned_vocal, corrected_path, mode=correction_mode,
                                    key=correction_key, report_path=correction_report,
                                    cancel=lambda: _control_check(self._active_control),
                                )
                                return {"corrected": record["wav"], "decision_path": record["decision"]}

                            corrected = self._run_stage(
                                job_id, "PitchCorrection", {"input": cleaned_vocal},
                                correction_parameters, _run_pitch_correction, 0.49,
                            )
                            cleaned_vocal = corrected["corrected"]
                    if mode == "transcribe":
                        transcribe_input = transcribe_source if selected_transcribe_stem != "vocal" else cleaned_vocal
                        transcribe_parameters = {"bpm": bpm, "transcribe_stem": selected_transcribe_stem}
                        transcribed = self._run_stage(job_id, "Transcribe", {"input": transcribe_input}, transcribe_parameters, lambda: {"midi": _transcribe(transcribe_input, os.path.join(midi_dir, f"{os.path.basename(project_dir)}.mid"), bpm=bpm)}, 0.72)
                        transcribed = self._cleanup_generated_midi(job_id, transcribed["midi"], midi_dir, reports_dir, params, 0.76)
                        master_input = raw_inst or standard_wav
                    else:
                        if generate_midi:
                            transcribe_input = transcribe_source if selected_transcribe_stem != "vocal" else cleaned_vocal
                            transcribe_parameters = {"bpm": bpm, "transcribe_stem": selected_transcribe_stem, "reason": "voice_conversion_generate_midi"}
                            transcribed = self._run_stage(job_id, "Transcribe", {"input": transcribe_input}, transcribe_parameters, lambda: {"midi": _transcribe(transcribe_input, os.path.join(midi_dir, f"{os.path.basename(project_dir)}.mid"), bpm=bpm)}, 0.52)
                            transcribed = self._cleanup_generated_midi(job_id, transcribed["midi"], midi_dir, reports_dir, params, 0.55)
                        try:
                            from core.config import get_available_rvc_models
                            model_info = params["effective_rvc"]
                            model_display_name = model_info.get("name", rvc_model_key)
                        except Exception:
                            model_display_name = rvc_model_key
                        from core.job_spec import validate
                        validate(params, mode)
                        from core.rvc_headroom import (
                            apply_rvc_input_gain, resolve_rvc_input_headroom, write_headroom_report,
                        )
                        assist_source = os.path.join(job_temp, "05_rvc_input.wav")
                        rvc_headroom = resolve_rvc_input_headroom(params, cleaned_vocal)
                        rvc_gain = rvc_headroom["gain_db"]
                        from core.f0_method import apply_f0_to_stage
                        rvc_parameters = {"model": rvc_model_key, "pitch_shift": pitch_shift, "f0_method": f0_method, "index_rate": index_rate, "effective_rvc": params["effective_rvc"]}
                        rvc_parameters["rvc_input_headroom_strategy"] = rvc_headroom["strategy"]
                        rvc_parameters["rvc_input_gain_db"] = rvc_gain
                        rvc_parameters["rvc_input_peak_dbfs"] = rvc_headroom["input_peak_dbfs"]
                        frozen_f0 = params.get("effective_f0")
                        if isinstance(frozen_f0, dict):
                            executed_f0 = frozen_f0["executed"]
                            rvc_parameters["f0_method"] = executed_f0
                            rvc_parameters["effective_f0"] = frozen_f0
                            if frozen_f0.get("fallback"):
                                rvc_parameters["f0_requested"] = frozen_f0["requested"]
                                rvc_parameters["f0_fallback_reason"] = frozen_f0["reason"]
                        elif f0_method == "rmvpe":
                            # Historical RMVPE requests executed harvest. Keep
                            # that frozen job's behavior after installing RMVPE.
                            executed_f0 = "harvest"
                            rvc_parameters.update(f0_method=executed_f0, f0_requested="rmvpe",
                                f0_fallback_reason="旧任务未冻结 RMVPE 引擎，沿用历史 harvest；创建新任务以启用 RMVPE")
                        else:
                            rvc_parameters, executed_f0 = apply_f0_to_stage(rvc_parameters, f0_method)
                        if "execution_policy" in params:
                            rvc_parameters["execution_policy"] = params["execution_policy"]
                            rvc_parameters["execution_policy_sha256"] = sha256_file(os.path.join(os.path.dirname(__file__), "execution_policy.py"))
                        def _run_rvc():
                            source = cleaned_vocal
                            if rvc_gain:
                                source = assist_source
                                apply_rvc_input_gain(cleaned_vocal, source, rvc_gain)
                            output = os.path.join(stems_dir, f"03_vocal_rvc_{rvc_model_key}.wav")
                            result = self._convert_voice(source, output, model_key=rvc_model_key, pitch_shift=pitch_shift, f0_method=executed_f0, index_rate=index_rate, temp_dir=job_temp, profile=params["effective_rvc"], **({"execution_policy": params["execution_policy"]} if "execution_policy" in params else {}))
                            result["headroom_report"] = write_headroom_report(
                                output + ".headroom.json", rvc_headroom, cleaned_vocal, output,
                            )
                            return result
                        rvc_result = self._run_stage(job_id, "RVC", {"input": cleaned_vocal}, rvc_parameters, _run_rvc, 0.60)
                        rvc_vocal = rvc_result["rvc"]
                        from core.qc import inspect_rvc_raw
                        rvc_raw = inspect_rvc_raw(rvc_vocal)
                        with open(rvc_vocal + ".raw-qc.json", "w", encoding="utf-8", newline="\n") as raw_qc_file:
                            json.dump(rvc_raw, raw_qc_file, ensure_ascii=False, indent=2, sort_keys=True)
                            raw_qc_file.write("\n")
                        from core.f0_quality import worker_export_record, assess_f0_track
                        f0_quality = worker_export_record()
                        f0_track_path = rvc_vocal + ".f0-track.json"
                        if os.path.isfile(f0_track_path):
                            try:
                                with open(f0_track_path, encoding="utf-8") as f0_track_file:
                                    f0_track = json.load(f0_track_file)
                                if f0_track.get("status") == "CAPTURED":
                                    f0_quality = assess_f0_track(f0_track["frames_hz"])
                                    f0_quality["actual_f0"] = f0_track.get("actual_f0")
                            except (OSError, ValueError, KeyError, TypeError):
                                self.log("F0 轨迹损坏或不可读取，保留不可观测状态")
                        with open(rvc_vocal + ".f0-qc.json", "w", encoding="utf-8", newline="\n") as f0_qc_file:
                            json.dump(f0_quality, f0_qc_file, ensure_ascii=False, indent=2, sort_keys=True)
                            f0_qc_file.write("\n")
                        from core.artifact_score import assess_artifact_file, unread_record
                        try:
                            artifact_record = assess_artifact_file(rvc_vocal)
                        except (OSError, ValueError, RuntimeError) as exc:
                            artifact_record = unread_record("无法读取 RVC 波形：" + str(exc))
                        with open(rvc_vocal + ".artifact-qc.json", "w", encoding="utf-8", newline="\n") as artifact_file:
                            json.dump(artifact_record, artifact_file, ensure_ascii=False, indent=2, sort_keys=True)
                            artifact_file.write("\n")
                        from core.voice_quality_flow import (
                            apply_voice_quality, completed_choice, read_clip_source, write_quality_sidecars,
                        )
                        assist_source = os.path.join(job_temp, "05_rvc_input.wav") if rvc_gain else cleaned_vocal
                        existing_quality = completed_choice(rvc_vocal, params)
                        if existing_quality is not None:
                            quality_record = existing_quality
                        else:
                            clip_samples, clip_rate = read_clip_source(rvc_vocal)
                            profile = (params.get("effective_rvc") or {}).get("profile") or {}
                            job_axes = {
                                "pitch_shift": pitch_shift,
                                "index_rate": index_rate,
                                "f0_method": executed_f0,
                                "effective_f0": frozen_f0,
                            }
                            def _infer_quality(payload):
                                # Candidate files never replace the baseline or
                                # become the parameters for a resumed job.
                                candidate_parameters = {
                                    "pitch_shift": payload["pitch_shift"],
                                    "index_rate": payload["index_rate"],
                                    "protect": (params["effective_rvc"].get("profile") or {}).get("protect", 0.33),
                                    "f0_method": executed_f0,
                                    "effective_f0": frozen_f0,
                                }
                                candidate_hash = hashlib.sha256(canonical_json(candidate_parameters).encode("utf-8")).hexdigest()
                                dest = os.path.join(stems_dir, "quality_assist", candidate_hash + ".wav")
                                os.makedirs(os.path.dirname(dest), exist_ok=True)
                                try:
                                    produced = self._convert_voice(
                                        assist_source, dest, model_key=rvc_model_key,
                                        pitch_shift=payload["pitch_shift"], f0_method=executed_f0,
                                        index_rate=payload["index_rate"], temp_dir=job_temp,
                                        profile=params["effective_rvc"],
                                        **({"execution_policy": params["execution_policy"]} if "execution_policy" in params else {}),
                                    )
                                    return {
                                        "failed": False,
                                        "artifact": produced["rvc"],
                                        "candidate_parameter_sha256": candidate_hash,
                                        "artifact_sha256": sha256_file(produced["rvc"]),
                                    }
                                except Exception as exc:
                                    return {"failed": True, "artifact": rvc_vocal, "error": type(exc).__name__}

                            quality_record = apply_voice_quality(
                                params, job_axes, profile, rvc_vocal, artifact_record, f0_quality,
                                infer=_infer_quality, samples=clip_samples, sample_rate=clip_rate,
                            )
                            if quality_record.get("executed") is True:
                                candidate_path = quality_record.get("kept_path")
                                candidate_hash = None
                                candidate_artifact_hash = None
                                if isinstance(candidate_path, str) and os.path.isfile(candidate_path):
                                    candidate_artifact_hash = sha256_path(candidate_path)
                                    candidate_hash = os.path.splitext(os.path.basename(candidate_path))[0]
                                quality_record["candidate_path"] = candidate_path
                                quality_record["candidate_parameter_sha256"] = candidate_hash
                                quality_record["candidate_artifact_sha256"] = candidate_artifact_hash
                                quality_record["selected_for_job"] = "baseline"
                                quality_record["kept_path"] = rvc_vocal
                                quality_record["kept_sha256"] = sha256_path(rvc_vocal)
                            write_quality_sidecars(rvc_vocal, quality_record)
                        # Search and quality-assist candidates are review-only.
                        # The frozen baseline remains the source for later stages.
                        kept_vocal = rvc_vocal
                        if params.get("parameter_search_enabled") is True:
                            from core.budgeted_search import run_budgeted_search
                            from core.parameter_search import bounded_candidates
                            from core.artifact_score import assess_artifact_file
                            frozen_profile = (params.get("effective_rvc") or {}).get("profile") or {}
                            search_job = {
                                "pitch_shift": pitch_shift,
                                "index_rate": index_rate,
                                "f0_method": executed_f0,
                                "effective_f0": frozen_f0,
                            }
                            search_plan = bounded_candidates(
                                search_job, frozen_profile,
                                budget=params.get("parameter_search_budget", "fast"),
                                model=params.get("effective_rvc"),
                            )
                            search_source = assist_source
                            if rvc_gain and not os.path.isfile(search_source):
                                apply_rvc_input_gain(cleaned_vocal, search_source, rvc_gain)
                            search_dir = os.path.join(stems_dir, "parameter_search")
                            os.makedirs(search_dir, exist_ok=True)
                            baseline_parameters = search_plan["baseline"]
                            baseline_parameter_hash = hashlib.sha256(canonical_json(baseline_parameters).encode("utf-8")).hexdigest()
                            search_parameters = {
                                "job_spec_hash": params["spec_hash"],
                                "budget": search_plan["budget"],
                                "max_gpu_seconds": {"fast": 300, "balanced": 900, "quality": 1800}[search_plan["budget"]],
                                "candidate_clip_limit": 3,
                                "candidate_clip_seconds_limit": 8.0,
                                "candidate_parameters": search_plan["candidates"],
                                "effective_f0": frozen_f0,
                                "effective_rvc": params["effective_rvc"],
                                "effective_vocal_restoration": restoration,
                                "rvc_input_headroom_strategy": rvc_headroom["strategy"],
                                "rvc_input_gain_db": rvc_gain,
                            }

                            def _run_search():
                                import copy
                                import soundfile as sf
                                from core.config import BASE_DIR, RVC_PYTHON
                                from core.representative_clips import select_clips

                                samples, sample_rate = read_clip_source(search_source)
                                if samples is None or sample_rate is None:
                                    raise RuntimeError("无法读取冻结的 RVC 输入，参数搜索保持未执行")
                                selected = select_clips(samples, sample_rate, window_seconds=8.0, limit=3)
                                if selected is samples:
                                    clip_rows = [{"start_sample": 0, "end_sample": len(samples),
                                                  "start_timecode": "00:00:00.000",
                                                  "end_timecode": "00:00:%06.3f" % (len(samples) / sample_rate),
                                                  "samples": samples}]
                                else:
                                    clip_rows = selected
                                representative_dir = os.path.join(search_dir, "representative")
                                os.makedirs(representative_dir, exist_ok=True)
                                clip_records = []
                                for index, row in enumerate(clip_rows[:3]):
                                    clip_path = os.path.join(representative_dir, "clip_%02d.wav" % index)
                                    sf.write(clip_path, row["samples"], sample_rate, subtype="FLOAT")
                                    clip_records.append({
                                        "path": clip_path,
                                        "sha256": sha256_file(clip_path),
                                        "start_sample": int(row["start_sample"]),
                                        "end_sample": int(row["end_sample"]),
                                        "start_timecode": row["start_timecode"],
                                        "end_timecode": row["end_timecode"],
                                        "duration_seconds": (int(row["end_sample"]) - int(row["start_sample"])) / float(sample_rate),
                                    })
                                source_hash = sha256_file(search_source)

                                def _infer_candidate(candidate, timeout_seconds):
                                    parameter_hash = hashlib.sha256(canonical_json(candidate).encode("utf-8")).hexdigest()
                                    candidate_dir = os.path.join(search_dir, "candidates", parameter_hash)
                                    os.makedirs(candidate_dir, exist_ok=True)
                                    candidate_model = copy.deepcopy(params["effective_rvc"])
                                    if "execution_policy" in params:
                                        candidate_model["execution_policy"] = params["execution_policy"]
                                    candidate_model.setdefault("profile", {})["protect"] = candidate["protect"]
                                    candidate_deadline = time.monotonic() + max(0.0, float(timeout_seconds))
                                    artifacts = []
                                    try:
                                        for clip_index, clip in enumerate(clip_records):
                                            remaining = candidate_deadline - time.monotonic()
                                            if remaining <= 0:
                                                raise TimeoutError("候选代表片段耗尽剩余 GPU 预算")
                                            output_path = os.path.join(candidate_dir, "clip_%02d.wav" % clip_index)
                                            request_path = os.path.join(candidate_dir, "clip_%02d.request.json" % clip_index)
                                            request = {
                                                "input": clip["path"], "output": output_path,
                                                "model_key": rvc_model_key,
                                                "pitch_shift": candidate["pitch_shift"],
                                                "f0_method": candidate["f0_method"],
                                                "index_rate": candidate["index_rate"],
                                                "profile": candidate_model,
                                                "temp_dir": job_temp,
                                                "job_spec_hash": params["spec_hash"],
                                                "candidate_parameter_sha256": parameter_hash,
                                            }
                                            _json_file(request_path, request)
                                            completed = process_run(
                                                [RVC_PYTHON, os.path.join(BASE_DIR, "core", "search_candidate_worker.py"),
                                                 "--request", request_path],
                                                timeout=remaining, check=True,
                                            )
                                            if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                                                raise RuntimeError("搜索候选 worker 未生成代表片段输出")
                                            artifacts.append({
                                                "path": output_path,
                                                "sha256": sha256_file(output_path),
                                                "input_clip_sha256": clip["sha256"],
                                                "heuristic": assess_artifact_file(output_path),
                                            })
                                        return {
                                            "failed": False,
                                            "candidate_parameter_sha256": parameter_hash,
                                            "candidate_source_sha256": source_hash,
                                            "artifacts": artifacts,
                                        }
                                    except TimeoutError:
                                        raise
                                    except ProcessCancelled:
                                        raise
                                    except Exception as exc:
                                        return {"failed": True, "stop_search": True,
                                                "stop_reason": "候选 worker 失败，后续搜索已停止",
                                                "error": type(exc).__name__ + ": " + str(exc)[-2000:],
                                                "candidate_parameter_sha256": parameter_hash,
                                                "artifacts": artifacts}

                                max_gpu_seconds = search_parameters["max_gpu_seconds"]
                                search_results = run_budgeted_search(
                                    search_plan["candidates"], _infer_candidate, max_gpu_seconds,
                                )
                                search_manifest = {
                                    "schema_version": 1,
                                    "job_spec_hash": params["spec_hash"],
                                    "budget": search_plan["budget"],
                                    "max_gpu_seconds": max_gpu_seconds,
                                    "actual_elapsed_seconds": search_results["actual_elapsed_seconds"],
                                    "stop_reason": search_results["stop_reason"],
                                    "attempted_count": search_results["attempted_count"],
                                    "candidate_clip_limit": 3,
                                    "candidate_clip_seconds_limit": 8.0,
                                    "candidate_source_sha256": source_hash,
                                    "representative_clips": clip_records,
                                    "baseline": {
                                        "candidate_parameter_sha256": baseline_parameter_hash,
                                        "artifact_path": rvc_vocal,
                                        "artifact_sha256": sha256_file(rvc_vocal),
                                        "reused_from_completed_RVC_stage": True,
                                        "inference_repeated": False,
                                    },
                                    "results": search_results["results"],
                                    "planned_count": search_results["planned_count"],
                                    "executed_count": search_results["executed_count"],
                                    "stopped_reason": search_results["stopped_reason"],
                                    "selected_for_job": "baseline",
                                    "selected_output": {
                                        "candidate_parameter_sha256": baseline_parameter_hash,
                                        "artifact_sha256": sha256_file(rvc_vocal),
                                    },
                                    "promoted": False,
                                    "heuristic_only": True,
                                    "quality_truth": False,
                                    "listening_quality": "not_assessed",
                                    "quality_acceptance": "NOT_ASSESSED",
                                }
                                search_report_path = os.path.join(search_dir, "search.json")
                                _json_file(search_report_path, search_manifest)
                                outputs = {"manifest_path": search_report_path}
                                for index, row in enumerate(search_results["results"]):
                                    for clip_index, artifact in enumerate((row.get("output") or {}).get("artifacts") or []):
                                        artifact_path = artifact.get("path")
                                        if artifact_path:
                                            outputs["candidate_%d_clip_%d_path" % (index, clip_index)] = artifact_path
                                return outputs

                            self._run_stage(
                                job_id, "RVCSearch", {"input": search_source}, search_parameters,
                                _run_search, 0.66,
                            )
                        from core.rvc_postprocess import prepare_align_input, resolve_rvc_postprocess
                        post_enabled = resolve_rvc_postprocess(params)
                        post_parameters = {"rvc_postprocess": post_enabled}
                        if 'rvc_postprocess_profile' in params:
                            post_parameters['rvc_postprocess_profile'] = params['rvc_postprocess_profile']
                            post_parameters['bounded_dsp_sha256'] = sha256_file(os.path.join(os.path.dirname(__file__), 'bounded_voice_dsp.py'))
                        if params.get("quality_assist") is True:
                            post_parameters["quality_assist"] = True
                        post_dest = os.path.join(job_temp, "07_rvc_post.wav")
                        def _run_post():
                            post_source = prepare_align_input(kept_vocal, post_dest, post_enabled)
                            if params.get('rvc_postprocess_profile', 'off') == 'gentle':
                                from core.bounded_voice_dsp import process
                                post_source = process(post_source, os.path.join(job_temp, '07_rvc_shaped.wav'), kind='post')
                                return {'rvc': post_source, 'dsp_report': post_source + '.dsp.json'}
                            return {"rvc": post_source}
                        post_result = self._run_stage(job_id, "RVCPost", {"rvc": kept_vocal}, post_parameters, _run_post, 0.68)
                        align_input = post_result["rvc"]
                        alignment_result = self._run_stage(job_id, "Align", {"reference": raw_vocal, "rvc": align_input}, {"max_lag_ms": 300}, lambda: self._align(raw_vocal, align_input, os.path.join(stems_dir, "03_vocal_aligned.wav")), 0.75)
                        sync_offset_ms = float(alignment_result.get("offset_ms", 0.0))
                        alignment_path = alignment_result["alignment_json"]
                        from core.mix_compressor import resolve_skip_mix_compressor
                        skip_compressor = resolve_skip_mix_compressor(params)
                        from core.job_spec import resolve_mix_reverb
                        previous_mix = self.storage.get_stage(job_id, 'Mix')
                        add_mix_reverb = resolve_mix_reverb(params, (previous_mix or {}).get('parameters'))
                        # Include every value that reaches mix_stems in the
                        # stage identity.  Historical specs without these
                        # fields resolve to the same legacy constants above.
                        mix_parameters = {
                            "reverb": add_mix_reverb,
                            "mix_vocal_gain_db": mix_vocal_gain_db,
                            "mix_instrumental_gain_db": mix_instrumental_gain_db,
                        }
                        if 'adaptive_mix' in params:
                            mix_parameters['adaptive_mix'] = params['adaptive_mix']
                        if 'mix_bus_profile' in params:
                            mix_parameters['mix_bus_profile'] = params['mix_bus_profile']
                            mix_parameters['bounded_dsp_sha256'] = sha256_file(os.path.join(os.path.dirname(__file__), 'bounded_voice_dsp.py'))
                        if skip_compressor:
                            mix_parameters["skip_mix_compressor"] = True
                        mix_instrumental = raw_inst or standard_wav
                        if "mix_ducking" in params:
                            duck_inputs = {"vocal": alignment_result["aligned"], "instrumental": mix_instrumental}
                            if params["mix_ducking"]:
                                from core.mix_ducking import PROFILE, apply_vocal_ducking
                                duck_profile = dict(PROFILE)
                                ducked = self._run_stage(
                                    job_id, "Ducking", duck_inputs,
                                    {"enabled": True, "settings": duck_profile},
                                    lambda: apply_vocal_ducking(
                                        alignment_result["aligned"], mix_instrumental,
                                        os.path.join(job_temp, "06_ducked_instrumental.wav"),
                                        os.path.join(reports_dir, "ducking.json"),
                                    ), 0.83,
                                )
                                mix_instrumental = ducked["instrumental"]
                            else:
                                self._record_skipped_stage(
                                    job_id, "Ducking", duck_inputs, {"enabled": False},
                                    "用户在任务参数中关闭了 Ducking", 0.83,
                                )
                        def _run_mix():
                            raw_mix = os.path.join(job_temp, '06_mixed.wav')
                            output = mix_stems(alignment_result["aligned"], mix_instrumental, raw_mix,
                                               vocal_gain_db=mix_vocal_gain_db,
                                               inst_gain_db=mix_instrumental_gain_db,
                                               add_reverb=add_mix_reverb, skip_mix_compressor=skip_compressor, adaptive_mix=params.get('adaptive_mix'),
                                               mix_bus_profile=params.get('mix_bus_profile', 'off'))
                            results = {'mixed': output}
                            if 'adaptive_mix' in params:
                                results['mix_report'] = raw_mix + '.mix.json'
                            if params.get('mix_bus_profile') == 'gentle':
                                results['bus_report'] = output + '.dsp.json'
                            return results
                        mixed = self._run_stage(job_id, "Mix", {"vocal": alignment_result["aligned"], "instrumental": mix_instrumental}, mix_parameters, _run_mix, 0.85)
                        master_input = mixed["mixed"]
                preset = params["effective_master"]
                master_parameters={"preset":master_preset,"effective_master":preset,"sample_rate":output_sample_rate,"formats":sorted(formats)}
                if 'output_channels' in params:master_parameters['output_channels']=params['output_channels']
                render_master = lambda: self._master(master_input, master_dir, os.path.basename(project_dir), formats, preset, output_sample_rate,**({'output_channels':params['output_channels']} if params.get('output_channels',2)!=2 else {}))
                master_result = self._run_stage(job_id, "Master", {"input": master_input}, master_parameters, render_master, 0.90)
                master_wav = master_result["wav"]
                master_mp3 = master_result.get("mp3")
                qc_stems = sorted(os.path.join(stems_dir,n) for n in os.listdir(stems_dir) if n.lower().endswith('.wav'))
                export_repair_receipt = None
                def _run_qc_stage():
                    from core.voice_profile_report import append_effective_profile
                    from core.f0_quality import append_f0_report
                    from core.artifact_score import append_artifact_report
                    from core.voice_quality_flow import append_quality_report
                    from core.stage_quality_trace import append_stage_trace
                    report_path = os.path.join(reports_dir, "process_report.md")
                    try:
                        qc_outputs = self._qc(master_wav, master_mp3, os.path.basename(input_file), mode, model_display_name, sync_offset_ms, meta.get("duration"), preset, alignment_path, reports_dir, stems_dir, master_result.get("flac"), rvc_raw, polish_before if mode == "polish" else None)
                    finally:
                        if mode == "voice_conversion" and os.path.isfile(report_path):
                            append_effective_profile(report_path, index_rate, params.get("effective_rvc") or {}, rvc_model_key)
                            if f0_quality is not None:
                                append_f0_report(report_path, f0_quality)
                            if artifact_record is not None:
                                append_artifact_report(report_path, artifact_record)
                            if quality_record is not None:
                                append_quality_report(report_path, quality_record)
                            pending = sys.exc_info()[1]
                            try:
                                append_stage_trace(report_path, project_dir, source_path=input_file, master_wav=master_wav)
                            except Exception as exc:
                                if pending is None:
                                    raise
                                self.log("阶段质量追踪未写入：" + type(exc).__name__ + ": " + str(exc))
                    if mode == "voice_conversion":
                        trace_path = os.path.join(reports_dir, "stage_quality_trace.json")
                        if os.path.isfile(trace_path):
                            qc_outputs["stage_trace_path"] = trace_path
                    if export_repair_receipt is not None:
                        export_repair_receipt['status']='QC_RECHECKED'
                        export_repair_receipt['qc_status']=qc_outputs['qc_data'].get('status')
                        repair_path=_json_file(os.path.join(reports_dir,'export_repair.json'),export_repair_receipt)
                        qc_outputs['export_repair_report']=repair_path
                    return qc_outputs
                qc_inputs = {"wav": master_wav, "mp3": master_mp3, "flac": master_result.get("flac"), "alignment": alignment_path, "stems": qc_stems, "master_json": master_result.get('master_json')}
                if mode == "voice_conversion":
                    qc_inputs["voice_evidence"] = [rvc_vocal + suffix for suffix in
                        (".f0-track.json", ".raw-qc.json", ".f0-qc.json", ".artifact-qc.json", ".quality-flow.json")
                        if os.path.isfile(rvc_vocal + suffix)]
                qc_parameters={"mode":mode,"expected_duration": meta.get("duration"), "target_lufs": preset["target_lufs"], "true_peak_limit": preset["true_peak"]}
                try:
                    qc_result = self._run_stage(job_id, "QC", qc_inputs, qc_parameters, _run_qc_stage, 0.95)
                except ProcessCancelled:
                    raise
                except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as original_failure:
                    from core.export_repair import corrupt_exports
                    broken=corrupt_exports(master_result,meta.get('duration'))
                    if not broken:raise
                    master_record=self.storage.get_stage(job_id,'Master')
                    current_input=canonical_json(self._input_hash_data(self._input_manifest({'input':master_input})))
                    if not master_record or current_input!=master_record['input_hash']:
                        raise RuntimeError('母带上游输入已变化，拒绝自动重导') from original_failure
                    # Preserve failed bytes and the first ledger before forcing
                    # one Master rebuild. The next QC gets fresh input hashes;
                    # the normal stage input-mutation guard is never bypassed.
                    archive=os.path.join(reports_dir,'export-retry-'+uuid.uuid4().hex)
                    os.makedirs(archive,exist_ok=False)
                    old_paths=list(_output_paths(master_result).values())
                    preserved=[]
                    for index,path in enumerate(old_paths+[os.path.join(reports_dir,'qc_result.json'),os.path.join(reports_dir,'process_report.md')]):
                        if os.path.isfile(path):
                            destination=os.path.join(archive,str(index)+'-'+os.path.basename(path))
                            shutil.copyfile(path,destination)
                            preserved.append({'source':path,'backup':destination,'sha256':sha256_file(destination)})
                    export_repair_receipt=dict(status='RENDERING',retry_limit=1,broken_exports=broken,
                        first_failure=str(original_failure),first_master_stage=master_record,
                        first_qc_stage=self.storage.get_stage(job_id,'QC'),preserved_files=preserved,
                        upstream_input_sha256=sha256_file(master_input),quality_acceptance='READY_FOR_EXTERNAL_VALIDATION')
                    repair_path=_json_file(os.path.join(reports_dir,'export_repair.json'),export_repair_receipt)
                    try:
                        self.storage.invalidate_paths(old_paths)
                        self.storage.upsert_stage(job_id,'Master',status='PENDING',log='Corrupt export; one verified-input rebuild')
                        master_result=self._run_stage(job_id,'Master',{'input':master_input},master_parameters,render_master,.90)
                        master_wav=master_result['wav'];master_mp3=master_result.get('mp3')
                        qc_inputs.update(wav=master_wav,mp3=master_mp3,flac=master_result.get('flac'),master_json=master_result.get('master_json'))
                        qc_result=self._run_stage(job_id,'QC',qc_inputs,qc_parameters,_run_qc_stage,.95)
                    except Exception as repair_failure:
                        export_repair_receipt.update(status='FAILED',retry_failure=str(repair_failure))
                        _json_file(repair_path,export_repair_receipt)
                        raise
                if mode == "polish" and polish_before:
                    from core.polish_listening import ab_manifest
                    trace_dir = os.path.dirname(polish_before)
                    master_trace = os.path.join(trace_dir, "master.wav")
                    if os.path.abspath(master_wav) != os.path.abspath(master_trace):
                        shutil.copyfile(master_wav, master_trace)
                    ab_paths = {"source": polish_before, "repaired": os.path.join(trace_dir, "repaired.wav"), "tone": os.path.join(trace_dir, "tone.wav"), "dynamics": os.path.join(trace_dir, "dynamics.wav"), "stereo": os.path.join(trace_dir, "stereo.wav"), "master": master_trace}
                    _json_file(os.path.join(reports_dir, "polish_ab.json"), ab_manifest(ab_paths, {"inference_ran": True, "artifact": master_wav}, None))
                from core.reporting import license_manifest
                job_source_hash = self.storage.get_job(job_id)['source_hash']
                if sha256_file(input_file) != job_source_hash:
                    raise RuntimeError('处理期间源文件发生变化，拒绝将该任务标为完成；请创建新任务')
                from core.quality_router import route_quality
                quality_plan = route_quality(
                    source_hint=params.get("source_hint", "auto"),
                    quality_mode=params.get("quality_mode", "balanced"),
                    profile=(params.get("effective_rvc") or {}).get("profile"),
                    hardware=params.get("hardware_evidence"),
                    job_spec=params,
                    executed_stages=self.storage.list_stages(job_id),
                    source_policy_record=source_policy_record,
                )
                quality_plan["mode"] = mode
                quality_plan_path = _json_file(
                    os.path.join(reports_dir, "quality_plan.json"), quality_plan)
                license_path = _json_file(os.path.join(project_dir, "license-manifest.json"), license_manifest(self.storage,mode,rvc_model_key,model_display_name,input_file,sha256_file(input_file)))
                elapsed = time.time() - start_time
                manifest = {"job_id": job_id, "project_name": os.path.basename(project_dir), "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "elapsed_seconds": round(elapsed, 2), "mode": mode, "clean_preset": clean_preset, "master_preset": master_preset, "rvc_model": rvc_model_key, "pitch_shift": pitch_shift, "f0_method": f0_method, "sync_offset_ms": round(sync_offset_ms, 2), "status": "DONE", "output_paths": {"master_wav": master_wav, "master_mp3": master_mp3, "master_flac": master_result.get("flac"), "stems_dir": stems_dir, "reports_dir": reports_dir, "midi_dir": midi_dir, "quality_plan": quality_plan_path}, "qc": qc_result.get("qc_data", {}), "license_manifest": license_path, "database": self.storage.db_path}
                manifest['source_path'] = input_file
                manifest['source_sha256'] = job_source_hash
                manifest['app_version'] = APP_VERSION
                manifest['job_spec'] = params
                manifest['job_spec_sha256'] = params.get('spec_hash')
                manifest['stage_records'] = self.storage.list_stages(job_id)
                manifest['dependencies'] = {s['stage_name']: {k:s['parameters'].get(k) for k in ('dependency_hashes','package_versions','stage_contract_version')} for s in manifest['stage_records']}
                manifest['worker_execution'] = {}
                for directory, _dirs, filenames in os.walk(project_dir):
                    for filename in filenames:
                        if filename == 'worker.json' or filename.endswith('.worker.json'):
                            path = os.path.join(directory,filename)
                            try:
                                with open(path,encoding='utf-8') as handle:
                                    manifest['worker_execution'][os.path.relpath(path,project_dir)] = json.load(handle)
                            except (OSError,ValueError): pass
                for record in manifest['worker_execution'].values():
                    if len(record.get('recovery_attempts',[])) > 1:
                        self.log('显存容错已降级：'+canonical_json(record))
                self._validate_final_exports(master_result, formats)
                from core.job_spec import validate
                validate(params,mode)
                if not manifest.get('job_spec_sha256'):
                    raise RuntimeError('最终 project.json 缺少可验证的冻结 JobSpec 身份')
                final_stage_records = self.storage.list_stages(job_id)
                for completed in final_stage_records:
                    if completed['status'] == 'DONE' and not _validate_outputs(completed['outputs'],completed['output_hashes']):
                        raise RuntimeError('提交前产物丢失或变化：'+completed['stage_name'])
                manifest['stage_records'] = final_stage_records
                manifest['dependencies'] = {s['stage_name']: {k:s['parameters'].get(k) for k in ('dependency_hashes','package_versions','stage_contract_version')} for s in final_stage_records}
                self.storage.assert_owner(job_id)
                _json_file(os.path.join(project_dir, "project.json"), manifest)
                self.storage.update_job(job_id, status="DONE", current_stage="Complete", progress=1.0, error=None)
                if (manifest.get("qc") or {}).get("processing_integrity") == "DEGRADED":
                    self.log("提示：本次处理包含降级阶段，客观指标通过但部分精修步骤已安全跳过；详情查看质检报告。")
                self.log(f"[+] 成品项目保存在: {project_dir}")
                return manifest
        except ProcessCancelled:
            raise
        except Exception as exc:
            if job_id and not isinstance(exc, JobOwnershipError):
                self.storage.update_job(job_id, status="FAILED", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._active_control = None
            claims.close()

    @staticmethod
    def _validate_final_exports(result, formats):
        for extension in formats:
            path = result.get(extension)
            if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise RuntimeError('必需导出丢失：'+extension)
        if not _validate_outputs(result, _hash_outputs(result)):
            raise RuntimeError('最终产物不完整，拒绝完成任务')

    def process_file(self, *args, **kwargs):
        if not self._execution_lock.acquire(blocking=False):
            raise JobBusyError('此Pipeline实例正在处理任务，请使用独立实例')
        try:
            self._begin_eta_run()
            return self._process_file(*args, **kwargs)
        finally:
            self._execution_lock.release()

