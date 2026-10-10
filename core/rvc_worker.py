"""Inference adapter for the installed 0618 RVC; never edits vendor configs."""
import argparse
import ast
import inspect
import json
import os
import sys
import textwrap
from types import SimpleNamespace, FunctionType
import numpy as np
import soundfile as sf

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from core.worker_runtime import clear_cuda, emit_worker_error, is_cuda_oom, resolve_device


def load_voice_checkpoint(path, torch_module):
    """User-importable voice checkpoints may contain tensors and plain data only."""
    from core.safe_checkpoint import load_rvc_checkpoint
    return load_rvc_checkpoint(path, torch_module=torch_module)


def safe_index_pipeline_class(base_class, prefetched_index=None, index_path=None):
    """Inject data-only index loading into this worker's trusted pipeline only."""
    original = base_class.pipeline
    native_faiss = original.__globals__.get('faiss')
    if native_faiss is None:
        return base_class  # dependency-free worker fixtures have no index IO
    from core.safe_faiss_index import load_faiss_index
    class IndexAPI:
        @staticmethod
        def read_index(path):
            if prefetched_index is not None and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(index_path)):
                return prefetched_index
            return load_faiss_index(path, faiss_module=native_faiss)
        def __getattr__(self, name):
            return getattr(native_faiss, name)
    namespace = dict(original.__globals__, faiss=IndexAPI())
    replacement = FunctionType(original.__code__, namespace, original.__name__,
                               original.__defaults__, original.__closure__)
    replacement.__kwdefaults__ = original.__kwdefaults__
    class SafeIndexVC(base_class):
        pipeline = replacement
    return SafeIndexVC


def floating_pipeline_class(base_class):
    """Adapt the trusted legacy source without integer-quantizing synth audio.

All segmentation, retrieval and RMS processing stay in the installed function.
Only its single terminal int16 conversion is replaced by equivalent floating
gain normalization. An unfamiliar vendor
shape fails closed instead of silently claiming a floating inference path.
"""
    if base_class.__module__ != 'vc_infer_pipeline':
        return base_class
    tree = ast.parse(textwrap.dedent(inspect.getsource(base_class.pipeline)))
    removed = []
    class FloatOutput(ast.NodeTransformer):
        def visit_Assign(self, node):
            value = node.value
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == 'audio_opt' and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute) and value.func.attr == 'astype'
                    and len(value.args) == 1 and isinstance(value.args[0], ast.Attribute)
                    and isinstance(value.args[0].value, ast.Name) and value.args[0].value.id == 'np'
                    and value.args[0].attr == 'int16' and isinstance(value.func.value, ast.BinOp)
                    and isinstance(value.func.value.op, ast.Mult)
                    and isinstance(value.func.value.left, ast.Name) and value.func.value.left.id == 'audio_opt'
                    and isinstance(value.func.value.right, ast.Name) and value.func.value.right.id == 'max_int16'):
                removed.append(node)
                replacement = ast.parse('audio_opt = (audio_opt * (max_int16 / 32768.0)).astype(np.float32)').body[0]
                return ast.copy_location(replacement, node)
            return self.generic_visit(node)
    tree = FloatOutput().visit(tree)
    if len(removed) != 1:
        raise RuntimeError('Legacy RVC float adapter cannot verify the terminal quantizer')
    namespace = {}
    exec(compile(ast.fix_missing_locations(tree), '<verified-rvc-float-adapter>', 'exec'),
         base_class.pipeline.__globals__, namespace)
    class FloatVC(base_class):
        pipeline = namespace['pipeline']
    return FloatVC


