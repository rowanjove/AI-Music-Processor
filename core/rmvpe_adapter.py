"""RMVPE pitch extraction injected into the installed 0618 VC pipeline.

Inference consumes 16 kHz mono audio at a 160-sample hop. Weight errors are
fatal; this adapter never substitutes another pitch algorithm.
"""
import os
import io

import numpy as np


def load_rmvpe(weight, device="cpu"):
    if not os.path.isfile(weight) or os.path.getsize(weight) == 0:
        raise FileNotFoundError("RMVPE requires a nonempty rmvpe.pt: " + os.fspath(weight))
    from core.f0_method import verify_rmvpe_weight
    if str(device) != 'cpu' and not str(device).startswith('cuda'):
        raise ValueError('The managed RMVPE adapter supports CPU/CUDA only')
    data = verify_rmvpe_weight(weight, return_bytes=True)
    from core.vendor.rmvpe import RMVPE
    # Use authenticated immutable bytes so replacing/modifying the path after
    # verification cannot feed a different pickle to the bundled old Torch.
    with io.BytesIO(data) as stream:
        return RMVPE(stream, is_half=False, device=device)


def pitch_frames(extractor, audio, frame_count, semitones, x_pad=1, inp_f0=None):
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
        raise ValueError("RMVPE input must be finite 16 kHz mono audio")
    if not isinstance(frame_count, int) or frame_count < 1:
        raise ValueError("RMVPE frame_count must be positive")
    f0 = np.asarray(extractor.infer_from_audio(audio, thred=0.03), dtype=np.float64)
    if f0.ndim != 1 or not f0.size or not np.isfinite(f0).all() or np.any(f0 < 0):
        raise ValueError("RMVPE returned invalid pitch frames")
    # Centered STFT may return one extra frame. Preserve alignment and zeros,
    # without stretching voiced frames across the unvoiced regions.
    if abs(len(f0) - frame_count) > 2:
        raise ValueError("RMVPE pitch/audio frame counts disagree")
    f0 = np.pad(f0[:frame_count], (0, max(0, frame_count - len(f0))))
    if not np.isfinite(semitones) or not -24 <= semitones <= 24:
        raise ValueError("RMVPE pitch shift is outside [-24, 24]")
    f0 *= 2 ** (semitones / 12)
    if inp_f0 is not None:
        points = np.asarray(inp_f0, dtype=np.float64)
        if (points.ndim != 2 or points.shape[1] != 2 or not len(points)
                or not np.isfinite(points).all() or np.any(points[:, 1] < 0)
                or np.any(np.diff(points[:, 0]) <= 0)):
            raise ValueError("Invalid external pitch override")
        count = int(round((points[:, 0].max() - points[:, 0].min()) * 100 + 1))
        start = int(x_pad * 100)
        count = min(count, max(0, len(f0) - start))
        if count:
            f0[start:start + count] = np.interp(np.arange(count), points[:, 0] * 100, points[:, 1])
    mel = 1127 * np.log1p(f0 / 700)
    lower, upper = 1127 * np.log1p(50 / 700), 1127 * np.log1p(1100 / 700)
    voiced = mel > 0
    mel[voiced] = (mel[voiced] - lower) * 254 / (upper - lower) + 1
    coarse = np.rint(np.clip(mel, 1, 255)).astype(np.int64)
    return coarse, f0


def rmvpe_vc_class(base_class, weight, device):
    """Keep every legacy conversion step except the selected F0 method."""
    class RMVPEVC(base_class):
        _rmvpe = None

        def get_f0(self, input_audio_path, x, p_len, f0_up_key, f0_method,
                   filter_radius, inp_f0=None):
            if f0_method != "rmvpe":
                return super().get_f0(input_audio_path, x, p_len, f0_up_key,
                                      f0_method, filter_radius, inp_f0)
            if self._rmvpe is None:
                self._rmvpe = load_rmvpe(weight, device)
            return pitch_frames(self._rmvpe, x, p_len, f0_up_key, self.x_pad, inp_f0)

    return RMVPEVC
