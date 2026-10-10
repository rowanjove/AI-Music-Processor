"""Conservative local resource estimates; these are limits, not a RAM guarantee."""
import os
import shutil
import math


def require_disk_budget(input_path, output_root, duration):
    estimate = max(os.path.getsize(input_path)*12,float(duration)*48000*2*4*12,64*1024**2)
    free = shutil.disk_usage(output_root).free
    if free < estimate:
        raise OSError(f'输出磁盘空间不足：至少需要约 {estimate/1024**3:.2f} GB，可用 {free/1024**3:.2f} GB')


def require_processing_budget(duration, mode, policy=None):
    policy = policy or {}
    limit = float(policy.get('max_duration_seconds',1800))
    memory = int(policy.get('memory_budget_bytes',2*1024**3))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('处理时长必须为有限正数')
    if not math.isfinite(limit) or limit <= 0 or memory <= 0:
        raise ValueError('处理时长及内存预算必须为正数')
    if duration > limit:
        raise ValueError(f'输入时长 {duration:.1f} 秒超过本机处理限额 {limit:.1f} 秒，请先切成片段或调整 processing_limits')
    if mode in ('voice_conversion','transcribe', 'polish'):
        # Full reference/conversion/candidate plus envelope FFT and worker overhead.
        # Polish holds stereo float64 processing buffers plus FFT/envelope
        # scratch arrays. It needs a budget just as the model modes do.
        sample_bytes = 120 if mode == 'polish' else 56
        estimated = duration*48000*sample_bytes + 256*1024**2
        if estimated > memory:
            raise MemoryError(f'预计工作内存约 {estimated/1024**3:.2f} GB，超过预算 {memory/1024**3:.2f} GB，请先切分音频')
