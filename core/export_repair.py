"""Classify broken encodings, never turn a failed sound-quality gate into a pass."""
import os
from core.analyzer import probe_metadata
from core.config import FFMPEG_EXE
from core.processes import run,ProcessCancelled


def corrupt_exports(outputs,expected_duration=None):
    broken=[]
    for role in ('wav','mp3','flac'):
        path=outputs.get(role)
        if not path:continue
        try:
            metadata=probe_metadata(path)
            if expected_duration is not None and abs(metadata['duration']-float(expected_duration))>.25:
                raise ValueError('导出时长与输入不一致')
            run([FFMPEG_EXE,'-v','error','-xerror','-i',os.fspath(path),'-map','0:a:0','-f','null','-'])
            if role=='wav':
                from core.qc import sample_metrics
                sample_metrics(path)
        except ProcessCancelled:raise
        except (OSError,ValueError,RuntimeError) as exc:
            broken.append(dict(role=role,path=os.fspath(path),reason=str(exc)))
        except Exception as exc:
            import subprocess
            if not isinstance(exc,subprocess.CalledProcessError):raise
            broken.append(dict(role=role,path=os.fspath(path),reason=str(exc)))
    return broken
