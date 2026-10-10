import os
from core.config import RVC_PYTHON, GPU_LOCK, BASE_DIR, get_available_rvc_models
from core.worker_runtime import run_worker_with_recovery

def convert_voice(input_vocal_wav, output_rvc_wav, model_key='zhoujielun',
                  pitch_shift=0, f0_method='harvest', index_rate=.65, temp_dir=None,
                  profile=None, execution_policy=None):
    # ``profile`` is the frozen model-info snapshot supplied by a resumed Job.
    # When present it is intentionally used verbatim; rescanning config here
    # would silently change pth/index or protect/rms settings between retries.
    if profile is None:
        models = get_available_rvc_models()
        if model_key not in models:
            raise ValueError('未找到 RVC 模型: ' + model_key)
        info = models[model_key]
    else:
        if not isinstance(profile, dict):
            raise ValueError('RVC 冻结 Profile 必须是对象')
        info = profile.get('model_info', profile)
        if not isinstance(info, dict):
            raise ValueError('RVC 冻结 Profile 缺少 model_info')
    model_path = info.get('pth') or info.get('model_path') or info.get('model')
    if not model_path:
        raise ValueError('RVC 冻结 Profile 缺少 pth')
    output_precision = info.get('output_precision', 'legacy_int16') if profile is not None else 'float32'
    if output_precision not in ('float32', 'legacy_int16'):
        raise ValueError('RVC output_precision 无效')
    index_path = info.get('index') or info.get('index_path') or ''
    model_profile = info.get('profile')
    if model_profile is None:
        # Accept a flat frozen profile for callers that already expanded it.
        model_profile = info
    if not isinstance(model_profile, dict):
        raise ValueError('RVC Profile 参数无效')
    from core.f0_method import resolve_f0_method
    f0_method = resolve_f0_method(f0_method)['executed']
    if not -24 <= int(pitch_shift) <= 24 or not 0 <= float(index_rate) <= 1:
        raise ValueError('RVC 音高或索引率超出范围')
    protect = float(model_profile.get('protect', .33))
    rms_mix_rate = float(model_profile.get('rms_mix_rate', .25))
    filter_radius = int(model_profile.get('filter_radius', 3))
    cmd = [RVC_PYTHON, os.path.join(BASE_DIR,'core','rvc_worker.py'),
           '--input', os.path.abspath(input_vocal_wav), '--output', os.path.abspath(output_rvc_wav),
           '--model', os.path.abspath(os.fspath(model_path)), '--index', os.path.abspath(os.fspath(index_path)) if index_path else '', '--pitch', str(int(pitch_shift)),
           '--f0', f0_method, '--output-precision', output_precision, '--index-rate', str(float(index_rate)),
           '--protect', str(protect), '--rms-mix', str(rms_mix_rate),
           '--filter-radius', str(filter_radius)]
    if not 0 <= protect <= .5 or not 0 <= rms_mix_rate <= 1 or not 0 <= filter_radius <= 7:
        raise ValueError('模型 Profile protect/rms_mix_rate 无效')
    if execution_policy is None:
        execution_policy = info.get('execution_policy')
    if execution_policy is not None:
        from core.execution_policy import validate_execution_policy
        policy = validate_execution_policy(execution_policy)
        cmd.extend(['--device', policy['device'], '--cpu-threads', str(policy['cpu_threads'])])
        for key, value in policy['rvc'].items():
            cmd.extend(['--' + key, str(value)])
    with GPU_LOCK:
        run_worker_with_recovery(
            cmd,
            [output_rvc_wav],
            timeout=14400,
            gpu_overrides={"x-pad": 1, "x-query": 2, "x-center": 12, "x-max": 18},
            sidecar_path=output_rvc_wav + '.worker.json',
        )
    if not os.path.isfile(output_rvc_wav):
        raise RuntimeError('RVC 未生成输出')
    return output_rvc_wav

