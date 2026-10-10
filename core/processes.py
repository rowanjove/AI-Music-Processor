"""Cancellable subprocess scope shared by every pipeline stage."""
import contextlib
import contextvars
import os
import subprocess
import threading
import time
import json
import datetime
import math
from core.paths import application_data_paths, is_release_mode, release_numba_cache_dir

_log_lock = threading.Lock()
_CAPTURE_LIMIT = 256 * 1024
_LOG_LIMIT = 8 * 1024 * 1024
_LOG_BACKUPS = 3
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_ENV_UNSET = object()


def _prepare_subprocess_env(env=_ENV_UNSET, base_dir=None, release_mode=None):
    """Build a child-only environment, pinning release workers to bundled tools."""
    root = os.path.abspath(base_dir or _BASE_DIR)
    strict = is_release_mode(root) if release_mode is None else bool(release_mode)
    if env is _ENV_UNSET:
        child_env = dict(os.environ, PYTHONIOENCODING='utf-8', OMP_NUM_THREADS='4')
    elif env is None:
        if not strict:
            return None
        child_env = dict(os.environ)
    else:
        child_env = dict(env)
    if strict:
        child_env['PYTHONDONTWRITEBYTECODE'] = '1'
        child_env['NUMBA_CACHE_DIR'] = str(release_numba_cache_dir(environ=child_env))
        runtime_bin = os.path.join(root, 'runtime', 'bin')
        required = [os.path.join(runtime_bin, name) for name in ('ffmpeg.exe', 'ffprobe.exe')]
        missing = [path for path in required if not os.path.isfile(path)]
        if missing:
            raise FileNotFoundError('Portable release is missing bundled FFmpeg tools: ' + ', '.join(missing))
        wanted = os.path.normcase(os.path.abspath(runtime_bin))
        inherited_path = child_env.get('PATH', os.environ.get('PATH', '')) or ''
        remaining = []
        for entry in inherited_path.split(os.pathsep):
            if not entry:
                continue
            try:
                if os.path.normcase(os.path.abspath(entry)) == wanted:
                    continue
            except (OSError, ValueError):
                pass
            remaining.append(entry)
        child_env['PATH'] = os.pathsep.join([runtime_bin] + remaining)
    return child_env


class _TailCapture:
    """Bounded pipe capture: retain the diagnostic tail, never unbounded RAM."""

    def __init__(self, limit=_CAPTURE_LIMIT):
        self.limit = int(limit)
        self.data = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def append(self, chunk):
        if not chunk:
            return
        with self.lock:
            self.total += len(chunk)
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[:-self.limit]

    def value(self):
        with self.lock:
            return bytes(self.data)

    @property
    def truncated(self):
        return self.total > self.limit


def _drain_pipe(pipe, capture, progress_callback=None):
    pending = b''
    try:
        while True:
            # read1 returns currently available pipe bytes; BufferedReader.read
            # may wait for 64 KiB and hide short live progress messages.
            reader = getattr(pipe, 'read1', pipe.read)
            chunk = reader(64 * 1024)
            if not chunk:
                return
            capture.append(chunk)
            if progress_callback is not None:
                pending = (pending + chunk)[-64 * 1024:]
                lines = pending.split(b'\n')
                pending = lines.pop()
                for line in lines:
                    try:
                        event = json.loads(line.decode('utf-8', errors='replace')).get('worker_progress')
                        fraction = event.get('fraction') if isinstance(event, dict) else None
                        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                            continue
                        progress_callback(dict(event, fraction=float(fraction), message=str(event.get('message', ''))[:1000]))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    except Exception:
                        # A failed display callback cannot break the audio pipe.
                        continue
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _collect_process_output(proc):
    captures = {}
    threads = []
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name, None)
        if pipe is None or not hasattr(pipe, "read"):
            continue
        capture = _TailCapture()
        captures[name] = capture
        callback = _worker_progress.get() if name == 'stdout' else None
        thread = threading.Thread(target=_drain_pipe, args=(pipe, capture, callback), daemon=True)
        thread.start()
        threads.append(thread)
    return captures, threads


