"""One heavy GPU job across threads AND independent app/CLI processes."""
import os
import threading
import time
import msvcrt
from core.processes import checkpoint
from core.paths import application_data_paths

def _gpu_lock_path(base_dir=None):
    return os.path.join(str(application_data_paths(base_dir)["temp"]), 'gpu.lock')

class GPULock:
    def __init__(self):
        self.thread_lock=threading.Lock()
        self.handle=None
    def __enter__(self):
        while not self.thread_lock.acquire(timeout=.1):
            checkpoint()
        try:
            path=_gpu_lock_path()
            os.makedirs(os.path.dirname(path),exist_ok=True)
            self.handle=open(path,'a+b')
            if os.path.getsize(path)==0:
                self.handle.write(b'0');self.handle.flush()
            while True:
                checkpoint()
                self.handle.seek(0)
                try:
                    msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
                    return self
                except OSError:
                    time.sleep(.1)
        except Exception:
            if self.handle:self.handle.close();self.handle=None
            self.thread_lock.release()
            raise
    def __exit__(self,*args):
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            self.handle.close()
            self.handle=None
        finally:
            self.thread_lock.release()

