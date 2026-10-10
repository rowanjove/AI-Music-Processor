"""Bounded RVC ZIP checkpoint reader without torch/pickle reconstruction hooks.

Unpickling only produces inert records and plain containers. Tensor storage is
checked against actual ZIP bytes before NumPy/Torch see it. Legacy tar,
arbitrary globals, BUILD and Torch-native storage rebuilding are rejected.
"""
import collections
import io
import math
import os
import sys
import pickle
import pickletools
import re
import zipfile

import numpy as np

MAX_BYTES = 2 * 1024 ** 3
MAX_PICKLE_BYTES = 4 * 1024 ** 2
MAX_RECORDS = 10000
MAX_OPS = 300000
MAX_ARCHITECTURE_BYTES = 512 * 1024 ** 2
StorageKind = collections.namedtuple('StorageKind', 'dtype')
StorageRecord = collections.namedtuple('StorageRecord', 'kind key count')
TensorRecord = collections.namedtuple('TensorRecord', 'storage offset shape stride')
_DTYPES = {'HalfStorage': '<f2', 'FloatStorage': '<f4', 'DoubleStorage': '<f8',
           'LongStorage': '<i8', 'IntStorage': '<i4', 'ShortStorage': '<i2',
           'CharStorage': 'i1', 'ByteStorage': 'u1', 'BoolStorage': '?'}
_OPS = {'PROTO', 'FRAME', 'STOP', 'MARK', 'POP', 'POP_MARK', 'DUP',
        'NONE', 'NEWTRUE', 'NEWFALSE', 'BININT', 'BININT1', 'BININT2',
        'LONG1', 'LONG4', 'BINFLOAT', 'BINUNICODE',
        'SHORT_BINUNICODE', 'BINUNICODE8', 'EMPTY_LIST', 'LIST', 'APPEND', 'APPENDS',
        'EMPTY_TUPLE', 'TUPLE', 'TUPLE1', 'TUPLE2', 'TUPLE3', 'EMPTY_DICT', 'DICT',
        'SETITEM', 'SETITEMS', 'BINPUT', 'LONG_BINPUT', 'BINGET',
        'LONG_BINGET', 'MEMOIZE', 'GLOBAL', 'STACK_GLOBAL', 'REDUCE', 'BINPERSID'}


class CheckpointError(ValueError):
    pass


def validate_rvc_inference_checkpoint(checkpoint, max_architecture_bytes=MAX_ARCHITECTURE_BYTES, complete=False):
    """Semantic/resource guard before any synthesizer architecture is allocated."""
    config, weights = checkpoint['config'], checkpoint['weight']
    inter, hidden, filters = config[2:5]
    initial, gin = config[13], config[16]
    feature = 768 if checkpoint.get('version', 'v1') == 'v2' else 256
    expected = {'enc_p.emb_phone.weight': (hidden, feature),
                'enc_p.proj.weight': (inter * 2, hidden, 1),
                'flow.flows.0.pre.weight': (hidden, inter // 2, 1),
                'dec.conv_pre.weight': (initial, inter, 7)}
    if inter % 2:
        raise CheckpointError('RVC coupling channels must be even')
    for name, shape in expected.items():
        tensor = weights.get(name)
        if tensor is None or tuple(tensor.shape) != shape:
            raise CheckpointError('Required RVC inference weight is absent/incompatible: ' + name)
    for layer in range(config[6]):
        for kind in ('q', 'k', 'v', 'o'):
            name = 'enc_p.encoder.attn_layers.%d.conv_%s.weight' % (layer, kind)
            if name not in weights or tuple(weights[name].shape) != (hidden, hidden, 1):
                raise CheckpointError('RVC attention weights disagree with configured layers')
    speaker_rows = weights['emb_g.weight'].shape[0]
    parameters = speaker_rows * gin + hidden * feature
    parameters += config[6] * (4 * hidden ** 2 + 2 * hidden * filters * config[7] + 8 * hidden)
    parameters += 4 * (inter * hidden * 2 + 3 * (12 * hidden ** 2 + 2 * hidden * gin))
    parameters += initial * inter * 7 + initial * gin
    channels = initial
    for kernel in config[14]:
        next_channels = channels // 2
        if next_channels < 1:
            raise CheckpointError('RVC upsampler channels become empty')
        parameters += channels * next_channels * kernel
        for residual_kernel, dilations in zip(config[10], config[11]):
            parameters += 2 * len(dilations) * next_channels ** 2 * residual_kernel
        channels = next_channels
    # Include weight-normalization buffers, source/noise layers and allocator
    # overhead conservatively; this is an estimate, not measured peak RAM.
    estimate = int(parameters * 4 * 2)
    actual_float_bytes = sum(tensor.numel() * 4 for tensor in weights.values())
    if estimate > max_architecture_bytes or actual_float_bytes > max_architecture_bytes:
        raise CheckpointError('RVC architecture exceeds the bounded inference parameter budget')
    result = {'architecture_estimate_bytes': estimate, 'weight_float32_bytes': actual_float_bytes,
              'parameter_budget_bytes': max_architecture_bytes, 'required_shapes_verified': True,
              'complete_weight_shapes_verified': False}
    if complete:
        # Torch 2.0's legacy weight_norm has no Meta implementation. A bounded
        # CPU-only constructor is used for import/inspection, never inference.
        # The worker itself checks strict=True on its one real constructor.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        vendor = os.path.join(root, 'tools', 'RVC', 'RVC-beta-v2-0618')
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        from infer_pack.models import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
                                       SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono)
        classes = {('v1', 1): SynthesizerTrnMs256NSFsid, ('v1', 0): SynthesizerTrnMs256NSFsid_nono,
                   ('v2', 1): SynthesizerTrnMs768NSFsid, ('v2', 0): SynthesizerTrnMs768NSFsid_nono}
        selected = classes[(checkpoint.get('version', 'v1'), checkpoint.get('f0', 1))]
        effective_config = list(config)
        effective_config[-3] = speaker_rows
        import torch
        with torch.device('cpu'):
            model = selected(*effective_config, **({'is_half': False} if checkpoint.get('f0', 1) else {}))
        try:
            del model.enc_q
            model.load_state_dict(weights, strict=True)
        except (RuntimeError, ValueError) as error:
            raise CheckpointError('RVC checkpoint does not contain complete compatible inference weights') from error
        finally:
            del model
        result['complete_weight_shapes_verified'] = True
    return result


