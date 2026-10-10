"""CPU ONNX MDX inference helpers for the bundled UVR separator models.

The two supported models use the standard UVR MDX STFT contract.  Model
dimensions and gain compensation come from the installed UVR model catalog,
selected by the MD5 of the final 10 MiB of each ONNX file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Mapping


SAMPLE_RATE = 44100
HOP_LENGTH = 1024
MDX_METADATA_TAIL_BYTES = 10000 * 1024
DEFAULT_MDX_WEIGHT_DIR = Path(__file__).resolve().parents[1] / "tools" / "UVR5" / "models" / "MDX_Net_Models"
DEFAULT_MDX_METADATA = DEFAULT_MDX_WEIGHT_DIR / "model_data" / "model_data.json"
MDX_FILENAMES = {
    "kim_vocal_2": "Kim_Vocal_2.onnx",
    "inst_hq_3": "UVR-MDX-NET-Inst_HQ_3.onnx",
}


class MdxModelError(RuntimeError):
    """The installed model or its fixed UVR metadata is not supported."""


class MdxProviderError(MdxModelError):
    """The requested ONNX execution provider is not available or active."""


def tail_md5(path: str | os.PathLike[str], tail_bytes: int = MDX_METADATA_TAIL_BYTES) -> str:
    """Return UVR's model hash: MD5 of the last 10 MiB, or the whole short file."""
    if type(tail_bytes) is bool or not isinstance(tail_bytes, int) or tail_bytes <= 0:
        raise ValueError("tail_bytes 必须是正整数")
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - tail_bytes), os.SEEK_SET)
        digest = hashlib.md5()
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def full_sha256(path: str | os.PathLike[str]) -> str:
    """Hash a complete model for a frozen JobSpec identity."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model_metadata(
    model_path: str | os.PathLike[str],
    metadata_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve and validate the model's shipped UVR MDX parameters."""
    model = Path(model_path).resolve()
    if not model.is_file() or model.stat().st_size <= 0:
        raise FileNotFoundError("未找到 MDX 权重：" + str(model))
    catalog = Path(metadata_path).resolve() if metadata_path else DEFAULT_MDX_METADATA
    try:
        metadata_bytes = catalog.read_bytes()
        catalog_sha256 = hashlib.sha256(metadata_bytes).hexdigest()
        rows = json.loads(metadata_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise MdxModelError("无法读取 UVR MDX 模型目录：" + str(exc)) from exc
    if not isinstance(rows, dict):
        raise MdxModelError("UVR MDX 模型目录格式无效")

    model_hash = tail_md5(model)
    raw = rows.get(model_hash)
    if not isinstance(raw, dict):
        raise MdxModelError("模型尾部 MD5 未登记在 UVR model_data.json：" + model_hash)

    dim_f = _positive_int(raw.get("mdx_dim_f_set"), "mdx_dim_f_set")
    dim_t_exponent = _positive_int(raw.get("mdx_dim_t_set"), "mdx_dim_t_set")
    n_fft = _positive_int(raw.get("mdx_n_fft_scale_set"), "mdx_n_fft_scale_set")
    compensate = raw.get("compensate")
    if isinstance(compensate, bool) or not isinstance(compensate, (int, float)):
        raise MdxModelError("UVR compensate 必须是有限正数")
    compensate = float(compensate)
    if not math.isfinite(compensate) or compensate <= 0:
        raise MdxModelError("UVR compensate 必须是有限正数")
    primary_stem = raw.get("primary_stem")
    if primary_stem not in ("Vocals", "Instrumental"):
        raise MdxModelError("不支持的 MDX primary_stem：" + str(primary_stem))
    if n_fft % 2:
        raise MdxModelError("MDX n_fft 必须是偶数")
    if dim_f > n_fft // 2 + 1:
        raise MdxModelError("MDX dim_f 超出 n_fft 的频率范围")
    dim_t = 2 ** dim_t_exponent
    if dim_t < 2:
        raise MdxModelError("MDX 时间维度过小")
    return {
        "model_path": str(model),
        "model_hash_tail_md5": model_hash,
        "metadata_path": str(catalog),
        "metadata_sha256": catalog_sha256,
        "dim_f": dim_f,
        "dim_t": dim_t,
        "dim_t_exponent": dim_t_exponent,
        "n_fft": n_fft,
        "hop_length": HOP_LENGTH,
        "sample_rate": SAMPLE_RATE,
        "primary_stem": primary_stem,
        "compensate": compensate,
    }


def model_identity(
    model_path: str | os.PathLike[str],
    metadata_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return the exact path, content hashes, and verified UVR settings to freeze."""
    model = Path(model_path).resolve()
    initial_sha256 = full_sha256(model)
    metadata = load_model_metadata(model, metadata_path)
    final_sha256 = full_sha256(model)
    if final_sha256 != initial_sha256:
        raise MdxModelError("生成 MDX 身份期间权重文件发生变化")
    return {
        "path": str(model),
        "filename": model.name,
        "size_bytes": model.stat().st_size,
        "sha256": final_sha256,
        "metadata_path": metadata["metadata_path"],
        "metadata_tail_md5": metadata["model_hash_tail_md5"],
        "metadata_sha256": metadata["metadata_sha256"],
        "metadata": {key: metadata[key] for key in (
            "dim_f", "dim_t", "dim_t_exponent", "n_fft", "hop_length",
            "sample_rate", "primary_stem", "compensate",
        )},
    }


def model_path_for_id(model_id: str, weight_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a supported model ID under the installed UVR MDX weights directory."""
    try:
        filename = MDX_FILENAMES[model_id]
    except KeyError as exc:
        raise ValueError("未知的 MDX 模型：" + str(model_id)) from exc
    root = Path(weight_dir).resolve() if weight_dir is not None else DEFAULT_MDX_WEIGHT_DIR
    return (root / filename).resolve()


def dual_model_identities(weight_dir: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """Return the real paths and hashes to freeze for a dual-separator JobSpec."""
    return {
        "vocal": model_identity(model_path_for_id("kim_vocal_2", weight_dir)),
        "instrumental": model_identity(model_path_for_id("inst_hq_3", weight_dir)),
    }


def verify_model_identity(
    model_path: str | os.PathLike[str],
    expected: Mapping[str, Any] | None,
    metadata_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Check a current model against a frozen identity before inference."""
    current = model_identity(model_path, metadata_path)
    if expected is None:
        return current
    if not isinstance(expected, Mapping):
        raise MdxModelError("冻结的 MDX 模型身份格式无效")
    if not expected.get("path") or not expected.get("sha256"):
        raise MdxModelError("冻结的 MDX 模型身份缺少 path 或 SHA-256")
    for key in ("path", "filename", "size_bytes", "sha256", "metadata_path", "metadata_tail_md5", "metadata_sha256"):
        if key in expected and expected.get(key) != current.get(key):
            raise MdxModelError("MDX 模型与任务冻结身份不一致：" + key)
    metadata = expected.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise MdxModelError("冻结的 MDX metadata 格式无效")
        for key, value in metadata.items():
            if current["metadata"].get(key) != value:
                raise MdxModelError("MDX 模型与任务冻结 metadata 不一致：" + str(key))
    return current


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MdxModelError("UVR " + label + " 必须是正整数")
    return value


def _require_finite_audio(audio, label: str) -> None:
    import numpy as np

    if not isinstance(audio, np.ndarray) or audio.ndim != 2 or audio.shape[1] != 2:
        raise MdxModelError(label + " 必须是双声道帧数组")
    if audio.shape[0] <= 0 or not np.isfinite(audio).all():
        raise MdxModelError(label + " 为空或包含 NaN/Inf")


def read_stereo_float(path: str | os.PathLike[str]):
    """Read an audio file as finite stereo float32 without normalizing its level."""
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    elif audio.shape[1] != 2:
        raise MdxModelError("MDX 输入必须是单声道或双声道")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise MdxModelError("音频采样率无效")
    _require_finite_audio(audio, "MDX 输入")
    return np.ascontiguousarray(audio, dtype=np.float32), sample_rate


def resample_stereo(audio, source_rate: int, target_rate: int):
    """Polyphase resample a frame-major stereo array and retain exact expected length."""
    import numpy as np
    from scipy.signal import resample_poly

    if source_rate == target_rate:
        return np.ascontiguousarray(audio, dtype=np.float32)
    divisor = math.gcd(int(source_rate), int(target_rate))
    up = target_rate // divisor
    down = source_rate // divisor
    result = resample_poly(audio, up, down, axis=0).astype(np.float32, copy=False)
    expected = int(round(audio.shape[0] * target_rate / source_rate))
    if result.shape[0] > expected:
        result = result[:expected]
    elif result.shape[0] < expected:
        result = np.pad(result, ((0, expected - result.shape[0]), (0, 0)))
    return np.ascontiguousarray(result)


class _MdxStft:
    """UVR's four-channel real/imaginary STFT and inverse for MDX ONNX."""

    def __init__(self, torch_module, metadata: Mapping[str, Any], device):
        self.torch = torch_module
        self.n_fft = int(metadata["n_fft"])
        self.hop = int(metadata["hop_length"])
        self.dim_f = int(metadata["dim_f"])
        self.device = device
        self.window = torch_module.hann_window(self.n_fft, periodic=True, dtype=torch_module.float32, device=device)

    def forward(self, audio):
        torch = self.torch
        batch_dims = audio.shape[:-2]
        channels, frames = audio.shape[-2:]
        flat = audio.reshape(-1, frames)
        spectrum = torch.stft(
            flat, n_fft=self.n_fft, hop_length=self.hop, window=self.window,
            center=True, return_complex=False,
        )
        spectrum = spectrum.permute(0, 3, 1, 2)
        spectrum = spectrum.reshape(*batch_dims, channels * 2, -1, spectrum.shape[-1])
        return spectrum[..., :self.dim_f, :].contiguous()

    def inverse(self, spectrum, length: int):
        torch = self.torch
        batch_dims = spectrum.shape[:-3]
        channels, frequency_bins, frames = spectrum.shape[-3:]
        total_bins = self.n_fft // 2 + 1
        if channels != 4 or frequency_bins != self.dim_f or self.dim_f > total_bins:
            raise MdxModelError("MDX 输出 shape 与模型目录不一致")
        padding = torch.zeros(
            (*batch_dims, channels, total_bins - frequency_bins, frames),
            dtype=spectrum.dtype,
            device=spectrum.device,
        )
        spectrum = torch.cat([spectrum, padding], dim=-2)
        spectrum = spectrum.reshape(*batch_dims, channels // 2, 2, total_bins, frames)
        spectrum = spectrum.reshape(-1, 2, total_bins, frames).permute(0, 2, 3, 1)
        complex_spec = torch.complex(spectrum[..., 0], spectrum[..., 1])
        audio = torch.istft(
            complex_spec, n_fft=self.n_fft, hop_length=self.hop,
            window=self.window, center=True, length=length,
        )
        return audio.reshape(*batch_dims, 2, -1)


def _validate_onnx_shape(session, metadata: Mapping[str, Any]) -> tuple[str, str]:
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise MdxModelError("MDX ONNX 必须只有一个输入和一个输出")
    input_shape = inputs[0].shape
    output_shape = outputs[0].shape
    expected_tail = [4, int(metadata["dim_f"]), int(metadata["dim_t"])]
    for label, shape in (("输入", input_shape), ("输出", output_shape)):
        if len(shape) != 4 or list(shape[1:]) != expected_tail:
            raise MdxModelError("MDX ONNX " + label + " shape 与 UVR model_data.json 不一致")
    return inputs[0].name, outputs[0].name


def _provider_plan(preference: str, available, torch_module):
    if preference not in ("auto", "cpu", "cuda"):
        raise ValueError("MDX provider 只能是 auto、cpu 或 cuda")
    available = list(available)
    has_cuda = "CUDAExecutionProvider" in available
    torch_cuda = bool(torch_module.cuda.is_available())
    if preference == "cpu":
        return ["CPUExecutionProvider"], "CPUExecutionProvider", None
    if preference == "cuda":
        if not has_cuda:
            raise MdxProviderError("请求 CUDA，但 ONNX Runtime 没有 CUDAExecutionProvider")
        if not torch_cuda:
            raise MdxProviderError("请求 CUDA，但 PyTorch STFT/ISTFT 没有可用 CUDA 设备")
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDAExecutionProvider", None
    if has_cuda and torch_cuda:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDAExecutionProvider", None
    reason = "CUDAExecutionProvider 不可用" if not has_cuda else "PyTorch CUDA 不可用"
    return ["CPUExecutionProvider"], "CPUExecutionProvider", reason


def _looks_like_cuda_provider_error(exc):
    message = (type(exc).__name__ + ": " + str(exc)).lower()
    return any(token in message for token in (
        "failed to create cudaexecutionprovider", "cuda failure", "cuda error",
        "cudaexecutionprovider is not enabled", "cudnn", "cublas",
        "provider factory", "provider options", "failed to load library",
    ))


def _run_model_chunk(session, input_name: str, output_name: str, stft: _MdxStft, wave, length: int):
    import numpy as np

    spectrum = stft.forward(wave)
    spectrum[:, :, :3, :] = 0
    feed = spectrum.detach().to("cpu").numpy()
    prediction = session.run([output_name], {input_name: feed})[0]
    prediction = np.asarray(prediction, dtype=np.float32)
    if prediction.shape != feed.shape or not np.isfinite(prediction).all():
        raise MdxModelError("MDX ONNX 输出 shape 无效或包含 NaN/Inf")
    tensor = stft.torch.from_numpy(prediction).to(stft.device)
    audio = stft.inverse(tensor, length)
    result = audio.detach().to("cpu").numpy()
    if result.shape != (1, 2, length) or not np.isfinite(result).all():
        raise MdxModelError("MDX ISTFT 输出 shape 无效或包含 NaN/Inf")
    return result


def separate_model_array(
    audio_44100,
    model_path: str | os.PathLike[str],
    metadata: Mapping[str, Any] | None = None,
    *,
    device: str = "cpu",
    session=None,
    session_factory: Callable | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    execution_callback: Callable[[Mapping[str, Any]], None] | None = None,
    cpu_threads: int | None = None,
):
    """Run one MDX model over 44.1 kHz stereo and report the active provider."""
    import numpy as np
    import torch

    _require_finite_audio(audio_44100, "MDX 输入")
    metadata = dict(metadata or load_model_metadata(model_path))
    if int(metadata.get("sample_rate", 0)) != SAMPLE_RATE or int(metadata.get("hop_length", 0)) != HOP_LENGTH:
        raise MdxModelError("MDX 模型必须使用 44.1 kHz 和 hop 1024")
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("MDX 设备偏好必须是 auto、cpu 或 cuda")

    if cpu_threads is not None and (type(cpu_threads) is not int or not 1 <= cpu_threads <= 4):
        raise ValueError('MDX CPU threads必须在1到4之间')
    if cpu_threads is not None:
        torch.set_num_threads(cpu_threads)
    provider_fallback_reason = None
    if session is None:
        import onnxruntime as ort

        providers, selected_provider, provider_fallback_reason = _provider_plan(
            device, ort.get_available_providers(), torch)
        options = ort.SessionOptions()
        options.intra_op_num_threads = cpu_threads or max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if session_factory is None:
            session_factory = ort.InferenceSession
        try:
            session = session_factory(str(model_path), options, providers=providers)
        except Exception as exc:
            if (device == "auto" and selected_provider == "CUDAExecutionProvider"
                    and _looks_like_cuda_provider_error(exc)):
                provider_fallback_reason = "CUDA provider 初始化失败：" + type(exc).__name__
                session = session_factory(str(model_path), options, providers=["CPUExecutionProvider"])
                selected_provider = "CPUExecutionProvider"
            else:
                raise
    session_providers = list(getattr(session, "get_providers", lambda: ["CPUExecutionProvider"])())
    if not session_providers or any(provider not in ("CUDAExecutionProvider", "CPUExecutionProvider") for provider in session_providers):
        raise MdxProviderError("MDX ONNX session 没有受支持的 CUDA/CPU provider：" + repr(session_providers))
    selected_provider = session_providers[0]
    if selected_provider not in ("CUDAExecutionProvider", "CPUExecutionProvider"):
        raise MdxProviderError("MDX ONNX session provider 顺序无效")
    if device == "cpu" and selected_provider != "CPUExecutionProvider":
        raise MdxProviderError("请求 CPU，但 ONNX session 实际优先使用 " + selected_provider)
    if device == "cuda" and selected_provider != "CUDAExecutionProvider":
        raise MdxProviderError("请求 CUDA，但 ONNX session 没有激活 CUDAExecutionProvider")
    inference_device = "cuda" if selected_provider == "CUDAExecutionProvider" else "cpu"
    if inference_device == "cuda" and not torch.cuda.is_available():
        raise MdxProviderError("ONNX session 选择了 CUDA，但 PyTorch CUDA 设备不可用")
    target_device = torch.device(inference_device)
    input_name, output_name = _validate_onnx_shape(session, metadata)
    stft = _MdxStft(torch, metadata, target_device)

    cuda_memory = None
    if inference_device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        memory_free_before, _memory_total = torch.cuda.mem_get_info()
        memory_allocated_before = torch.cuda.memory_allocated()

    mix = torch.from_numpy(np.ascontiguousarray(audio_44100.T, dtype=np.float32)).unsqueeze(0).to(target_device)
    n_fft = int(metadata["n_fft"])
    trim = n_fft // 2
    chunk_size = HOP_LENGTH * (int(metadata["dim_t"]) - 1)
    gen_size = chunk_size - 2 * trim
    if gen_size <= 0:
        raise MdxModelError("MDX 模型时间窗口不足以裁掉边缘")
    pad = gen_size + trim - (mix.shape[-1] % gen_size)
    mixture = torch.cat((
        torch.zeros((1, 2, trim), dtype=torch.float32, device=target_device),
        mix,
        torch.zeros((1, 2, pad), dtype=torch.float32, device=target_device),
    ), dim=-1)
    step = gen_size
    result = torch.zeros_like(mixture)
    divider = torch.zeros_like(mixture)
    starts = range(0, mixture.shape[-1], step)
    total_chunks = len(starts)
    for chunk_index, start in enumerate(starts):
        end = min(start + chunk_size, mixture.shape[-1])
        actual = end - start
        section = mixture[..., start:end]
        if actual < chunk_size:
            section = torch.nn.functional.pad(section, (0, chunk_size - actual))
        predicted = _run_model_chunk(session, input_name, output_name, stft, section, chunk_size)
        window = torch.from_numpy(np.hanning(actual).astype(np.float32)).to(target_device).reshape(1, 1, -1)
        result[..., start:end] += torch.from_numpy(predicted[..., :actual]).to(target_device) * window
        divider[..., start:end] += window
        if progress_callback is not None:
            progress_callback(chunk_index + 1, total_chunks)

    divisor = divider.clamp_min(torch.finfo(torch.float32).eps)
    separated = result / divisor
    separated = separated[..., trim:-trim][..., :mix.shape[-1]]
    primary = separated[0].T.detach().to("cpu").numpy().astype(np.float32, copy=False)
    primary = np.ascontiguousarray(primary * float(metadata["compensate"]))
    _require_finite_audio(primary, "MDX 输出")
    if primary.shape != audio_44100.shape:
        raise MdxModelError("MDX 输出长度与输入未对齐")
    if inference_device == "cuda":
        torch.cuda.synchronize()
        memory_free_after, _memory_total = torch.cuda.mem_get_info()
        cuda_memory = {
            "torch_allocated_before_bytes": int(memory_allocated_before),
            "torch_allocated_after_bytes": int(torch.cuda.memory_allocated()),
            "torch_peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
            "device_free_before_bytes": int(memory_free_before),
            "device_free_after_bytes": int(memory_free_after),
            "note": "device-wide free memory is sampled, not attributed exclusively to this worker",
        }
    if execution_callback is not None:
        execution_callback({
            "provider_preference": device,
            "selected_provider": selected_provider,
            "session_providers": session_providers,
            "inference_device": inference_device,
            "torch_device": inference_device,
            "provider_fallback_reason": provider_fallback_reason,
            "inference_completed": True,
            "cuda_memory_evidence": cuda_memory,
        })
    del session
    if inference_device == "cuda":
        torch.cuda.empty_cache()
    return primary


def dual_model_arrays(audio, model_outputs: Mapping[str, Any]):
    """Map two primary predictions to the four named outputs without reassigning roles."""
    import numpy as np

    _require_finite_audio(audio, "双模型源音频")
    required = ("kim_vocal", "insthq3_instrumental")
    for key in required:
        prediction = model_outputs.get(key)
        _require_finite_audio(prediction, "MDX " + key)
        if prediction.shape != audio.shape:
            raise MdxModelError("MDX " + key + " 与源音频长度或声道数不一致")
    outputs = {
        "kim_vocal": model_outputs["kim_vocal"],
        "kim_instrumental": np.ascontiguousarray(audio - model_outputs["kim_vocal"], dtype=np.float32),
        "insthq3_vocal": np.ascontiguousarray(audio - model_outputs["insthq3_instrumental"], dtype=np.float32),
        "insthq3_instrumental": model_outputs["insthq3_instrumental"],
    }
    for key, value in outputs.items():
        _require_finite_audio(value, "MDX " + key)
    return outputs
