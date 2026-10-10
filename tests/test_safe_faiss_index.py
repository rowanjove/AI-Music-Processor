import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import faiss
import numpy as np
from core.safe_faiss_index import load_faiss_index, IndexDataError
from core.rvc_worker import safe_index_pipeline_class


class SafeIndexTests(unittest.TestCase):
    def test_worker_injection_is_local_and_preserves_selected_index(self):
        class Pipeline:
            def pipeline(self, path):
                return faiss.read_index(path)
        selected = object()
        adapted = safe_index_pipeline_class(Pipeline, selected, 'selected.index')()
        with mock.patch.object(faiss, 'read_index', side_effect=AssertionError('native reader forbidden')):
            self.assertIs(adapted.pipeline('selected.index'), selected)
            with mock.patch('core.safe_faiss_index.load_faiss_index', return_value=selected) as loader:
                # The injected function retains its validated loader; the
                # absent path must fail rather than invoke the native reader.
                with self.assertRaises(FileNotFoundError):
                    adapted.pipeline('missing.index')
                loader.assert_not_called()

    def test_flat_and_ivf_preserve_search_and_reconstruction_without_native_reader(self):
        faiss.omp_set_num_threads(1)
        generator = np.random.default_rng(42)
        vectors = generator.normal(size=(200, 8)).astype('float32')
        queries = generator.normal(size=(10, 8)).astype('float32')
        flat = faiss.IndexFlatL2(8)
        flat.add(vectors)
        ivf = faiss.IndexIVFFlat(faiss.IndexFlatL2(8), 8, 4)
        ivf.train(vectors)
        ivf.add(vectors)
        ivf.nprobe = 2
        with tempfile.TemporaryDirectory() as directory:
            for original in (flat, ivf):
                path = Path(directory)/'model.index'
                faiss.write_index(original, str(path))
                with mock.patch.object(faiss, 'read_index', side_effect=AssertionError('native read forbidden')):
                    rebuilt = load_faiss_index(path)
                before, after = original.search(queries, 5), rebuilt.search(queries, 5)
                np.testing.assert_array_equal(before[1], after[1])
                np.testing.assert_array_equal(before[0], after[0])
                np.testing.assert_array_equal(original.reconstruct_n(0, 200), rebuilt.reconstruct_n(0, 200))
                if isinstance(original, faiss.IndexIVFFlat):
                    self.assertEqual((rebuilt.nlist, rebuilt.nprobe), (4, 2))

    def test_invalid_lengths_nan_ids_and_unknown_formats_fail_before_native_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'model.index'
            flat = faiss.IndexFlatL2(2)
            flat.add(np.array([[1, 2], [3, 4]], dtype='float32'))
            faiss.write_index(flat, str(path))
            good = path.read_bytes()
            cases = [good[:20], b'unknown format', good + b'trailing',
                     good[:4] + struct.pack('<i', 2**30) + good[8:],
                     good[:37] + struct.pack('<Q', 2**60) + good[45:],
                     good[:-4] + struct.pack('<f', float('nan'))]
            for data in cases:
                path.write_bytes(data)
                with mock.patch.object(faiss, 'IndexFlatL2', side_effect=AssertionError('invalid input must not reach native')):
                    with self.assertRaises(IndexDataError):
                        load_faiss_index(path)
            ivf = faiss.IndexIVFFlat(faiss.IndexFlatL2(2), 2, 1)
            ivf.train(np.ones((40, 2), dtype='float32'))
            ivf.add(np.array([[1, 2], [3, 4]], dtype='float32'))
            faiss.write_index(ivf, str(path))
            corrupted = bytearray(path.read_bytes())
            # Last list has two int64 IDs; an out-of-range ID cannot reach
            # native reconstruct_n or inverted-list insertion.
            corrupted[-8:] = struct.pack('<q', 2**40)
            path.write_bytes(corrupted)
            with mock.patch.object(faiss, 'IndexFlatL2', side_effect=AssertionError('invalid IDs cannot reach native')):
                with self.assertRaises(IndexDataError):
                    load_faiss_index(path)


if __name__ == '__main__':
    unittest.main()
