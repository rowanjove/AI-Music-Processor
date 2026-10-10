"""Worker-local exact-length UVR adapter primitives for experiments.

The production UVR worker is intentionally untouched.  This module clones
only trusted function globals into a candidate worker and supplies band
sample counts measured from the actual resampled source arrays.
"""
from __future__ import absolute_import

import types


class ExactLengthError(RuntimeError):
    pass


class BandLengthTracker:
    def __init__(self, model_parameters):
        self._bands = model_parameters.param["band"]
        self._band_ids = sorted(self._bands, reverse=True)
        self._capture_index = 0
        self._lengths = {}
        self._resample_plan = []
        self._resample_index = 0
        self._active_combination = None
        self._combination_index = 0
        self._records = {
            "band_source_lengths": [],
            "istft_lengths": [],
            "resample_length_corrections": [],
        }
        self._key_to_band = {}
        self._sample_rate_to_band = {}
        for band_id, band in self._bands.items():
            key = (int(band["n_fft"]), int(band["hl"]))
            if key in self._key_to_band:
                raise ExactLengthError("UVR band n_fft/hop 键重复，拒绝猜测 source length")
            self._key_to_band[key] = band_id
            self._sample_rate_to_band.setdefault(int(band["sr"]), []).append(band_id)
        for band_id in range(1, max(self._bands)):
            source = self._bands[band_id]
            target = self._bands[band_id + 1]
            self._resample_plan.append({
                "source_band": band_id,
                "target_band": band_id + 1,
                "source_rate": int(source["sr"]),
                "target_rate": int(target["sr"]),
            })

    def record_spectrogram_input(self, wave, hop_length, n_fft):
        if self._capture_index >= len(self._band_ids):
            raise ExactLengthError("UVR 实际 band 输入数量超出冻结模型参数")
        band_id = self._band_ids[self._capture_index]
        band = self._bands[band_id]
        key = (int(n_fft), int(hop_length))
        expected_key = (int(band["n_fft"]), int(band["hl"]))
        if key != expected_key:
            raise ExactLengthError("UVR band 输入顺序/参数与 ModelParameters 不一致")
        shape = getattr(wave, "shape", ())
        if len(shape) < 1 or int(shape[-1]) <= 0:
            raise ExactLengthError("UVR band 实际输入无有效采样")
        sample_count = int(shape[-1])
        self._lengths[key] = sample_count
        self._records["band_source_lengths"].append({
            "band": int(band_id),
            "sample_rate": int(band["sr"]),
            "hop_length": int(band["hl"]),
            "n_fft": int(band["n_fft"]),
            "source_samples": sample_count,
        })
        self._capture_index += 1

    def require_complete_capture(self):
        if self._capture_index != len(self._band_ids):
            raise ExactLengthError("尚未记录每个 UVR band 的真实 source sample_count")

    def istft_length(self, stft_matrix, hop_length):
        shape = getattr(stft_matrix, "shape", ())
        if len(shape) < 2:
            raise ExactLengthError("ISTFT 输入没有可辨识的 band 维度")
        n_fft = (int(shape[-2]) - 1) * 2
        key = (n_fft, int(hop_length))
        if key not in self._lengths:
            raise ExactLengthError("ISTFT 未找到该 band 实测 source length：{}".format(key))
        target = self._lengths[key]
        self._records["istft_lengths"].append({
            "n_fft": n_fft,
            "hop_length": int(hop_length),
            "source_samples": target,
        })
        return target

    def begin_combination(self):
        self.require_complete_capture()
        if self._active_combination is not None:
            raise ExactLengthError("上一个 UVR 输出的频带合成尚未完成")
        labels = ("instrumental", "vocal")
        if self._combination_index >= len(labels):
            raise ExactLengthError("UVR 频带合成调用次数超过两个输出")
        self._active_combination = labels[self._combination_index]
        self._resample_index = 0

    def finish_combination(self):
        if self._active_combination is None:
            raise ExactLengthError("UVR 频带合成状态无效")
        if self._resample_index != len(self._resample_plan):
            raise ExactLengthError("UVR resample 调用数与模型 band 结构不符")
        self._active_combination = None
        self._combination_index += 1

    def resample_target(self, source_rate, target_rate):
        if self._active_combination is None:
            raise ExactLengthError("UVR resample 不在受跟踪的 band 合成中")
        if self._resample_index >= len(self._resample_plan):
            raise ExactLengthError("UVR resample 次数超出 band 参数")
        plan = self._resample_plan[self._resample_index]
        if (int(source_rate), int(target_rate)) != (plan["source_rate"], plan["target_rate"]):
            raise ExactLengthError("UVR resample 采样率与 ModelParameters band 顺序不符")
        target_band = self._bands[plan["target_band"]]
        key = (int(target_band["n_fft"]), int(target_band["hl"]))
        if key not in self._lengths:
            raise ExactLengthError("目标 UVR band 尚无实测 source length")
        expected = self._lengths[key]
        self._resample_index += 1
        return expected, plan

    def correct_resample_rounding(self, audio, expected_samples, plan, numpy_module, api_name,
                                  source_samples):
        actual = int(audio.shape[-1])
        delta = int(expected_samples) - actual
        if delta < -1 or delta > 1:
            raise ExactLengthError(
                "UVR band resample 长度偏差超过 1 sample：{} -> {} ({:+d})".format(
                    actual, expected_samples, delta
                )
            )
        if delta == 1:
            if actual <= 0:
                raise ExactLengthError("不能对空 resample 结果做边界校正")
            audio = numpy_module.concatenate((audio, audio[..., -1:]), axis=-1)
            kind = "repeat_last_sample"
        elif delta == -1:
            audio = audio[..., :expected_samples]
            kind = "remove_rounding_excess_sample"
        else:
            kind = "none"
        self._records["resample_length_corrections"].append({
            "output": self._active_combination,
            "api": api_name,
            "source_band": int(plan["source_band"]),
            "target_band": int(plan["target_band"]),
            "source_rate": int(plan["source_rate"]),
            "target_rate": int(plan["target_rate"]),
            "source_samples_before_resample": int(source_samples),
            "resampled_samples_before_correction": actual,
            "expected_samples_from_measured_target_band": int(expected_samples),
            "correction_samples": int(delta),
            "correction": kind,
        })
        return audio

    def finish(self):
        self.require_complete_capture()
        if self._active_combination is not None or self._combination_index != 2:
            raise ExactLengthError("UVR 没有完成伴奏与人声的精确长度合成")
        if not self._records["istft_lengths"]:
            raise ExactLengthError("UVR 没有记录 ISTFT source lengths")
        return {
            "adapter": "candidate_local_exact_istft_length",
            "method": "captured_actual_wave_to_spectrogram_band_samples",
            "band_source_lengths": list(self._records["band_source_lengths"]),
            "istft_lengths": list(self._records["istft_lengths"]),
            "resample_length_corrections": list(self._records["resample_length_corrections"]),
            "combination_count": self._combination_index,
        }

    def partial_evidence(self):
        return {
            "adapter": "candidate_local_exact_istft_length",
            "band_source_lengths": list(self._records["band_source_lengths"]),
            "istft_lengths": list(self._records["istft_lengths"]),
            "resample_length_corrections": list(self._records["resample_length_corrections"]),
            "combination_count": self._combination_index,
            "active_combination": self._active_combination,
        }


