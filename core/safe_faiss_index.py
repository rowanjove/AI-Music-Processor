"""Read bounded Flat/IVFFlat data and reconstruct trusted Faiss objects.

Never pass file bytes to faiss.read_index/deserialize_index. Preserve IVF
centroids, inverted-list order, IDs and nprobe rather than changing search to
Flat. Format reference: Faiss v1.7.3 impl/index_read.cpp and io_macros.h.
"""
import os
import struct

import numpy as np

ALGORITHM_VERSION = 'bounded-faiss-flat-ivfflat-v1'
MAX_BYTES = 512 * 1024 ** 2
MAX_LISTS = 8192
MAX_VECTORS = 500000


class IndexDataError(ValueError):
    pass


class _Reader:
    def __init__(self, handle, size):
        self.handle, self.size = handle, size
    def take(self, count):
        if type(count) is not int or count < 0 or count > MAX_BYTES or count > self.size - self.handle.tell():
            raise IndexDataError('Index data length is outside the bounded file')
        data = self.handle.read(count)
        if len(data) != count:
            raise IndexDataError('Truncated index data')
        return data
    def scalar(self, code):
        return struct.unpack('<' + code, self.take(struct.calcsize('<' + code)))[0]
    def vector(self, dtype, count=None, max_count=None):
        length = self.scalar('Q')
        if count is not None and length != count:
            raise IndexDataError('Index vector count disagrees with its header')
        if max_count is not None and length > max_count:
            raise IndexDataError('Index vector count exceeds its budget')
        dtype = np.dtype(dtype)
        return np.frombuffer(self.take(int(length) * dtype.itemsize), dtype=dtype)


def _header(reader):
    dimension, total = reader.scalar('i'), reader.scalar('q')
    reader.take(16)  # historical capacity fields do not control any allocation
    trained, metric = reader.scalar('B'), reader.scalar('i')
    if not 1 <= dimension <= 2048 or not 1 <= total <= MAX_VECTORS or total * dimension * 4 > MAX_BYTES or trained != 1 or metric != 1:
        raise IndexDataError('Only bounded trained L2 float indexes are supported')
    return dimension, total


def _finite_vectors(values):
    if not np.isfinite(values).all() or (values.size and float(np.max(np.abs(values))) > 1e6):
        raise IndexDataError('Index vectors contain nonfinite/unbounded values')


def _flat(reader, expected_dimension=None, expected_count=None):
    if reader.take(4) not in (b'IxF2', b'IxFl'):
        raise IndexDataError('IVF coarse quantizer must be plain L2 Flat data')
    dimension, total = _header(reader)
    if expected_dimension is not None and (dimension != expected_dimension or total != expected_count):
        raise IndexDataError('IVF quantizer dimensions/count disagree with its header')
    values = reader.vector('<f4', count=total * dimension).reshape(total, dimension)
    _finite_vectors(values)
    return dimension, values


