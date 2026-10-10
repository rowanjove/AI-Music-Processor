"""UI-first startup authorization; no audio-library imports before validation."""
import threading


class RuntimeGate:
    def __init__(self):
        self.state='PENDING'
        self.result=None
        self.cancelled=threading.Event()

    @property
    def ready(self):
        return self.state=='READY' and not self.cancelled.is_set()

    def finish(self,result):
        self.result=result
        self.state=('READY' if isinstance(result,dict) and result.get('status') in ('PASS','PASS_WITH_WARNINGS')
                    and not self.cancelled.is_set() else 'FAILED')

    def require_ready(self):
        if not self.ready:
            raise RuntimeError('运行环境尚未通过校验，不能执行音频处理')

    def require_mode(self,mode):
        self.require_ready()
        allowed=(self.result or {}).get('allowed_modes')
        if allowed is not None and mode not in allowed:
            raise RuntimeError('当前已安装组件不支持该模式，请先安装对应组件')


class DeferredPipeline:
    def __init__(self,gate,storage,log_callback,progress_callback):
        self.gate=gate
        self.storage=storage
        self.log_callback=log_callback
        self.progress_callback=progress_callback
        self._backend=None

    def process_file(self,*args,**kwargs):
        self.gate.require_ready()
        mode=kwargs.get('mode',args[1] if len(args)>1 else 'voice_conversion')
        resume_id=kwargs.get('resume_job_id',args[9] if len(args)>9 else None)
        if resume_id:
            job=self.storage.get_job(resume_id)
            if not job:
                raise RuntimeError('恢复任务不存在，不能执行音频处理')
            stored_mode=job.get('mode')
            if ('mode' in kwargs or len(args)>1) and mode!=stored_mode:
                raise RuntimeError('恢复任务模式与请求模式不一致')
            mode=stored_mode
        self.gate.require_mode(mode)
        if self._backend is None:
            from core.pipeline import AudioPipeline
            self._backend=AudioPipeline(storage=self.storage,log_callback=self.log_callback,
                                        progress_callback=self.progress_callback)
        return self._backend.process_file(*args,**kwargs)