class _CoreResampleProxy:
    def __init__(self, original, tracker, numpy_module, api_name):
        self._original = original
        self._tracker = tracker
        self._numpy = numpy_module
        self._api_name = api_name

    def __getattr__(self, name):
        return getattr(self._original, name)

    def resample(self, audio, orig_sr, target_sr, *args, **kwargs):
        expected, plan = self._tracker.resample_target(orig_sr, target_sr)
        source_samples = int(audio.shape[-1])
        result = self._original.resample(audio, orig_sr, target_sr, *args, **kwargs)
        return self._tracker.correct_resample_rounding(
            result, expected, plan, self._numpy, self._api_name, source_samples
        )


class _LibrosaProxy:
    def __init__(self, original, tracker, numpy_module):
        self._original = original
        self._tracker = tracker
        self._numpy = numpy_module
        self.core = _CoreResampleProxy(original.core, tracker, numpy_module, "librosa.core.resample")

    def __getattr__(self, name):
        return getattr(self._original, name)

    def resample(self, audio, orig_sr, target_sr, *args, **kwargs):
        expected, plan = self._tracker.resample_target(orig_sr, target_sr)
        source_samples = int(audio.shape[-1])
        result = self._original.resample(audio, orig_sr, target_sr, *args, **kwargs)
        return self._tracker.correct_resample_rounding(
            result, expected, plan, self._numpy, "librosa.resample", source_samples
        )

    def istft(self, stft_matrix, *args, **kwargs):
        if "hop_length" in kwargs:
            hop_length = kwargs["hop_length"]
        elif len(args) >= 1:
            hop_length = args[0]
        else:
            hop_length = None
        if hop_length is None:
            raise ExactLengthError("ISTFT 未显式提供 hop_length，拒绝猜测 band")
        expected = self._tracker.istft_length(stft_matrix, hop_length)
        if "length" in kwargs and int(kwargs["length"]) != expected:
            raise ExactLengthError("ISTFT 调用方 length 与实测 band source count 不符")
        kwargs["length"] = expected
        return self._original.istft(stft_matrix, *args, **kwargs)


