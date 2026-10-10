import os
import subprocess
from core.config import FFMPEG_EXE, CLEAN_PRESETS
from core.processes import run

def clean_audio_stream(input_path, output_path, preset_key="light", custom_filter=None, vocal=False):
    """
    应用可选静态滤波与轻度齿音处理；模型伪影仍需试听判断。
    """
    if custom_filter:
        filters = custom_filter
    else:
        preset = CLEAN_PRESETS.get(preset_key, CLEAN_PRESETS["medium"])
        filters = preset["filter"]

    # Compensate lookahead latency; vocal HPF differs from a full mix.
    filters = filters.replace('level=false', 'level=false:latency=true')
    if vocal:
        filters = filters.replace('highpass=f=35', 'highpass=f=75').replace('highpass=f=38', 'highpass=f=75')
        filters += ',deesser=i=0.15:m=0.3:f=0.5'
    cmd = [
        FFMPEG_EXE, "-y",
        "-i", input_path,
        "-af", filters,
        "-c:a", "pcm_f32le",
        "-ar", "48000",
        "-ac", "2",
        output_path
    ]

    run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    )
    return output_path