def _finish_process_output(captures, threads):
    for thread in threads:
        thread.join(timeout=2.0)
    return {
        name: capture.value() for name, capture in captures.items()
    }, captures


def _rotate_log(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < _LOG_LIMIT:
            return
        for index in range(_LOG_BACKUPS, 0, -1):
            older = f"{path}.{index}"
            newer = f"{path}.{index + 1}"
            if index == _LOG_BACKUPS and os.path.exists(older):
                os.remove(older)
            elif os.path.exists(older):
                os.replace(older, newer)
        os.replace(path, f"{path}.1")
    except OSError:
        # A diagnostic log must never hide the worker's real result.
        pass

def _process_log(cmd, result, elapsed, captures=None, base_dir=None):
    directory = str(application_data_paths(base_dir or _BASE_DIR)["logs"])
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return
    decode = lambda value: value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
    record = dict(time=datetime.datetime.now().isoformat(), command=list(map(str, cmd)),
                  exit_code=result.returncode, elapsed_seconds=round(elapsed,3),
                  stdout=decode(result.stdout), stderr=decode(result.stderr))
    if captures:
        record['stdout_truncated'] = bool(captures.get('stdout') and captures['stdout'].truncated)
        record['stderr_truncated'] = bool(captures.get('stderr') and captures['stderr'].truncated)
    with _log_lock:
        path = os.path.join(directory, 'worker.jsonl')
        _rotate_log(path)
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            pass

class ProcessCancelled(RuntimeError):
    pass

class Control:
    def __init__(self):
        self.cancelled = threading.Event()
        self.paused = threading.Event()
    def cancel(self):
        self.cancelled.set()
    def pause(self):
        self.paused.set()
    def resume(self):
        self.paused.clear()
    def check(self):
        while self.paused.is_set() and not self.cancelled.is_set():
            time.sleep(0.1)
        if self.cancelled.is_set():
            raise ProcessCancelled("任务已取消；已完成步骤保留，可从历史继续")
    def run(self, cmd, timeout=7200, check=True, **kwargs):
        self.check()
        kwargs.pop('creationflags', None)
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        kwargs.setdefault('stdin', subprocess.DEVNULL)
        provided_env = kwargs['env'] if 'env' in kwargs else _ENV_UNSET
        kwargs['env'] = _prepare_subprocess_env(provided_env)
        started = time.monotonic()
        proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, **kwargs)
        captures, reader_threads = _collect_process_output(proc)
        try:
            while True:
                if self.cancelled.is_set():
                    raise ProcessCancelled("任务已取消；已完成步骤保留")
                if time.monotonic() - started > timeout:
                    raise TimeoutError("处理超时: " + os.path.basename(str(cmd[0])))
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            output, captures = _finish_process_output(captures, reader_threads)
            out = output.get('stdout')
            err = output.get('stderr')
            result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
            _process_log(cmd, result, time.monotonic()-started, captures)
            if check and proc.returncode:
                message = err.decode('utf-8', errors='replace') if isinstance(err, bytes) else str(err)
                raise RuntimeError("外部工具失败 (%s):\n%s" % (os.path.basename(str(cmd[0])), message[-6000:]))
            self.check()
            return result
        finally:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                output, captures = _finish_process_output(captures, reader_threads)
                out = output.get('stdout')
                err = output.get('stderr')
                try:
                    _process_log(cmd, subprocess.CompletedProcess(cmd, proc.returncode, out, err), time.monotonic()-started, captures)
                except OSError:
                    pass  # Preserve the original timeout/cancellation if logging storage is unavailable.

_control = contextvars.ContextVar('audio_process_control', default=None)
_worker_progress = contextvars.ContextVar('audio_worker_progress', default=None)


@contextlib.contextmanager
def worker_progress_scope(callback):
    token = _worker_progress.set(callback)
    try:
        yield
    finally:
        _worker_progress.reset(token)

def checkpoint():
    control = _control.get()
    if control:
        control.check()

@contextlib.contextmanager
def process_scope(control):
    token = _control.set(control)
    try:
        yield control
    finally:
        _control.reset(token)

def run(cmd, **kwargs):
    return (_control.get() or Control()).run(cmd, **kwargs)

