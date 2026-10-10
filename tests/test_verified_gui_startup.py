import contextlib,io,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import runtime_verify,paths


class VerifiedGUIStartupTests(unittest.TestCase):
    def test_normal_release_defers_gate_without_authorizing_runtime(self):
        source=(ROOT/'AI_Music_Processor.pyw').read_text(encoding='utf-8').split('import tkinter as tk')[0]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'PORTABLE_RELEASE').write_text(paths.PORTABLE_RELEASE_CONTENT)
            scope={'__name__':'__main__','__file__':str(root/'app.pyw')}
            with patch.object(sys,'argv',['app.pyw']),patch.object(paths,'release_numba_cache_dir',return_value=root/'cache'), \
                 patch.object(runtime_verify,'verify_runtime') as gate:
                exec(compile(source,'startup','exec'),scope)
            self.assertTrue(scope['_DEFER_RUNTIME_CHECK']);gate.assert_not_called()

    def test_same_process_runs_full_gate_once_and_ready_is_never_a_bypass(self):
        source=(ROOT/'AI_Music_Processor.pyw').read_text(encoding='utf-8').split('import tkinter as tk')[0]
        token='A'*64
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'PORTABLE_RELEASE').write_text(paths.PORTABLE_RELEASE_CONTENT)
            for status in ('PASS','PASS_WITH_WARNINGS','FAIL','UNKNOWN'):
                stream=io.StringIO()
                with patch.object(sys,'argv',['app.pyw','--smoke-test','--verify-and-run','--startup-token',token]), \
                     patch.object(paths,'release_numba_cache_dir',return_value=root/'cache'), \
                     patch.object(runtime_verify,'verify_runtime',return_value={'status':status}) as verify, \
                     contextlib.redirect_stdout(stream),contextlib.redirect_stderr(io.StringIO()):
                    if status in ('PASS','PASS_WITH_WARNINGS'):
                        exec(compile(source,'startup','exec'),{'__name__':'__main__','__file__':str(root/'app.pyw')})
                        self.assertEqual(stream.getvalue(),'AMP_RUNTIME_READY:'+token+'\n')
                    else:
                        with self.assertRaises(SystemExit) as stopped:
                            exec(compile(source,'startup','exec'),{'__name__':'__main__','__file__':str(root/'app.pyw')})
                        self.assertEqual(stopped.exception.code,1)
                        self.assertEqual(stream.getvalue(),'')
                    verify.assert_called_once()
                    self.assertNotIn('fast',verify.call_args.kwargs)

    def test_real_progress_callback_reports_before_ready(self):
        source=(ROOT/'AI_Music_Processor.pyw').read_text(encoding='utf-8').split('import tkinter as tk')[0]
        token='B'*64
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'PORTABLE_RELEASE').write_text(paths.PORTABLE_RELEASE_CONTENT)
            def gate(**kwargs):
                kwargs['progress']('已校验 10/35001 个运行文件')
                return {'status':'PASS'}
            stream=io.StringIO()
            with patch.object(sys,'argv',['app.pyw','--smoke-test','--verify-and-run','--startup-token',token]), \
                 patch.object(paths,'release_numba_cache_dir',return_value=root/'cache'), \
                 patch.object(runtime_verify,'verify_runtime',side_effect=gate),contextlib.redirect_stdout(stream):
                exec(compile(source,'startup','exec'),{'__name__':'__main__','__file__':str(root/'app.pyw')})
            self.assertEqual(stream.getvalue(),'AMP_RUNTIME_PROGRESS:已校验 10/35001 个运行文件\nAMP_RUNTIME_READY:'+token+'\n')


if __name__=='__main__':unittest.main()
