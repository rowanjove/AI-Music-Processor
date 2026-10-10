import os
import sys
import shutil
import argparse
import warnings
import json
warnings.filterwarnings("ignore")

# 切换工作目录至 RVC 根目录，使得 uvr5_pack 与 infer_uvr5 能够正常导入
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
rvc_root = os.path.abspath(os.path.join(script_dir, "..", "tools", "RVC", "RVC-beta-v2-0618"))
sys.path.insert(0, rvc_root)
os.chdir(rvc_root)

import torch
from infer_uvr5 import _audio_pre_, _audio_pre_new
from core.worker_runtime import clear_cuda, emit_worker_error, is_cuda_oom, resolve_device

def validate_window_size(window_size, model):
    if window_size <= 2 * int(model.offset) or window_size % 32:
        raise ValueError("UVR window_size 必须大于模型双侧裁剪宽度且为32的倍数")

def run_uvr_separation(input_audio, vocal_out, inst_out, model_name="HP2_all_vocals.pth", agg=10,
                       device="auto", window_size=512, cpu_threads=None, disable_cudnn=False, zero_band_buffers=True):
    input_audio = os.path.abspath(input_audio)
    vocal_out = os.path.abspath(vocal_out)
    inst_out = os.path.abspath(inst_out)

    device = resolve_device(device, torch)
    if disable_cudnn:
        # Explicit policy choice only, never a claimed fix for band-buffer NaN.
        torch.backends.cudnn.enabled = False
    if cpu_threads is not None:
        if type(cpu_threads) is not int or not 1 <= cpu_threads <= 4:
            raise ValueError('CPU threads 必须在1到4之间')
        torch.set_num_threads(cpu_threads)
    is_half = False  # GTX 1060 强制单精度
    window_size = int(window_size)
    if not 128 <= window_size <= 2048:
        raise ValueError("UVR window_size 必须在128到2048之间")

    model_path = os.path.join(rvc_root, "uvr5_weights", model_name)
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"未找到 UVR5 权重: {model_path}")

    # 准备临时输出目录
    temp_dir = os.path.join(os.path.dirname(vocal_out), f"temp_uvr_{os.getpid()}")
    temp_voc = os.path.join(temp_dir, "vocal")
    temp_ins = os.path.join(temp_dir, "ins")
    os.makedirs(temp_voc, exist_ok=True)
    os.makedirs(temp_ins, exist_ok=True)

    try:
        from core.uvr_trusted_weights import create_trusted_uvr_model
        if "DeEcho" in model_name or "DeReverb" in model_name:
            pre_fun = create_trusted_uvr_model(_audio_pre_new, model_path, device=device, is_half=is_half, agg=agg)
            if zero_band_buffers:
                from core.uvr_band_safety import install_zero_band_buffers
                install_zero_band_buffers(pre_fun)
            validate_window_size(window_size, pre_fun.model)
            pre_fun.data["window_size"] = window_size
            pre_fun._path_audio_(input_audio, vocal_root=temp_voc, ins_root=temp_ins, format="wav")
        else:
            pre_fun = create_trusted_uvr_model(_audio_pre_, model_path, agg=agg, device=device, is_half=is_half)
            if zero_band_buffers:
                from core.uvr_band_safety import install_zero_band_buffers
                install_zero_band_buffers(pre_fun)
            validate_window_size(window_size, pre_fun.model)
            pre_fun.data["window_size"] = window_size
            pre_fun._path_audio_(input_audio, ins_root=temp_ins, vocal_root=temp_voc, format="wav")

        # 移动并重命名输出
        voc_files = [os.path.join(temp_voc, f) for f in os.listdir(temp_voc) if f.endswith(".wav")]
        ins_files = [os.path.join(temp_ins, f) for f in os.listdir(temp_ins) if f.endswith(".wav")]

        if not voc_files or not ins_files:
            raise RuntimeError("UVR5 分轨输出文件未找到！")

        os.makedirs(os.path.dirname(vocal_out), exist_ok=True)
        os.makedirs(os.path.dirname(inst_out), exist_ok=True)
        vocal_part = vocal_out + f".part.{os.getpid()}"
        inst_part = inst_out + f".part.{os.getpid()}"
        try:
            shutil.copy2(voc_files[0], vocal_part)
            shutil.copy2(ins_files[0], inst_part)
            os.replace(vocal_part, vocal_out)
            os.replace(inst_part, inst_out)
        finally:
            for partial in (vocal_part, inst_part):
                try:
                    if os.path.exists(partial):
                        os.remove(partial)
                except OSError:
                    pass
        return {'model_guard': pre_fun.authenticated_checkpoint}

    finally:
        # 清理显存与临时文件夹
        clear_cuda(torch)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--vocal_out", required=True)
    parser.add_argument("--inst_out", required=True)
    parser.add_argument("--model", default="HP2_all_vocals.pth")
    parser.add_argument("--agg", type=int, default=10)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--window-size", type=int, default=512)
    parser.add_argument('--cpu-threads', type=int, choices=range(1, 5), default=None)
    parser.add_argument('--disable-cudnn', action='store_true')
    parser.add_argument('--zero-band-buffers', action='store_true', default=True)
    args = parser.parse_args()
    device = args.device
    try:
        evidence = run_uvr_separation(args.input, args.vocal_out, args.inst_out, model_name=args.model,
                           agg=args.agg, device=args.device, window_size=args.window_size, cpu_threads=args.cpu_threads, disable_cudnn=args.disable_cudnn, zero_band_buffers=args.zero_band_buffers)
        print(json.dumps({'worker': 'uvr', 'status': 'complete', 'device': resolve_device(args.device, torch), 'window_size': args.window_size, 'cpu_threads': torch.get_num_threads(), 'cudnn_enabled': torch.backends.cudnn.enabled, 'zero_initialized_band_buffers': args.zero_band_buffers, **(evidence or {})}, ensure_ascii=False), flush=True)
    except Exception as exc:
        category = 'cuda_oom' if is_cuda_oom(exc) else 'worker_error'
        emit_worker_error(category, 'load_or_inference', device, exc, worker='uvr')
        clear_cuda(torch)
        raise

if __name__ == "__main__":
    main()
