import collections
import io
import pickle
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
from core.safe_checkpoint import load_rvc_checkpoint, validate_rvc_inference_checkpoint, CheckpointError

CONFIG = [1025, 32, 192, 192, 768, 2, 6, 3, 0, '1', [3, 7, 11],
          [[1, 3, 5]] * 3, [12, 10, 2, 2], 512, [24, 20, 4, 4], 1, 256, 48000]


class _Storage:
    def __init__(self, count):
        self.count = count


class _Tensor:
    def __init__(self, count=256, shape=(1, 256), stride=(256, 1), offset=0):
        self.storage, self.shape, self.stride, self.offset = _Storage(count), shape, stride, offset
    def __reduce__(self):
        return torch._utils._rebuild_tensor_v2, (self.storage, self.offset, self.shape, self.stride, False, collections.OrderedDict())


def crafted_checkpoint(path, tensor=None, config=None, payload=None, storage=None):
    stream = io.BytesIO()
    class Writer(pickle.Pickler):
        def persistent_id(self, value):
            if isinstance(value, _Storage):
                return ('storage', torch.FloatStorage, '0', 'cpu', value.count)
    Writer(stream, protocol=2).dump(payload if payload is not None else {
        'config': config or CONFIG, 'weight': {'emb_g.weight': tensor or _Tensor()}, 'version': 'v1', 'f0': 1})
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('voice/data.pkl', stream.getvalue())
        archive.writestr('voice/data/0', storage if storage is not None else np.zeros(256, dtype='<f4').tobytes())
        archive.writestr('voice/version', '3\n')


class SafeCheckpointTests(unittest.TestCase):
    def test_inference_semantics_and_architecture_budget_precede_model_allocation(self):
        checkpoint = {'config': CONFIG, 'weight': {'emb_g.weight': torch.zeros(1, 256)}}
        with self.assertRaisesRegex(CheckpointError, 'Required RVC inference weight'):
            validate_rvc_inference_checkpoint(checkpoint)
        weights = checkpoint['weight']
        weights.update({'enc_p.emb_phone.weight': torch.zeros(192, 256),
                        'enc_p.proj.weight': torch.zeros(384, 192, 1),
                        'flow.flows.0.pre.weight': torch.zeros(192, 96, 1),
                        'dec.conv_pre.weight': torch.zeros(512, 192, 7)})
        for layer in range(6):
            for kind in ('q', 'k', 'v', 'o'):
                weights['enc_p.encoder.attn_layers.%d.conv_%s.weight' % (layer, kind)] = torch.zeros(192, 192, 1)
        with self.assertRaisesRegex(CheckpointError, 'parameter budget'):
            validate_rvc_inference_checkpoint(checkpoint, max_architecture_bytes=1024)
        self.assertTrue(validate_rvc_inference_checkpoint(checkpoint)['required_shapes_verified'])
        with self.assertRaisesRegex(CheckpointError, 'complete compatible inference weights'):
            validate_rvc_inference_checkpoint(checkpoint, complete=True)

    def test_real_torch_zip_roundtrip_without_torch_load(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)/'model.pth'
            expected = torch.arange(256, dtype=torch.float16).reshape(1, 256)
            torch.save({'config': CONFIG, 'weight': collections.OrderedDict({'emb_g.weight': expected}),
                        'version': 'v1', 'f0': 1}, model)
            with mock.patch.object(torch, 'load', side_effect=AssertionError('native deserializer forbidden')):
                actual = load_rvc_checkpoint(model)
            self.assertEqual(actual['config'], CONFIG)
            self.assertTrue(torch.equal(actual['weight']['emb_g.weight'], expected))

    def test_storage_shape_stride_offset_and_config_rejected_before_tensor_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)/'model.pth'
            cases = [dict(tensor=_Tensor(count=257)), dict(tensor=_Tensor(shape=(2, 256))),
                     dict(tensor=_Tensor(stride=(256, -1))), dict(tensor=_Tensor(offset=1)),
                     dict(tensor=_Tensor(shape=(1, 2**40))), dict(storage=b'too short'),
                     dict(tensor=_Tensor(stride=(256, 0)))]
            oversized = list(CONFIG); oversized[3] = 2**40
            cases.append(dict(config=oversized))
            for case in cases:
                with self.subTest(case=case):
                    crafted_checkpoint(model, **case)
                    with mock.patch.object(torch, 'from_numpy', side_effect=AssertionError('cannot construct invalid tensor')):
                        with self.assertRaises(CheckpointError):
                            load_rvc_checkpoint(model)

    def test_arbitrary_global_and_legacy_pickle_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, model = root/'executed', root/'model.pth'
            class Payload:
                def __reduce__(self):
                    return eval, ("__import__('pathlib').Path(%r).write_text('executed')" % str(marker),)
            crafted_checkpoint(model, payload={'config': CONFIG, 'weight': {'emb_g.weight': Payload()}})
            with self.assertRaises(CheckpointError):
                load_rvc_checkpoint(model)
            self.assertFalse(marker.exists())
            model.write_bytes(pickle.dumps(Payload()))
            with self.assertRaisesRegex(CheckpointError, 'legacy tar'):
                load_rvc_checkpoint(model)
            self.assertFalse(marker.exists())

    def test_build_cycles_and_duplicate_records_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory)/'model.pth'
            weights = collections.OrderedDict({'emb_g.weight': _Tensor()})
            weights.extra = 'unexpected container state'
            crafted_checkpoint(model, payload={'config': CONFIG, 'weight': weights})
            with self.assertRaises(CheckpointError):
                load_rvc_checkpoint(model)
            cyclic = list(CONFIG); cyclic[11] = cyclic
            crafted_checkpoint(model, config=cyclic)
            with self.assertRaises(CheckpointError):
                load_rvc_checkpoint(model)
            crafted_checkpoint(model)
            with zipfile.ZipFile(model, 'a') as archive:
                with self.assertWarns(UserWarning):
                    archive.writestr('voice/data/0', b'duplicate')
            with self.assertRaisesRegex(CheckpointError, 'Duplicate'):
                load_rvc_checkpoint(model)


if __name__ == '__main__':
    unittest.main()
