"""Read-only source information; musical estimates use at most 90 seconds."""
import os
import tempfile
from pathlib import Path
from core.analyzer import probe_metadata,measure_loudness_ebur128,analyze_audio
from core.config import FFMPEG_EXE
from core.processes import run,process_scope
from core.storage import sha256_file


def preview_source(path,work_root,control=None):
    path=Path(path);original=sha256_file(path)
    root=Path(work_root).resolve();root.mkdir(parents=True,exist_ok=True)
    with process_scope(control),tempfile.TemporaryDirectory(prefix='source-preview-',dir=str(root)) as folder:
        metadata=probe_metadata(path)
        levels=measure_loudness_ebur128(path)
        sample=Path(folder)/'first-90s.wav'
        run([FFMPEG_EXE,'-v','error','-i',str(path),'-t','90','-vn','-ar','48000','-ac','2',
             '-c:a','pcm_f32le',str(sample)])
        estimates=analyze_audio(sample)
        if control is not None:control.check()
        if sha256_file(path)!=original:raise ValueError('分析期间源文件发生变化，请重新分析')
        return dict(metadata,**levels,bpm_estimate=estimates.get('bpm_estimate'),
                    key_estimate=estimates.get('key_estimate'),source_path=os.path.abspath(path),
                    source_sha256=original,musical_analysis_seconds=min(metadata['duration'],90),
                    writes_source=False)
