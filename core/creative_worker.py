"""Isolated optional packages, using already installed Torch and local models."""
import os
import sys
import argparse
import shutil
import json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT,'runtime','addons'))
from core.worker_runtime import clear_cuda, emit_worker_error, is_cuda_oom, resolve_device

def separate(input_path, output_dir, device='auto', segment=6, cpu_threads=4):
    os.makedirs(output_dir, exist_ok=True)
    import torch
    import soundfile as sf
    import numpy as np
    from scipy.signal import resample_poly
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    from pathlib import Path
    if type(cpu_threads) is not int or not 1 <= cpu_threads <= 4:
        raise ValueError('CPU threads必须在1到4之间')
    torch.set_num_threads(cpu_threads)
    model = get_model('htdemucs', repo=Path(ROOT)/'tools/UVR5/models/Demucs_Models/v3_v4_repo')
    device = resolve_device(device, torch)
    segment = float(segment)
    if not 1 <= segment <= 30:
        raise ValueError('Demucs segment 必须在1到30秒之间')
    model.to(device).eval()
    data, sr = sf.read(input_path, always_2d=True, dtype='float32')
    if sr != model.samplerate:
        import math
        divisor = math.gcd(sr, model.samplerate)
        data = resample_poly(data, model.samplerate//divisor, sr//divisor, axis=0)
    audio = torch.from_numpy(data.T.copy())
    if audio.shape[0] == 1:
        audio = audio.repeat(2,1)
    reference = audio.mean(0)
    mean, std = reference.mean(), reference.std()
    if std < 1e-6:
        raise ValueError('分轨输入静音')
    audio = (audio-mean)/std
    with torch.inference_mode():
        result = apply_model(model, audio[None], device=device, shifts=0, split=True,
                             segment=segment, overlap=.25, progress=True)[0].cpu()
    result = result*std+mean
    staging = os.path.join(output_dir, f'.staging-{os.getpid()}')
    os.makedirs(staging, exist_ok=True)
    try:
        staged = []
        for name, stem in zip(model.sources, result):
            name = 'vocal' if name == 'vocals' else name
            stage_path = os.path.join(staging, name + '.wav')
            sf.write(stage_path, stem.numpy().T, model.samplerate, subtype='FLOAT')
            staged.append((stage_path, os.path.join(output_dir, name + '.wav')))
        # All stems have been encoded before any destination is replaced.
        for stage_path, final_path in staged:
            os.replace(stage_path, final_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

def transcribe(input_path, output_path, bpm):
    import json
    import numpy as np
    from basic_pitch.inference import predict
    model = os.path.join(ROOT,'runtime','addons','basic_pitch','saved_models','icassp_2022','nmp.onnx')
    _, midi, events = predict(input_path, model, midi_tempo=bpm, minimum_note_length=100,
                              minimum_frequency=65., maximum_frequency=1400.)
    # Gentle 1/32-note grid; preserve sustained notes and refuse empty success.
    step = 60 / bpm / 8
    total = 0
    for instrument in midi.instruments:
        cleaned = []
        for note in instrument.notes:
            if not 24 <= note.pitch <= 108 or note.end-note.start < .08:
                continue
            note.start = max(0, round(note.start/step)*step)
            note.end = max(note.start+.08, round(note.end/step)*step)
            cleaned.append(note)
        instrument.notes = cleaned
        instrument.name = 'Transcribed melody'
        total += len(cleaned)
    if not total:
        raise ValueError('未识别到有效音符，请使用 NeuralNote 人工复查')
    midi.write(output_path)
    with open(output_path+'.json','w',encoding='utf-8') as f:
        json.dump(dict(engine='Basic Pitch 0.4.0 ONNX', bpm=bpm, note_count=total, quantization='1/32',
                       accuracy='需要人工修谱；不保证恢复所有配器'), f, ensure_ascii=False, indent=2)

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['separate','transcribe'])
    p.add_argument('input')
    p.add_argument('output')
    p.add_argument('--bpm',type=float,default=120)
    p.add_argument('--cpu-threads', type=int, choices=range(1, 5), default=4)
    p.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    p.add_argument('--segment', type=float, default=6)
    a = p.parse_args()
    device = a.device
    try:
        if a.action == 'separate':
            separate(a.input, a.output, device=a.device, segment=a.segment, cpu_threads=a.cpu_threads)
            print(json.dumps({'worker': 'demucs', 'status': 'complete', 'device': resolve_device(a.device, __import__('torch')), 'segment': a.segment, 'cpu_threads': a.cpu_threads}, ensure_ascii=False), flush=True)
        else:
            transcribe(a.input,a.output,a.bpm)
    except Exception as exc:
        category = 'cuda_oom' if is_cuda_oom(exc) else 'worker_error'
        emit_worker_error(category, 'load_or_inference' if a.action == 'separate' else 'transcribe', device, exc, worker='demucs')
        try:
            clear_cuda(__import__('torch'))
        except Exception:
            pass
        raise