def _integer(value, minimum=0, maximum=MAX_BYTES):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CheckpointError('Invalid bounded tensor integer')
    return value


def _tensor(storage, offset, shape, stride, requires_grad=False, backward_hooks=None, metadata=None):
    if requires_grad is not False or backward_hooks or metadata is not None:
        raise CheckpointError('Tensor hooks/metadata are not permitted in voice checkpoints')
    if type(storage) is not StorageRecord or type(shape) is not tuple or type(stride) is not tuple:
        raise CheckpointError('Invalid inert tensor record')
    return TensorRecord(storage, offset, shape, stride)


class _Records(pickle.Unpickler):
    def find_class(self, module, name):
        if module == 'collections' and name == 'OrderedDict':
            return collections.OrderedDict
        if module == 'torch' and name in _DTYPES:
            return StorageKind(_DTYPES[name])
        if module == 'torch._utils' and name in ('_rebuild_tensor', '_rebuild_tensor_v2'):
            return _tensor
        raise CheckpointError('Checkpoint global is not a permitted data record')

    def persistent_load(self, record):
        if (type(record) is not tuple or len(record) != 5 or record[0] != 'storage'
                or type(record[1]) is not StorageKind or type(record[2]) is not str
                or not re.fullmatch(r'[0-9]{1,12}', record[2]) or type(record[3]) is not str
                or not re.fullmatch(r'cpu|cuda:[0-9]{1,3}', record[3])):
            raise CheckpointError('Invalid storage reference')
        return StorageRecord(record[1], record[2], _integer(record[4]))


def _read_member(archive, name, budget):
    info = archive.getinfo(name)
    if info.file_size > budget:
        raise CheckpointError('Checkpoint record exceeds byte budget')
    with archive.open(info) as handle:
        data = handle.read(budget + 1)
    if len(data) != info.file_size or len(data) > budget:
        raise CheckpointError('Checkpoint ZIP storage length mismatch')
    return data


