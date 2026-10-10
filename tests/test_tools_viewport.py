import importlib.machinery,importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core import config
from core.storage import JobStore
from core.pipeline import AudioPipeline
ROOT=Path(__file__).resolve().parents[1]


class ToolsViewportTests(unittest.TestCase):
    def test_minimum_window_can_reach_version_and_completion_controls(self):
        loader=importlib.machinery.SourceFileLoader('tools_viewport_fixture',str(ROOT/'AI_Music_Processor.pyw'))
        spec=importlib.util.spec_from_loader(loader.name,loader);ui=importlib.util.module_from_spec(spec);loader.exec_module(ui)
        # Keep a different interpreter as Tk's default root. The app's controls
        # must still change the variables belonging to its own interpreter.
        external=ui.tk.Tk();external.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'app.json').write_text(json.dumps(dict(first_run_completed=True,tutorial_dismissed=True)),encoding='utf-8')
            store=JobStore(root/'jobs.db')
            with patch.object(ui,'CONFIG_DIR',folder),patch.object(config,'CONFIG_DIR',folder), \
                 patch.object(ui,'get_available_rvc_models',return_value={}), \
                 patch.object(ui,'AudioPipeline',return_value=AudioPipeline(storage=store)):
                app=ui.AIMusicProcessorApp()
                try:
                    def descendants(widget):
                        yield widget
                        for child in widget.winfo_children():yield from descendants(child)
                    app.geometry('880x660');app.update()
                    notebook=next(w for w in descendants(app) if isinstance(w,ui.ttk.Notebook))
                    notebook.select(2);app.update()
                    buttons={}
                    for widget in descendants(app):
                        try:text=str(widget.cget('text'))
                        except Exception:continue
                        buttons[text]=widget
                    def visible(widget):
                        top=app.tools_canvas.winfo_rooty();bottom=top+app.tools_canvas.winfo_height()
                        return widget.winfo_ismapped() and widget.winfo_height()>1 and top<=widget.winfo_rooty() and widget.winfo_rooty()+widget.winfo_height()<=bottom
                    app.tools_canvas.yview_moveto(0);app.update()
                    self.assertTrue(visible(buttons['运行诊断']))
                    app.tools_canvas.yview_moveto(1);app.update()
                    for title in ('安装并启用新版本完整包','验证并回退上一版本','处理时防止系统休眠','程序内提醒'):
                        self.assertTrue(visible(buttons[title]),title)
                    buttons['处理时防止系统休眠'].invoke()
                    self.assertTrue(app.prevent_sleep_var.get())
                    buttons['处理时防止系统休眠'].invoke()
                    self.assertFalse(app.prevent_sleep_var.get())
                finally:
                    app.close(persist=False)
                    external.destroy()