class _ZeroBandNumpyProxy:
    """Only the cloned trusted combination uses initialized band storage."""
    def __init__(self,original):self._original=original
    def __getattr__(self,name):return getattr(self._original,name)
    def ndarray(self,shape,dtype=float,**kwargs):
        if set(kwargs)-{'order'}:
            raise ExactLengthError('Unsupported band-array constructor; refusing to change semantics')
        return self._original.zeros(shape,dtype=dtype,order=kwargs.get('order','C'))


class _SpecUtilsProxy:
    def __init__(self, original, tracker, numpy_module):
        self._original = original
        self._tracker = tracker
        self._numpy = numpy_module
        librosa_proxy = _LibrosaProxy(original.librosa, tracker, numpy_module)
        local_spectrogram_to_wave = _clone_function(
            original.spectrogram_to_wave,
            {"librosa": librosa_proxy},
        )
        self._cmb = _clone_function(
            original.cmb_spectrogram_to_wave,
            {"librosa": librosa_proxy, "spectrogram_to_wave": local_spectrogram_to_wave,
             'np':_ZeroBandNumpyProxy(numpy_module)},
        )

    def __getattr__(self, name):
        return getattr(self._original, name)

    def wave_to_spectrogram_mt(self, wave, hop_length, n_fft, *args, **kwargs):
        self._tracker.record_spectrogram_input(wave, hop_length, n_fft)
        return self._original.wave_to_spectrogram_mt(wave, hop_length, n_fft, *args, **kwargs)

    def cmb_spectrogram_to_wave(self, *args, **kwargs):
        self._tracker.begin_combination()
        result = self._cmb(*args, **kwargs)
        self._tracker.finish_combination()
        return result


def build_exact_length_spec_proxy(spec_utils_module, model_parameters, numpy_module):
    """Return a local proxy suitable for one cloned UVR ``_path_audio_`` call."""
    tracker = BandLengthTracker(model_parameters)
    return _SpecUtilsProxy(spec_utils_module, tracker, numpy_module), tracker


def clone_method_with_spec_proxy(method, spec_utils_proxy):
    """Clone trusted bound method code with only its module global replaced."""
    function = getattr(method, "__func__", None)
    if function is None:
        raise TypeError("UVR candidate expects the trusted bound _path_audio_ method")
    cloned = _clone_function(function, {"spec_utils": spec_utils_proxy})
    return types.MethodType(cloned, method.__self__)


def _clone_function(function, overrides):
    if not isinstance(function, types.FunctionType):
        raise TypeError("只允许克隆真实 Python function")
    namespace = dict(function.__globals__)
    namespace.update(overrides)
    clone = types.FunctionType(
        function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__
    )
    clone.__kwdefaults__ = getattr(function, "__kwdefaults__", None)
    return clone