def capture_f0_class(base_class):
    """Observe the exact contour used by inference, never re-estimate output."""
    class CapturedVC(base_class):
        f0_track = None

        def pipeline(self, *args, **kwargs):
            self._processed_samples = 0
            self._completed_segments = 0
            self._total_samples = len(args[3]) if len(args) > 3 else 0
            self._emit_progress(0, 'inference')
            result = super().pipeline(*args, **kwargs)
            self._emit_progress(1, 'inference_complete')
            return result

        def _emit_progress(self, fraction, phase):
            print(json.dumps({'worker_progress': {'worker': 'rvc', 'phase': phase,
                  'fraction': fraction, 'completed_segments': getattr(self, '_completed_segments', 0),
                  'processed_samples': getattr(self, '_processed_samples', 0),
                  'total_samples': getattr(self, '_total_samples', 0)}}, ensure_ascii=False), flush=True)

        def vc(self, *args, **kwargs):
            result = super().vc(*args, **kwargs)
            self._completed_segments = getattr(self, '_completed_segments', 0) + 1
            # The legacy pipeline crops context on both sides of every result.
            # Count the actual synthesized retained samples, in source units.
            retained = max(0, len(result) - 2 * self.t_pad_tgt)
            source_samples = retained * 16000 / self._output_rate
            self._processed_samples = min(self._total_samples, self._processed_samples + source_samples)
            fraction = self._processed_samples / self._total_samples if self._total_samples else 0
            self._emit_progress(fraction, 'segment_complete')
            return result

        def get_f0(self, *args, **kwargs):
            coarse, hz = super().get_f0(*args, **kwargs)
            self.f0_track = np.asarray(hz).copy()
            return coarse, hz

    return CapturedVC