def load_faiss_index(path, faiss_module=None):
    """Return a native index created only from already validated plain arrays."""
    with open(path, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if not 4 <= size <= MAX_BYTES:
            raise IndexDataError('Index file exceeds the bounded size policy')
        reader = _Reader(handle, size)
        kind = reader.take(4)
        if kind in (b'IxF2', b'IxFl'):
            handle.seek(0)
            dimension, vectors = _flat(reader)
            parsed = {'kind': 'flat', 'dimension': dimension, 'vectors': vectors}
        elif kind == b'IwFl':
            dimension, total = _header(reader)
            nlist, nprobe = reader.scalar('Q'), reader.scalar('Q')
            if not 1 <= nlist <= MAX_LISTS or not 1 <= nprobe <= nlist:
                raise IndexDataError('Invalid bounded IVF list/probe count')
            _, centroids = _flat(reader, dimension, nlist)
            direct_map = reader.scalar('B')
            # NoMap and Array are accepted; serialized native hash tables and
            # out-of-process/mmap inverted lists are never reconstructed.
            if direct_map not in (0, 1):
                raise IndexDataError('Unsupported IVF direct-map format')
            mapping = reader.vector('<i8', count=0 if direct_map == 0 else total)
            if reader.take(4) != b'ilar' or reader.scalar('Q') != nlist or reader.scalar('Q') != dimension * 4:
                raise IndexDataError('Unsupported/inconsistent IVF inverted-list encoding')
            encoding = reader.take(4)
            if encoding == b'full':
                sizes = reader.vector('<u8', count=nlist)
            elif encoding == b'sprs':
                pairs = reader.vector('<u8', max_count=nlist * 2)
                if len(pairs) % 2 or (len(pairs) and (pairs[::2].max() >= nlist or len(np.unique(pairs[::2])) != len(pairs)//2)):
                    raise IndexDataError('Invalid sparse IVF list sizes')
                sizes = np.zeros(nlist, dtype=np.uint64)
                sizes[pairs[::2].astype(np.intp)] = pairs[1::2]
            else:
                raise IndexDataError('Unsupported IVF list-size encoding')
            if sum(int(count) for count in sizes) != total:
                raise IndexDataError('IVF list sizes disagree with total vector count')
            if total * (dimension * 4 + 8) > size - handle.tell():
                raise IndexDataError('IVF vector bytes exceed the actual remaining file')
            seen = np.zeros(total, dtype=np.bool_)
            lists = []
            for list_id, raw_count in enumerate(sizes):
                count = int(raw_count)
                values = np.frombuffer(reader.take(count * dimension * 4), dtype='<f4').reshape(count, dimension)
                ids = np.frombuffer(reader.take(count * 8), dtype='<i8')
                _finite_vectors(values)
                if count and (ids.min() < 0 or ids.max() >= total or len(np.unique(ids)) != count or seen[ids].any()):
                    raise IndexDataError('IVF IDs must be unique, bounded and contiguous across lists')
                seen[ids] = True
                if direct_map == 1 and count:
                    locations = (int(list_id) << 32) + np.arange(count, dtype=np.int64)
                    if not np.array_equal(mapping[ids], locations):
                        raise IndexDataError('IVF direct map disagrees with actual list locations')
                lists.append((values, ids))
            if total and not seen.all():
                raise IndexDataError('IVF vector IDs do not cover their total count')
            parsed = {'kind': 'ivfflat', 'dimension': dimension, 'total': total,
                      'nlist': nlist, 'nprobe': nprobe, 'centroids': centroids,
                      'direct_map': direct_map, 'lists': lists}
        else:
            raise IndexDataError('Only plain FlatL2/IVFFlat indexes are supported; native fallback is forbidden')
        if handle.tell() != size:
            raise IndexDataError('Trailing data in index file')
    if faiss_module is None:
        import faiss as faiss_module
    quantizer = faiss_module.IndexFlatL2(parsed['dimension'])
    if parsed['kind'] == 'flat':
        quantizer.add(np.ascontiguousarray(parsed['vectors'], dtype=np.float32))
        return quantizer
    quantizer.add(np.ascontiguousarray(parsed['centroids'], dtype=np.float32))
    index = faiss_module.IndexIVFFlat(quantizer, parsed['dimension'], parsed['nlist'], faiss_module.METRIC_L2)
    index.is_trained = True
    index.nprobe = parsed['nprobe']
    for list_id, (values, ids) in enumerate(parsed['lists']):
        if len(ids):
            codes = np.ascontiguousarray(values, dtype=np.float32).view(np.uint8)
            identifiers = np.ascontiguousarray(ids, dtype=np.int64)
            index.invlists.add_entries(list_id, len(ids), faiss_module.swig_ptr(identifiers), faiss_module.swig_ptr(codes))
    index.ntotal = parsed['total']
    if parsed['direct_map'] == 1:
        index.make_direct_map()
    return index
