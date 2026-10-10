import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mdx_separator import (
    MDX_METADATA_TAIL_BYTES,
    MdxModelError,
    MdxProviderError,
    _provider_plan,
    dual_model_arrays,
    load_model_metadata,
    model_identity,
    separate_model_array,
    verify_model_identity,
)


class _EchoSession:
    def __init__(self, shape, providers=("CPUExecutionProvider",)):
        self._shape = shape
        self._providers = list(providers)

    def get_inputs(self):
        return [type("Node", (), {"name": "input", "shape": self._shape})()]

    def get_outputs(self):
        return [type("Node", (), {"name": "output", "shape": self._shape})()]

    def get_providers(self):
        return self._providers

    def run(self, names, feeds):
        return [feeds["input"]]


class MdxSeparatorTests(unittest.TestCase):
    def test_provider_plan_uses_real_ort_and_torch_capabilities(self):
        from types import SimpleNamespace

        cpu_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        cuda_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.assertEqual(_provider_plan("cpu", available, cuda_torch),
                         (["CPUExecutionProvider"], "CPUExecutionProvider", None))
        self.assertEqual(_provider_plan("auto", available, cpu_torch),
                         (["CPUExecutionProvider"], "CPUExecutionProvider", "PyTorch CUDA 不可用"))
        self.assertEqual(_provider_plan("auto", available, cuda_torch),
                         (["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDAExecutionProvider", None))
        with self.assertRaisesRegex(MdxProviderError, "没有 CUDAExecutionProvider"):
            _provider_plan("cuda", ["CPUExecutionProvider"], cuda_torch)

    def test_uvr_hash_uses_last_10000_kib_and_freezes_catalog(self):
        self.assertEqual(MDX_METADATA_TAIL_BYTES, 10_240_000)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "Kim_Vocal_2.onnx"
            tail = b"v" * MDX_METADATA_TAIL_BYTES
            model.write_bytes(b"prefix-that-is-outside-the-UVR-hash" + tail)
            tail_md5 = hashlib.md5(tail).hexdigest()
            raw = {
                "mdx_dim_f_set": 9,
                "mdx_dim_t_set": 2,
                "mdx_n_fft_scale_set": 16,
                "primary_stem": "Vocals",
                "compensate": 1.009,
            }
            catalog = root / "model_data.json"
            catalog.write_text(json.dumps({tail_md5: raw}), encoding="utf-8")

            metadata = load_model_metadata(model, catalog)
            identity = model_identity(model, catalog)
            self.assertEqual(metadata["model_hash_tail_md5"], tail_md5)
            self.assertEqual(metadata["metadata_sha256"], hashlib.sha256(catalog.read_bytes()).hexdigest())
            self.assertEqual(metadata["dim_t"], 4)
            self.assertEqual(identity["metadata_path"], str(catalog.resolve()))
            self.assertEqual(identity["metadata"]["primary_stem"], "Vocals")
            self.assertEqual(verify_model_identity(model, identity, catalog), identity)

            raw["compensate"] = 1.01
            catalog.write_text(json.dumps({tail_md5: raw}), encoding="utf-8")
            with self.assertRaisesRegex(MdxModelError, "metadata_sha256"):
                verify_model_identity(model, identity, catalog)

    def test_cpu_onnx_contract_checks_shape_and_returns_finite_aligned_audio(self):
        import numpy as np

        metadata = {
            "dim_f": 1025,
            "dim_t": 4,
            "n_fft": 2048,
            "hop_length": 1024,
            "sample_rate": 44100,
            "primary_stem": "Vocals",
            "compensate": 1.0,
        }
        audio = np.random.default_rng(7).normal(0, 0.02, size=(4097, 2)).astype(np.float32)
        session = _EchoSession([1, 4, 1025, 4])
        evidence = {}
        result = separate_model_array(audio, "unused.onnx", metadata, device="cpu", session=session,
                                      execution_callback=evidence.update)
        self.assertEqual(result.shape, audio.shape)
        self.assertTrue(np.isfinite(result).all())
        self.assertEqual(evidence["selected_provider"], "CPUExecutionProvider")
        self.assertEqual(evidence["session_providers"], ["CPUExecutionProvider"])
        self.assertEqual(evidence["inference_device"], "cpu")
        self.assertTrue(evidence["inference_completed"])

        with self.assertRaisesRegex(MdxProviderError, "没有激活 CUDAExecutionProvider"):
            separate_model_array(audio, "unused.onnx", metadata, device="cuda",
                                 session=_EchoSession([1, 4, 1025, 4]))

        with self.assertRaisesRegex(MdxModelError, "shape"):
            separate_model_array(audio, "unused.onnx", metadata, device="cpu", session=_EchoSession([1, 4, 1024, 4]))

    def test_dual_stems_keep_model_roles_and_finite_subtractions(self):
        import numpy as np

        source = np.ones((12, 2), dtype=np.float32)
        kim = np.full_like(source, 0.25)
        inst = np.full_like(source, 0.6)
        outputs = dual_model_arrays(source, {"kim_vocal": kim, "insthq3_instrumental": inst})
        self.assertIs(outputs["kim_vocal"], kim)
        self.assertIs(outputs["insthq3_instrumental"], inst)
        np.testing.assert_allclose(outputs["kim_instrumental"], 0.75)
        np.testing.assert_allclose(outputs["insthq3_vocal"], 0.4)
        for audio in outputs.values():
            self.assertTrue(np.isfinite(audio).all())


if __name__ == "__main__":
    unittest.main()