def _validate_config(checkpoint):
    config = checkpoint['config']
    if len(config) != 18:
        raise CheckpointError('RVC configuration must contain exactly 18 architecture fields')
    limits = {0: (1, 4097), 1: (1, 2048), 2: (1, 512), 3: (1, 512),
              4: (1, 2048), 5: (1, 16), 6: (1, 24), 7: (1, 31),
              13: (1, 1024), 15: (1, 4096), 16: (1, 1024)}
    for index, (minimum, maximum) in limits.items():
        _integer(config[index], minimum, maximum)
    if config[3] % config[5] or type(config[8]) not in (int, float) or not 0 <= config[8] < 1:
        raise CheckpointError('Invalid attention/dropout configuration')
    if config[9] not in ('1', '2'):
        raise CheckpointError('Unsupported RVC residual block')
    for index, maximum in ((10, 63), (12, 32), (14, 64)):
        if type(config[index]) not in (list, tuple) or not 1 <= len(config[index]) <= 8:
            raise CheckpointError('Invalid bounded RVC convolution list')
        for value in config[index]:
            _integer(value, 1, maximum)
    if len(config[12]) != len(config[14]) or math.prod(config[12]) > 2048:
        raise CheckpointError('Invalid bounded RVC upsampling configuration')
    if any(kernel < rate for kernel, rate in zip(config[14], config[12])) or any(kernel % 2 == 0 for kernel in config[10]):
        raise CheckpointError('Invalid RVC convolution kernel geometry')
    if type(config[11]) not in (list, tuple) or len(config[11]) != len(config[10]):
        raise CheckpointError('Invalid RVC dilation configuration')
    for dilations in config[11]:
        if type(dilations) not in (list, tuple) or not 1 <= len(dilations) <= 8:
            raise CheckpointError('Invalid bounded RVC dilations')
        for value in dilations:
            _integer(value, 1, 64)
    rates = {'32k': 32000, '40k': 40000, '48k': 48000}
    sample_rate = rates.get(config[17]) if type(config[17]) is str else config[17]
    if type(sample_rate) is not int or sample_rate not in (32000, 40000, 48000):
        raise CheckpointError('Unsupported RVC sample rate')
    if checkpoint.get('version', 'v1') not in ('v1', 'v2') or type(checkpoint.get('f0', 1)) not in (int, bool) or checkpoint.get('f0', 1) not in (0, 1):
        raise CheckpointError('Invalid RVC version/F0 metadata')
    if checkpoint.get('info') is not None and type(checkpoint['info']) is not str:
        raise CheckpointError('RVC training metadata must be plain text')
    if 'sr' in checkpoint and type(checkpoint['sr']) not in (str, int):
        raise CheckpointError('RVC sample-rate metadata must be plain data')
    embedding = checkpoint['weight'].get('emb_g.weight')
    if (type(embedding) is not TensorRecord or len(embedding.shape) != 2
            or not 1 <= embedding.shape[0] <= 4096 or embedding.shape[1] != config[16]):
        raise CheckpointError('RVC speaker embedding disagrees with bounded configuration')


