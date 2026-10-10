"""Audio/tool stage implementations; orchestration stays in AudioPipeline."""
from __future__ import annotations
import os, inspect
from typing import Any, Mapping, Optional
from core.config import FFMPEG_EXE
from core.pipeline_support import (_json_file, _existing_path, _copy_if_file, _normalise_stems, separate_four_stems)
from core.analyzer import probe_metadata, standardize_to_48k_float
from core.decryptor import process_input_audio
from core.cleaner import clean_audio_stream
from core.separator import resolve_voice_backend, separate_audio, separate_dual_audio
from core.aligner import align_vocal_timing
from core.mastering import master_bus
from core.qc import run_quality_control
from core.processes import run as process_run
import json

class StageOperations:
    def _call_master(self, input_path: str, master_wav: str, master_mp3: Optional[str], master_flac: Optional[str], target_lufs: float, true_peak_limit: float, sample_rate: int, output_channels=2) -> dict[str, Optional[str]]:
        kwargs: dict[str, Any] = {"target_lufs": target_lufs, "true_peak_limit": true_peak_limit}
        parameters = inspect.signature(master_bus).parameters
        if "sample_rate" in parameters:
            kwargs["sample_rate"] = sample_rate
        if "output_flac" in parameters:
            kwargs["output_flac"] = master_flac
        if output_channels!=2:
            if 'output_channels' not in parameters:raise ValueError('母带实现不支持所选输出声道')
            kwargs['output_channels']=output_channels
        master_bus(input_path, master_wav, master_mp3, **kwargs)
        return {"wav": master_wav, "mp3": master_mp3, "flac": master_flac, 'master_json':master_wav+'.json'}

    def _run_qc(self, master_wav: str, master_mp3: Optional[str], original_file_name: str, mode: str, model_name: str, sync_offset_ms: float, expected_duration: Optional[float], target_lufs: float, true_peak_limit: float, alignment_path: Optional[str], stem_paths: list[str], extra_exports: list[str], rvc_raw: Optional[dict] = None, polish_before: Optional[str] = None) -> tuple[dict[str, Any], str]:
        kwargs: dict[str, Any] = {"master_wav_path": master_wav, "master_mp3_path": master_mp3, "original_file_name": original_file_name, "mode_name": mode, "model_name": model_name, "sync_offset_ms": sync_offset_ms, "expected_duration": expected_duration, "target_lufs": target_lufs, "true_peak_limit": true_peak_limit, "alignment_path": alignment_path, "stem_paths": stem_paths, "extra_exports": extra_exports, "rvc_raw": rvc_raw, "polish_before": polish_before}
        params = inspect.signature(run_quality_control).parameters
        if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in params.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in params}
        return run_quality_control(**kwargs)

    @staticmethod
    def _clean(input_path: str, output_path: str, preset: str, vocal: bool = False, effective=None) -> str:
        kwargs: dict[str, Any] = {"preset_key": preset}
        if "vocal" in inspect.signature(clean_audio_stream).parameters:
            kwargs["vocal"] = vocal
        if effective is not None:
            kwargs["custom_filter"] = effective["filter"]
        return clean_audio_stream(input_path, output_path, **kwargs)

    @staticmethod
    def _ingest(input_file: str, job_temp: str, mode=None, policy=None, input_channel_mode='auto') -> dict[str, Any]:
        actual_input, _ = process_input_audio(input_file, job_temp)
        # Re-probe decrypted compressed input before allocating the float WAV.
        meta = probe_metadata(actual_input)
        from core.resource_limits import require_disk_budget,require_processing_budget
        require_disk_budget(actual_input,os.path.dirname(os.path.dirname(job_temp)),meta['duration'])
        if mode:require_processing_budget(float(meta['duration']),mode,policy)
        standard_wav = os.path.join(job_temp, "01_standard_input.wav")
        standardize_to_48k_float(actual_input, standard_wav,**({'input_channel_mode':input_channel_mode} if input_channel_mode!='auto' else {}))
        return {"standard_wav": standard_wav, "metadata": probe_metadata(standard_wav)}

    @staticmethod
    def _analyze(standard_wav: str, reports_dir: str, detailed_analysis=True) -> dict[str, Any]:
        try:
            from core.analyzer import analyze_audio
            analysis = analyze_audio(standard_wav,**({'detailed_analysis':False} if not detailed_analysis else {}))
        except (ImportError, AttributeError):
            analysis = {"metadata": probe_metadata(standard_wav)}
        if not isinstance(analysis, Mapping):
            analysis = {"metadata": probe_metadata(standard_wav), "value": analysis}
        metadata = analysis.get("metadata") or probe_metadata(standard_wav)
        report = _json_file(os.path.join(reports_dir, "input_analysis.json"), dict(analysis))
        return {"metadata": metadata, "analysis": dict(analysis), "report": report}

    @staticmethod
    def _preclean_source(source_wav: str, output_wav: str, report_path: str,
                         source_hint: str = "auto", enabled: bool = False) -> dict[str, Any]:
        from core.source_policy import apply_source_preclean
        report = apply_source_preclean(source_wav, output_wav, report_path,
                                       source_hint=source_hint, enabled=enabled)
        return {"cleaned": report["output_path"], "report_path": report_path}

    @staticmethod
    def _separate_and_copy(standard_wav: str, job_temp: str, stems_dir: str, mode: str, backend: Optional[str] = None, execution_policy=None) -> dict[str, Optional[str]]:
        if mode == "voice_conversion":
            spec = resolve_voice_backend(backend)
            vocal = os.path.join(job_temp, "separate", "vocal.wav")
            instrumental = os.path.join(job_temp, "separate", "instrumental.wav")
            os.makedirs(os.path.dirname(vocal), exist_ok=True)
            separate_audio(standard_wav, vocal, instrumental, model_name=spec["model_name"], agg=spec["agg"], **({"execution_policy": execution_policy} if execution_policy is not None else {}))
            return {"vocal": _copy_if_file(vocal, os.path.join(stems_dir, "01_vocal_original.wav")), "other": _copy_if_file(instrumental, os.path.join(stems_dir, "04_instrumental.wav")), "drums": None, "bass": None, "instrumental": _copy_if_file(instrumental, os.path.join(stems_dir, "04_instrumental.wav")), 'worker_path':_copy_if_file(vocal+'.worker.json',os.path.join(stems_dir,'separation.worker.json'))}
        result = _normalise_stems(separate_four_stems(standard_wav, os.path.join(job_temp, "separate"), **({"execution_policy": execution_policy} if execution_policy is not None else {})))
        targets = {"vocal": "01_vocal_original.wav", "other": "04_other.wav", "drums": "05_drums.wav", "bass": "06_bass.wav"}
        copied = {name: _copy_if_file(result.get(name), os.path.join(stems_dir, filename)) for name, filename in targets.items()}
        tracks = [copied[name] for name in ("drums", "bass", "other") if copied.get(name)]
        instrumental = StageOperations._mix_instrumental(tracks, os.path.join(stems_dir, "04_instrumental.wav")) if tracks else None
        copied["instrumental"] = instrumental
        copied['worker_path'] = _copy_if_file(os.path.join(job_temp,'separate','worker.json'),os.path.join(stems_dir,'separation.worker.json'))
        return copied

    @staticmethod
    def _separate_dual_and_copy(
        standard_wav: str,
        job_temp: str,
        stems_dir: str,
        mode: str = "voice_conversion",
        profile: Optional[dict] = None,
        artifacts: Optional[dict] = None,
        execution_policy=None,
    ) -> dict[str, Optional[str]]:
        """Run both frozen MDX models and preserve all four stems for diagnosis."""
        if mode != "voice_conversion":
            raise ValueError("双模型分离只适用于换声模式")
        from core.separator_routing import dual_profile, validate_separator_profile

        checked = validate_separator_profile(profile or dual_profile())
        if checked["mode"] != "dual":
            raise ValueError("双模型分离需要 Kim Vocal 2 + Inst HQ 3 配置")
        output_dir = os.path.join(job_temp, "separate-dual")
        separated = separate_dual_audio(standard_wav, output_dir, artifacts=artifacts, **({"execution_policy": execution_policy} if execution_policy is not None else {}))

        extra_names = {
            "kim_vocal": "kim_vocal.wav",
            "kim_instrumental": "kim_instrumental.wav",
            "insthq3_vocal": "insthq3_vocal.wav",
            "insthq3_instrumental": "insthq3_instrumental.wav",
        }
        extra_audio_paths = {
            key: _copy_if_file(separated[key], os.path.join(stems_dir, filename))
            for key, filename in extra_names.items()
        }
        vocal = _copy_if_file(extra_audio_paths["kim_vocal"], os.path.join(stems_dir, "01_vocal_original.wav"))
        instrumental = _copy_if_file(extra_audio_paths["insthq3_instrumental"], os.path.join(stems_dir, "04_instrumental.wav"))
        worker_path = _copy_if_file(separated.get("worker_path"), os.path.join(stems_dir, "separation.worker.json"))
        return {
            "vocal": vocal,
            "instrumental": instrumental,
            "other": instrumental,
            "drums": None,
            "bass": None,
            "worker_path": worker_path,
            **extra_audio_paths,
            "extra_audio_paths": extra_audio_paths,
        }

    @staticmethod
    def _convert_voice(input_path, output_path, **kwargs):
        from core.rvc_engine import convert_voice
        result = convert_voice(input_path, output_path, **kwargs)
        record = {'rvc': result, 'worker_path': output_path + '.worker.json'}
        f0_track_path = output_path + '.f0-track.json'
        if os.path.isfile(f0_track_path):
            record['f0_track_path'] = f0_track_path
        return record

    @staticmethod
    def _mix_instrumental(tracks: list[str], output: str) -> Optional[str]:
        if not tracks:
            return None
        if len(tracks) == 1:
            return _copy_if_file(tracks[0], output)
        command = [FFMPEG_EXE, "-y", "-v", "error"]
        for track in tracks:
            command.extend(("-i", track))
        labels = "".join(f"[{index}:a]" for index in range(len(tracks)))
        command.extend(("-filter_complex", f"{labels}amix=inputs={len(tracks)}:duration=longest:normalize=0[mix]", "-map", "[mix]", "-c:a", "pcm_f32le", "-ar", "48000", output))
        process_run(command)
        return output if os.path.isfile(output) else None

    @staticmethod
    def _align(reference: str, rvc: str, output: str) -> dict[str, Any]:
        result = align_vocal_timing(reference, rvc, output)
        if not isinstance(result, (tuple, list)):
            aligned, offset, extra = output, 0.0, {}
        else:
            aligned = result[0] if result else output
            offset = float(result[1]) if len(result) > 1 else 0.0
            extra = {"correlation": result[2]} if len(result) > 2 else {}
        aligned = _existing_path(aligned) or os.path.abspath(output)
        alignment_json = output + ".json"
        alignment_record: dict[str, Any] = {}
        if os.path.isfile(alignment_json):
            try:
                with open(alignment_json, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, Mapping):
                    alignment_record.update(loaded)
            except (OSError, ValueError):
                pass
        alignment_record.update({"offset_ms": offset, **extra, "reference": reference, "rvc": rvc, "aligned": aligned})
        # New aligners provide residual/verification fields; old two-value
        # aligners remain readable and fail QC closed instead of crashing it.
        alignment_record.setdefault("residual_ms", abs(offset))
        alignment_record.setdefault("max_local_residual_ms", abs(offset))
        alignment_record.setdefault("verified", False)
        _json_file(alignment_json, alignment_record)
        return {"aligned": aligned, "offset_ms": offset, "alignment_json": alignment_json}

    def _master(self, master_input: str, master_dir: str, title: str, formats: set[str], preset: Mapping[str, Any], sample_rate: int, output_channels=2) -> dict[str, Optional[str]]:
        wav = os.path.join(master_dir, f"{title}_MASTER.wav")
        mp3 = os.path.join(master_dir, f"{title}_MASTER.mp3") if "mp3" in formats else None
        flac = os.path.join(master_dir, f"{title}_MASTER.flac") if "flac" in formats else None
        return self._call_master(master_input, wav, mp3, flac, float(preset["target_lufs"]), float(preset["true_peak"]), int(sample_rate),**({'output_channels':output_channels} if output_channels!=2 else {}))

    def _qc(self, master_wav: str, master_mp3: Optional[str], original_name: str, mode: str, model_name: str, sync_offset_ms: float, expected_duration: Optional[float], preset: Mapping[str, Any], alignment_path: Optional[str], reports_dir: str, stems_dir: str, master_flac: Optional[str], rvc_raw: Optional[dict] = None, polish_before: Optional[str] = None) -> dict[str, Any]:
        stem_paths = [os.path.join(stems_dir, name) for name in os.listdir(stems_dir) if name.lower().endswith(".wav") and os.path.isfile(os.path.join(stems_dir, name))]
        extra_exports = [master_flac] if master_flac else []
        qc_data, report_md = self._run_qc(master_wav, master_mp3, original_name, mode, model_name, sync_offset_ms, expected_duration, float(preset["target_lufs"]), float(preset["true_peak"]), alignment_path, stem_paths, extra_exports, rvc_raw=rvc_raw, polish_before=polish_before)
        qc_path = _json_file(os.path.join(reports_dir, "qc_result.json"), qc_data)
        report_path = os.path.join(reports_dir, "process_report.md")
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(report_md)
            handle.write("\n\n## Durable pipeline\n\n")
            handle.write(f"- QC JSON: `{qc_path}`\n- Alignment JSON: `{alignment_path or 'N/A'}`\n- License status is recorded in `license-manifest.json`.\n")
            from core.reporting import dependency_evidence
            store = getattr(self, 'storage', None)
            job_id = store.project_job_id(os.path.dirname(reports_dir)) if store is not None else None
            handle.write(dependency_evidence({'stage_records': store.list_stages(job_id) if job_id else []}))
        if str(qc_data.get("status", "")).upper() == "FAIL":
            raise RuntimeError("QC FAIL：已写入质检报告，成品未标记为完成")
        return {"qc_data": qc_data, "qc_result": qc_path, "report": report_path}
