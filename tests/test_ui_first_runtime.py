import importlib.machinery,importlib.util,queue,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.runtime_startup import RuntimeGate,DeferredPipeline


class UIFirstRuntimeTests(unittest.TestCase):
    def test_processing_is_denied_for_pending_fail_unknown_and_cancel(self):
        gate=RuntimeGate();pipe=DeferredPipeline(gate,None,None,None)
        for state in ('PENDING','FAILED'):
            gate.state=state
            with self.assertRaises(RuntimeError):pipe.process_file('anything')
        gate.finish({'status':'UNKNOWN'});self.assertFalse(gate.ready)
        gate.finish({'status':'PASS'});self.assertTrue(gate.ready)
        gate.cancelled.set();self.assertFalse(gate.ready)

    def test_real_window_stays_responsive_with_progress_and_failed_gate(self):
        loader=importlib.machinery.SourceFileLoader('ui_first_runtime_fixture',str(ROOT/'AI_Music_Processor.pyw'))
        spec=importlib.util.spec_from_loader(loader.name,loader);ui=importlib.util.module_from_spec(spec);loader.exec_module(ui)
        from core.storage import JobStore
        release=threading.Event();started=threading.Event()
        def slow_gate(**kwargs):
            started.set();kwargs['progress']('已校验 10/100 个运行文件')
            while not release.wait(.02):
                if kwargs['cancel_event'].is_set():return {'status':'FAIL','error':'cancelled'}
            return {'status':'FAIL','error':'test corruption'}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=JobStore(root/'jobs.db')
            with patch('core.storage.JobStore',return_value=store),patch('core.runtime_verify.verify_runtime',side_effect=slow_gate), \
                 patch.object(ui,'CONFIG_DIR',str(root)),patch.object(ui,'LOG_DIR',str(root/'logs')),patch.object(ui,'AudioPipeline',side_effect=AssertionError('eager audio import')), \
                 patch.object(ui.AIMusicProcessorApp,'_maybe_open_onboarding',lambda self:None):
                app=ui.AIMusicProcessorApp(defer_runtime_verification=True)
                try:
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline and not started.is_set():app.update();time.sleep(.01)
                    self.assertTrue(started.is_set());self.assertTrue(app.winfo_viewable())
                    for _ in range(15):app.update();time.sleep(.01)
                    self.assertIn('10/100',app.runtime_status.cget('text'))
                    self.assertEqual(str(app.runtime_progress.cget('mode')),'determinate')
                    self.assertFalse(app.runtime_gate.ready)
                    app.start();self.assertIn('尚未通过',app.status.cget('text'))
                    self.assertTrue(all('disabled' in b.state() for b in app.runtime_action_buttons))
                    release.set()
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline and app.runtime_check_running:app.update();time.sleep(.01)
                    self.assertEqual(app.runtime_gate.state,'FAILED')
                    self.assertIn('未通过',app.runtime_status.cget('text'))
                    self.assertIn('test corruption',(root/'logs/startup-runtime-check.json').read_text(encoding='utf-8'))
                finally:
                    release.set();app.close(persist=False)
                    for _ in range(30):
                        try:app.update()
                        except Exception:break
                        time.sleep(.01)

    def test_verifier_cancelled_event_fails_closed_and_does_not_leak_context(self):
        from core.runtime_verify import verify_runtime,_CANCEL_EVENT
        event=threading.Event();event.set()
        with tempfile.TemporaryDirectory() as folder:
            result=verify_runtime(root=folder,data_root=Path(folder).parent/'cancel-check',cancel_event=event)
        self.assertEqual(result['status'],'FAIL');self.assertIn('cancelled',result['error'])
        self.assertIsNone(_CANCEL_EVENT.get())

    def test_cancel_terminates_the_actual_dependency_probe(self):
        from core import runtime_verify
        event=threading.Event();timer=threading.Timer(.3,event.set)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            packages={'runtime_python':{'executable':'python.exe'},'search_order':[], 'module_resolution':{}}
            token=runtime_verify._CANCEL_EVENT.set(event);timer.start();started=time.monotonic()
            try:
                with patch.object(runtime_verify,'_PYTHON_PROBE','import time;time.sleep(30)'):
                    with self.assertRaises(runtime_verify.RuntimeVerificationError):
                        runtime_verify._probe_python(root,packages,60,root,python_override=sys.executable)
                self.assertLess(time.monotonic()-started,5)
            finally:timer.cancel();runtime_verify._CANCEL_EVENT.reset(token)