def write_f0_track(path, vc, method):
    track = getattr(vc, 'f0_track', None)
    if track is None:
        record = {'schema_version': 1, 'status': 'UNOBSERVABLE',
                  'actual_f0': None, 'frames_hz': None}
    else:
        track = np.asarray(track)
        if track.ndim != 1 or not np.isfinite(track).all():
            raise ValueError('RVC F0 contour is invalid')
        record = {'schema_version': 1, 'status': 'CAPTURED', 'actual_f0': method,
                  'sample_rate': 16000, 'hop_length': 160,
                  'x_pad': getattr(vc, 'x_pad', None), 'frames_hz': track.tolist()}
    temporary = path + '.part.' + str(os.getpid())
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(record, handle, ensure_ascii=False, allow_nan=False)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main():
    parser = argparse.ArgumentParser()
    for arg in ('input', 'output', 'model'):
        parser.add_argument('--'+arg, required=True)
    parser.add_argument('--index', default='')
    parser.add_argument('--pitch', type=int, default=0)
    parser.add_argument('--f0', choices=['harvest', 'pm', 'crepe', 'rmvpe'], default='harvest')
    parser.add_argument('--output-precision', choices=['float32', 'legacy_int16'], default='float32')
    parser.add_argument('--seed', type=int, default=None, help='Optional reproducible validation seed')
    parser.add_argument('--index-rate', type=float, default=.65)
    parser.add_argument('--protect', type=float, default=.33)
    parser.add_argument('--rms-mix', type=float, default=.25)
    parser.add_argument('--filter-radius', type=int, default=3)
    parser.add_argument('--cpu-threads', type=int, choices=range(1, 5), default=4)
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    parser.add_argument('--x-pad', type=int, default=1)
    parser.add_argument('--x-query', type=int, default=5)
    parser.add_argument('--x-center', type=int, default=25)
    parser.add_argument('--x-max', type=int, default=30)
    args = parser.parse_args()
    if args.x_pad < 1:
        parser.error('--x-pad 必须大于等于1，拒绝产生空切片')
    if args.seed is not None and not 0 <= args.seed < 2 ** 32:
        parser.error('--seed must be in [0, 2**32)')
    args.input = os.path.abspath(args.input)
    args.output = os.path.abspath(args.output)
    args.model = os.path.abspath(args.model)
    if args.index:
        args.index = os.path.abspath(args.index)
    phase = 'initialise'
    device = args.device
    temp_output = args.output + f'.part.{os.getpid()}'
    try:
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tools', 'RVC', 'RVC-beta-v2-0618')
        sys.path.insert(0, root)
        os.chdir(root)
        import torch
        torch.set_num_threads(args.cpu_threads)
        if args.seed is not None:
            np.random.seed(args.seed)
            torch.manual_seed(args.seed)
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
        from fairseq import checkpoint_utils
        from vc_infer_pipeline import VC
        if args.output_precision == 'float32':
            VC = floating_pipeline_class(VC)
        from infer_pack.models import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
                                      SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono)
        from my_utils import load_audio
        device = resolve_device(args.device, torch)
        if args.f0 == 'rmvpe':
            from core.f0_method import weight_path
            from core.rmvpe_adapter import rmvpe_vc_class
            VC = rmvpe_vc_class(VC, weight_path(root), device)
        config = SimpleNamespace(device=device, is_half=False, x_pad=args.x_pad,
                                 x_query=max(1, args.x_query), x_center=max(1, args.x_center),
                                 x_max=max(1, args.x_max))
        phase = 'model_load'
        cpt = load_voice_checkpoint(args.model, torch)
        from core.safe_checkpoint import validate_rvc_inference_checkpoint
        model_guard = validate_rvc_inference_checkpoint(cpt)
        safe_index = None
        index_loader_version = None
        if args.index and args.index_rate != 0:
            phase = 'index_load'
            from core.safe_faiss_index import load_faiss_index, ALGORITHM_VERSION
            index_loader_version = ALGORITHM_VERSION
            safe_index = load_faiss_index(args.index)
            expected_dimension = 768 if cpt.get('version', 'v1') == 'v2' else 256
            if safe_index.d != expected_dimension:
                raise ValueError('RVC index feature dimension disagrees with its checkpoint')
        phase = 'model_load'
        sr = cpt['config'][-1]
        cpt['config'][-3] = cpt['weight']['emb_g.weight'].shape[0]
        version = cpt.get('version', 'v1')
        has_f0 = cpt.get('f0', 1)
        classes = {('v1', 1): SynthesizerTrnMs256NSFsid, ('v1', 0): SynthesizerTrnMs256NSFsid_nono,
                   ('v2', 1): SynthesizerTrnMs768NSFsid, ('v2', 0): SynthesizerTrnMs768NSFsid_nono}
        cls = classes[(version, has_f0)]
        model = cls(*cpt['config'], **({'is_half': False} if has_f0 else {}))
        del model.enc_q
        model.load_state_dict(cpt['weight'], strict=True)
        model_guard['complete_weight_shapes_verified'] = True
        model.eval().to(device).float()
        hubert = checkpoint_utils.load_model_ensemble_and_task([os.path.join(root, 'hubert_base.pt')])[0][0]
        hubert.to(device).float().eval()
        phase = 'input_load'
        audio = load_audio(args.input, 16000)
        if len(audio) < 1600 or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-6:
            raise ValueError('RVC 输入太短、静音或包含非法采样')
        vc = capture_f0_class(safe_index_pipeline_class(VC, safe_index, args.index))(sr, config)
        vc._output_rate = sr
        phase = 'inference'
        with torch.inference_mode():
            result = vc.pipeline(hubert, model, 0, audio, args.input, [0, 0, 0], args.pitch, args.f0,
                                 args.index, args.index_rate if args.index else 0, has_f0,
                                 args.filter_radius, sr, 0, args.rms_mix, version, args.protect)
        if result.dtype == np.int16:
            result = result.astype(np.float32) / 32768.
        if not np.isfinite(result).all():
            raise ValueError('RVC 返回 NaN/Inf')
        phase = 'output_commit'
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        sf.write(temp_output, result, sr, format='WAV', subtype='FLOAT')
        write_f0_track(args.output + '.f0-track.json', vc, args.f0)
        os.replace(temp_output, args.output)
        print(json.dumps({'worker': 'rvc', 'status': 'complete', 'device': device,
                          'cpu_threads': args.cpu_threads,
                          'resource_partition': {'x_pad': args.x_pad, 'x_query': args.x_query, 'x_center': args.x_center, 'x_max': args.x_max},
                          'output_precision': args.output_precision, 'actual_f0': args.f0,
                          'validation_seed': args.seed, 'model_guard': model_guard,
                          'index_reader': None if safe_index is None else {
                              'algorithm': index_loader_version, 'dimension': safe_index.d,
                              'vectors': safe_index.ntotal, 'nlist': getattr(safe_index, 'nlist', None),
                              'nprobe': getattr(safe_index, 'nprobe', None)}}, ensure_ascii=False), flush=True)
    except Exception as exc:
        category = 'cuda_oom' if is_cuda_oom(exc) else 'worker_error'
        emit_worker_error(category, phase, device, exc, worker='rvc')
        try:
            import torch
            clear_cuda(torch)
        except Exception:
            pass
        raise
    finally:
        try:
            if os.path.exists(temp_output):
                os.remove(temp_output)
        except OSError:
            pass

if __name__ == '__main__':
    main()