def load_rvc_checkpoint(path, torch_module=None):
    """Read plain RVC config plus dense tensor weights; never call torch.load."""
    if os.path.getsize(path) > MAX_BYTES:
        raise CheckpointError('Checkpoint file exceeds byte budget')
    if not zipfile.is_zipfile(path):
        raise CheckpointError('Only data-only ZIP RVC checkpoints are supported; legacy tar is refused')
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_RECORDS:
                raise CheckpointError('Checkpoint archive record limit exceeded')
            names = [entry.filename for entry in members]
            if len(names) != len(set(names)):
                raise CheckpointError('Duplicate checkpoint archive records')
            total = 0
            for entry in members:
                parts = entry.filename.split('/')
                if (any(part in ('', '.', '..') for part in parts) or ':' in entry.filename
                        or '\\' in entry.filename or entry.flag_bits & 1
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise CheckpointError('Invalid checkpoint archive path/encoding')
                total += entry.file_size
                if total > MAX_BYTES:
                    raise CheckpointError('Checkpoint expanded size limit exceeded')
            pickles = [name for name in names if name.endswith('/data.pkl')]
            if len(pickles) != 1:
                raise CheckpointError('Checkpoint must have exactly one data.pkl')
            prefix = pickles[0][:-len('data.pkl')]
            if any(not name.startswith(prefix) for name in names):
                raise CheckpointError('Checkpoint records use inconsistent archive roots')
            if prefix + 'byteorder' in names and _read_member(archive, prefix + 'byteorder', 16) != b'little':
                raise CheckpointError('Only little-endian checkpoint storages are supported')
            data = _read_member(archive, pickles[0], MAX_PICKLE_BYTES)
            operations = 0
            stopped_at = None
            for opcode, argument, position in pickletools.genops(data):
                operations += 1
                if opcode.name not in _OPS or operations > MAX_OPS:
                    raise CheckpointError('Checkpoint uses an unsupported/oversized pickle program')
                if opcode.name == 'PROTO' and argument > 4:
                    raise CheckpointError('Unsupported checkpoint pickle protocol')
                if opcode.name in ('PUT', 'BINPUT', 'LONG_BINPUT') and int(argument) > MAX_OPS:
                    raise CheckpointError('Checkpoint memo limit exceeded')
                if opcode.name == 'STOP':
                    stopped_at = position + 1
            if stopped_at != len(data):
                raise CheckpointError('Trailing checkpoint pickle data')
            parsed = _Records(io.BytesIO(data)).load()
            if type(parsed) not in (dict, collections.OrderedDict):
                raise CheckpointError('RVC checkpoint root must be a dictionary')
            allowed = {'weight', 'config', 'f0', 'version', 'sr', 'info'}
            if set(parsed) - allowed or not {'weight', 'config'}.issubset(parsed):
                raise CheckpointError('Unsupported RVC checkpoint fields')
            if type(parsed['config']) not in (list, tuple) or not 1 <= len(parsed['config']) <= 64:
                raise CheckpointError('Invalid RVC model configuration')
            if any(type(value) not in (int, float, bool, str, list, tuple) for value in parsed['config']):
                raise CheckpointError('Model configuration is not plain data')
            weights = parsed['weight']
            if type(weights) not in (dict, collections.OrderedDict) or not 1 <= len(weights) <= MAX_RECORDS:
                raise CheckpointError('Invalid RVC weight mapping')
            if any(type(name) is not str or not name or len(name) > 512 or type(value) is not TensorRecord
                   for name, value in weights.items()):
                raise CheckpointError('RVC weights must be named dense tensor records')
            _validate_config(parsed)
            buffers, tensors, active, plain_cache = {}, {}, set(), {}
            logical_bytes = 0
            # Validation produces only NumPy data. Torch is imported/called
            # after the entire object graph and every referenced storage pass.
            def convert(value, depth=0):
                nonlocal logical_bytes
                if depth > 64:
                    raise CheckpointError('Checkpoint object nesting limit exceeded')
                if type(value) in (str, int, float, bool, type(None)):
                    if type(value) is float and not math.isfinite(value):
                        raise CheckpointError('Nonfinite checkpoint metadata')
                    if type(value) is str and len(value) > 65536:
                        raise CheckpointError('Checkpoint metadata string too long')
                    return value
                identity = id(value)
                if identity in active:
                    raise CheckpointError('Cyclic checkpoint object graph')
                if identity in plain_cache:
                    return plain_cache[identity]
                active.add(identity)
                try:
                    if type(value) is TensorRecord:
                        record = value.storage
                        dtype = np.dtype(record.kind.dtype)
                        count = _integer(record.count)
                        expected = count * dtype.itemsize
                        if expected > MAX_BYTES:
                            raise CheckpointError('Tensor storage exceeds byte budget')
                        storage_name = prefix + 'data/' + record.key
                        if storage_name not in names or archive.getinfo(storage_name).file_size != expected:
                            raise CheckpointError('Declared tensor storage disagrees with actual ZIP bytes')
                        definition = (record.kind.dtype, count)
                        if record.key in buffers and buffers[record.key][0] != definition:
                            raise CheckpointError('Conflicting storage definitions')
                        shape, stride = value.shape, value.stride
                        if len(shape) != len(stride) or len(shape) > 8:
                            raise CheckpointError('Invalid tensor dimensions')
                        shape = tuple(_integer(size, 1) for size in shape)
                        stride = tuple(_integer(step, 1) for step in stride)
                        offset = _integer(value.offset)
                        elements = math.prod(shape)
                        logical_bytes += elements * dtype.itemsize
                        if logical_bytes > MAX_BYTES:
                            raise CheckpointError('Tensor logical size budget exceeded')
                        last = offset + sum((size - 1) * step for size, step in zip(shape, stride)) if elements else offset
                        if offset > count or (elements and last >= count):
                            raise CheckpointError('Tensor shape/stride/offset escapes its storage')
                        if record.key not in buffers:
                            buffers[record.key] = (definition, bytearray(_read_member(archive, storage_name, expected)))
                        array = np.ndarray(shape, dtype=dtype, buffer=buffers[record.key][1],
                                           offset=offset * dtype.itemsize,
                                           strides=tuple(step * dtype.itemsize for step in stride))
                        if dtype.kind == 'f' and not np.isfinite(array).all():
                            raise CheckpointError('Nonfinite RVC weight tensor')
                        tensors[identity] = array
                        result = value
                    elif type(value) in (dict, collections.OrderedDict):
                        if getattr(value, '__dict__', {}):
                            raise CheckpointError('Checkpoint container has unexpected state')
                        if any(type(key) is not str for key in value):
                            raise CheckpointError('Checkpoint dictionary keys must be strings')
                        result = {key: convert(item, depth + 1) for key, item in value.items()}
                    elif type(value) in (list, tuple):
                        result = [convert(item, depth + 1) for item in value]
                    else:
                        raise CheckpointError('Checkpoint contains a non-data object')
                    plain_cache[identity] = result
                    return result
                finally:
                    active.remove(identity)
            result = convert(parsed)
            if torch_module is None:
                import torch as torch_module
            result['weight'] = {name: torch_module.from_numpy(tensors[id(record)]) for name, record in weights.items()}
            return result
    except CheckpointError:
        raise
    except (pickle.UnpicklingError, ValueError, TypeError, AttributeError, EOFError, KeyError,
            zipfile.BadZipFile, RuntimeError, RecursionError, OverflowError) as error:
        raise CheckpointError('Invalid data-only RVC checkpoint (%s)' % type(error).__name__) from None
