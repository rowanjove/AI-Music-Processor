"""Local desktop music workbench. All Tk operations stay on the UI thread."""
import os
import sys
# First line defense: completely disable bytecode generation across the application
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
_PORTABLE_RELEASE = (os.path.lexists(os.path.join(_APP_ROOT, 'PORTABLE_RELEASE')) or
                     os.path.lexists(os.path.join(_APP_ROOT, 'release-manifest.json')))
if _PORTABLE_RELEASE:
    sys.path.insert(0, _APP_ROOT)
    from core.paths import release_numba_cache_dir
    try:
        os.environ['NUMBA_CACHE_DIR'] = str(release_numba_cache_dir())
    except Exception as _data_path_error:
        _message = 'Release 数据目录无效，程序未启动：' + str(_data_path_error)
        if '--verify-runtime' in sys.argv:
            sys.stderr.write(_message + '\n')
        else:
            try:
                import ctypes
                ctypes.WinDLL('user32', use_last_error=True).MessageBoxW(
                    None, _message, 'AI Music Processor', 0x10)
            except Exception:
                sys.stderr.write(_message + '\n')
        raise SystemExit(1)

import json
import gc
import copy
import queue
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
_APP_ROOT = Path(_APP_ROOT)

# Keep both release launch paths independent from Tk, config and JobStore
# startup. Launcher and direct pythonw use the same full gate. A Launcher
# session continues into Tk in this process after verification, avoiding a
# second full inventory read. The token is a readiness signal, never a bypass.
if __name__ == '__main__' and '--verify-runtime' in sys.argv:
    from core.runtime_verify import main as _verify_runtime_main
    raise SystemExit(_verify_runtime_main([arg for arg in sys.argv[1:] if arg != '--verify-runtime']))
_DEFER_RUNTIME_CHECK=(__name__=='__main__' and _PORTABLE_RELEASE and '--smoke-test' not in sys.argv
                      and '--verify-runtime' not in sys.argv)
if __name__ == '__main__' and _PORTABLE_RELEASE and not _DEFER_RUNTIME_CHECK:
    from core.runtime_verify import verify_runtime as _verify_runtime
    def _startup_progress(message):
        if '--verify-and-run' in sys.argv:
            print('AMP_RUNTIME_PROGRESS:' + message, flush=True)
    _runtime_result = _verify_runtime(root=_APP_ROOT, timeout=900, progress=_startup_progress)
    if _runtime_result.get('status') not in ('PASS','PASS_WITH_WARNINGS'):
        _details = json.dumps(_runtime_result, ensure_ascii=False, indent=2)
        if '--verify-and-run' in sys.argv or '--smoke-test' in sys.argv:
            sys.stderr.write(_details + '\n')
        else:
            try:
                import ctypes
                _user32 = ctypes.WinDLL('user32', use_last_error=True)
                _user32.MessageBoxW(None, '运行环境校验失败，主程序未启动。\n\n' + _details,
                                     'AI Music Processor', 0x10)
            except Exception:
                sys.stderr.write(_details + '\n')
        raise SystemExit(1)
    if '--verify-and-run' in sys.argv:
        try:
            _token_index=sys.argv.index('--startup-token')
            _startup_token=sys.argv[_token_index+1]
            if len(_startup_token)!=64 or any(letter not in '0123456789ABCDEF' for letter in _startup_token):
                raise ValueError('无效的启动握手')
        except (ValueError,IndexError):
            raise SystemExit(1)
        print('AMP_RUNTIME_READY:'+_startup_token,flush=True)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if _DEFER_RUNTIME_CHECK:
    os.environ['AI_MUSIC_PROCESSOR_UI_PATHS_ONLY']='1'
from core.config import (BASE_DIR, OUTPUT_DIR, CONFIG_DIR, TEMP_DIR, LOG_DIR, STARTUP_ERROR_PATH,
                         CLEAN_PRESETS, MASTER_PRESETS,
                         SUPPORTED_EXTENSIONS, get_available_rvc_models, load_settings, save_settings,
                         save_model_profile, get_config_warnings)
if _DEFER_RUNTIME_CHECK:
    os.environ.pop('AI_MUSIC_PROCESSOR_UI_PATHS_ONLY',None)

def AudioPipeline(*args,**kwargs):
    from core.pipeline import AudioPipeline as Implementation
    return Implementation(*args,**kwargs)
from core.processes import Control, ProcessCancelled
from core.transcriber import (open_in_audacity, open_in_musescore, open_in_lmms,
                              launch_neuralnote, validate_editor_input)


PROCESSING_KEYS = {'mode','clean_preset','master_preset','rvc_model_key','pitch_shift','f0_method',
                   'device','performance_profile','rvc_postprocess_profile','adaptive_mix','mix_bus_profile',
                   'input_channel_mode','output_channels','detailed_analysis',
                   'mix_vocal_gain_db','mix_instrumental_gain_db','transcribe_stem',
                   'index_rate','bpm','generate_midi','output_sample_rate','output_formats',
                   'custom_output_dir','dispatch_id','quality_assist','quality_retry_limit',
                   'parameter_search_enabled','parameter_search_budget','vocal_restoration_effect',
                   'vocal_restore_profile',
                   'pitch_correction_mode','pitch_correction_key',
                   'rvc_input_headroom_strategy',
                   'source_hint','source_preclean_enabled',
                   'separator_profile','polish_profile','polish_reference','polish_options',
                   'midi_cleanup','mix_ducking'}

def processing_parameters(snapshot):
    # Internal schema/hash/effective snapshots are read by Pipeline on resume;
    # GUI dispatch only passes the public processing API arguments.
    return {key:value for key,value in snapshot.items() if key in PROCESSING_KEYS}

def build_mode_flow_panel(parent, mode, generate_midi=False, source_preclean=False,
                          vocal_restore=False, parameter_search=False, pitch_correction=False,
                          midi_cleanup=False, mix_ducking=None):
    """Labels whose text is the stage list for this mode."""
    from core.ux_flow import mode_stages
    frame = tk.Frame(parent)
    frame.stage_labels = {}
    frame.current_stage = None
    frame.mode = mode
    for name in mode_stages(mode, generate_midi=generate_midi,
                            source_preclean=source_preclean,
                            vocal_restore=vocal_restore,
                            parameter_search=parameter_search,
                            pitch_correction=pitch_correction,
                            midi_cleanup=midi_cleanup,
                            mix_ducking=mix_ducking):
        label = tk.Label(frame, text=name)
        frame.stage_labels[name] = label
    def arrange(_event=None):
        width=max(frame.winfo_width(),300)
        cell=max([label.winfo_reqwidth()+6 for label in frame.stage_labels.values()] or [100])
        columns=max(1,width//cell)
        if getattr(frame, '_last_columns', None) == columns:
            return
        frame._last_columns = columns
        for index,label in enumerate(frame.stage_labels.values()):
            label.grid(row=index//columns,column=index%columns,sticky='w',padx=3,pady=2)
    frame.bind('<Configure>',arrange)
    arrange()
    return frame


def wrap_button_row(frame):
    """Keep all actions reachable when the window is narrower than a row."""
    def arrange(_event=None):
        children=frame.winfo_children()
        if not children:return
        width=max(frame.winfo_width(),300)
        if getattr(frame, '_last_arrange_width', None) == width:
            return
        frame._last_arrange_width = width
        x,row,column=0,0,0
        for child in children:child.pack_forget()
        for child in children:
            requested=child.winfo_reqwidth()+6
            if column and x+requested>width:row+=1;column=0;x=0
            child.grid(row=row,column=column,sticky='w',padx=3,pady=2)
            x+=requested;column+=1
    frame.bind('<Configure>',arrange)
    arrange()

def highlight_mode_stage(panel, stage, message=None):
    """Mark the stage the running job just reported."""
    labels = getattr(panel, 'stage_labels', {})
    if stage not in labels:
        return getattr(panel, 'current_stage', None)
    states = getattr(panel, 'stage_states', None)
    if states is None:
        states = panel.stage_states = {}
    skipped = isinstance(message, str) and message.startswith('跳过：')
    if skipped:
        states[stage] = 'SKIPPED'
    else:
        states[stage] = 'RUNNING'
    panel.current_stage = stage
    for name, label in labels.items():
        if states.get(name) == 'SKIPPED':
            text = '↷ ' + name + '（跳过）'
        elif name == stage:
            text = '● ' + name
        else:
            text = name
        label.configure(text=text)
    return panel.current_stage

def build_first_run_wizard(parent, registry, hardware, on_skip=None, on_finish=None, on_import=None,
                           worker_launcher=None, initial_output=None, initial_cache_quota='20',execution_guard=None,
                           on_import_package=None):
    """Next, back, skip, mode choice, and self-test controls bound to FirstRunWizard."""
    from core.ux_flow import FirstRunWizard
    session = FirstRunWizard(registry, hardware)
    frame = ttk.Frame(parent, padding=12)
    frame.session = session
    frame.step_label = ttk.Label(frame, text=session.title())
    frame.step_label.pack(anchor='w')
    frame.body = tk.Text(frame, wrap='word', height=12)
    frame.body.pack(fill='both', expand=True, pady=8)
    choices = ttk.Frame(frame)
    choices.pack(fill='x', pady=(0, 6))
    frame.mode_var = tk.StringVar(master=parent,value='')
    frame.separator_var = tk.StringVar(master=parent,value='dual')
    frame.output_var = tk.StringVar(master=parent,value=initial_output or OUTPUT_DIR)
    frame.cache_quota_var = tk.StringVar(master=parent,value=initial_cache_quota)

    def choose_mode():
        mode = frame.mode_var.get()
        if mode:
            session.select_mode(mode)
            refresh()

    def choose_separator():
        session.select_separator(frame.separator_var.get())
        refresh()

    mode_row = ttk.Frame(choices)
    mode_row.pack(fill='x')
    frame.mode_polish = ttk.Radiobutton(mode_row, text='精修', value='polish', variable=frame.mode_var, command=choose_mode)
    frame.mode_voice = ttk.Radiobutton(mode_row, text='换声重制', value='voice_conversion', variable=frame.mode_var, command=choose_mode)
    frame.mode_transcribe = ttk.Radiobutton(mode_row, text='扒谱', value='transcribe', variable=frame.mode_var, command=choose_mode)
    route_row = ttk.Frame(choices)
    route_row.pack(fill='x')
    frame.separator_hp2 = ttk.Radiobutton(route_row, text='快速 HP2', value='hp2', variable=frame.separator_var, command=choose_separator)
    frame.separator_dual = ttk.Radiobutton(route_row, text='Kim Vocal 2 + Inst HQ 3', value='dual', variable=frame.separator_var, command=choose_separator)
    for button in (frame.mode_polish, frame.mode_voice, frame.mode_transcribe, frame.separator_hp2, frame.separator_dual):
        button.pack(side='left', padx=4)
    paths_row = ttk.Frame(choices)
    paths_row.pack(fill='x',pady=4)
    ttk.Label(paths_row,text='成品目录').pack(side='left')
    ttk.Entry(paths_row,textvariable=frame.output_var).pack(side='left',fill='x',expand=True,padx=4)
    def choose_output():
        selected=filedialog.askdirectory(parent=parent,initialdir=frame.output_var.get())
        if selected:frame.output_var.set(selected)
    frame.output_button=ttk.Button(paths_row,text='选择',command=choose_output)
    frame.output_button.pack(side='left')
    quota_row=ttk.Frame(choices)
    quota_row.pack(fill='x')
    ttk.Label(quota_row,text='缓存上限 GiB（留空不限；完成时保存）').pack(side='left')
    ttk.Entry(quota_row,textvariable=frame.cache_quota_var,width=10).pack(side='left',padx=4)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x')

    def refresh():
        frame.step_label.configure(text=session.title())
        frame.body.delete('1.0', 'end')
        frame.body.insert('end', session.body())

    frame.refresh = refresh

    def on_next():
        session.next()
        refresh()

    def on_back():
        session.back()
        refresh()

    def skip():
        session.skip()
        refresh()
        if on_skip is not None:
            on_skip()

    def finish():
        from core.ux_flow import first_run_preferences
        try:
            session.preferences=first_run_preferences(frame.output_var.get(),frame.cache_quota_var.get())
        except ValueError as error:
            messagebox.showerror('首次设置无效',str(error),parent=parent);return
        session.finish()
        refresh()
        if on_finish is not None:
            on_finish(session)

    def on_test():
        if execution_guard is not None:
            reason=execution_guard()
            if reason:
                frame.body.insert('end','\n暂不能执行自检：'+str(reason));return
        if frame.__dict__.get('runtime_blocked',False):
            frame.body.insert('end','\n组件已变化，请重新启动并通过运行时验证后再自检。')
            return
        chain = session.selected_chain()
        if chain not in ('polish', 'voice_conversion', 'transcribe','dual'):
            session.run_self_test(runner=None)
            refresh()
            return
        if getattr(frame, 'self_test_running', False):
            return
        frame.self_test_running = True
        frame.self_test_button.state(['disabled'])
        frame.body.insert('end', '\n正在跑这条链路的短自检。')
        box = queue.Queue()

        def task():
            try:
                box.put(session.run_self_test(runner=None))
            except Exception as exc:
                box.put(exc)

        def poll():
            result=getattr(frame,'self_test_result',None)
            if result is None:
                try:
                    result = box.get_nowait()
                except queue.Empty:
                    if frame.winfo_exists():
                        frame.after(100, poll)
                    return
                frame.self_test_result=result
            # The worker posts its result just before returning. Do not report
            # it idle until Thread.run has released the closure on that worker.
            thread=getattr(frame,'self_test_thread',None)
            if thread is not None and thread.is_alive():
                if frame.winfo_exists():
                    frame.after(10,poll)
                return
            del frame.self_test_result
            frame.self_test_running = False
            try:
                frame.self_test_button.state(['disabled'] if frame.__dict__.get('runtime_blocked',False) else ['!disabled'])
            except tk.TclError:
                return
            if isinstance(result, Exception):
                session.self_test = {
                    'status': 'READY_FOR_EXTERNAL_VALIDATION',
                    'gpu_pass': False,
                    'inference_ran': False,
                    'start_gpu': False,
                    'writes_history': False,
                    'quality_acceptance': 'NOT_ASSESSED',
                    'listening_quality': 'not_assessed',
                    'reason': '短自检没有开始：' + str(result),
                }
            refresh()

        if worker_launcher is None:
            frame.self_test_thread=threading.Thread(target=task,daemon=True,name='quick-self-test')
            frame.self_test_thread.start()
        else:
            frame.self_test_thread=worker_launcher(task,daemon=True,name='quick-self-test')
        frame.after(100, poll)

    frame.next_button = ttk.Button(buttons, text='下一步', command=on_next)
    frame.back_button = ttk.Button(buttons, text='上一步', command=on_back)
    frame.skip_button = ttk.Button(buttons, text='稍后设置', command=skip)
    frame.self_test_button = ttk.Button(buttons, text='快速自检', command=on_test)
    imports = ttk.Frame(frame)
    imports.pack(fill='x',pady=(4,0))
    frame.import_button = ttk.Button(imports, text='从同版本离线目录安装/修复', command=on_import or (lambda: None))
    frame.import_button.pack(side='left',padx=3)
    frame.import_package_button = None
    if on_import_package is not None:
        frame.import_package_button = ttk.Button(imports,text='导入 .ampkg 组件包',command=on_import_package)
        frame.import_package_button.pack(side='left',padx=4)
    frame.finish_button = ttk.Button(buttons, text='完成并打开教程', command=finish)
    for button in (frame.next_button, frame.back_button, frame.skip_button, frame.self_test_button,
                   frame.finish_button):
        button.pack(side='left', padx=3)
    refresh()
    return frame

def build_tutorial_panel(parent, mode='polish', target_for=None, on_dismiss=None):
    """Next, back, skip, and reopen. The current step highlights a main-window control."""
    from core.ux_flow import TutorialSession
    session = TutorialSession(mode, target_for)
    frame = ttk.Frame(parent, padding=12)
    frame.session = session
    frame.step_label = ttk.Label(frame, text=session.title())
    frame.step_label.pack(anchor='w')
    frame.body = tk.Text(frame, wrap='word', height=8)
    frame.body.pack(fill='both', expand=True, pady=8)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x')

    def refresh():
        frame.step_label.configure(text=session.title())
        frame.body.delete('1.0', 'end')
        frame.body.insert('end', session.body())
        session.apply_highlight()

    frame.refresh = refresh

    def on_next():
        session.next()
        refresh()

    def on_back():
        session.back()
        refresh()

    def on_skip():
        session.skip()
        refresh()
        if on_dismiss is not None:
            on_dismiss()

    def on_done():
        session.complete()
        refresh()
        if on_dismiss is not None:
            on_dismiss()

    def on_reopen():
        session.reopen()
        refresh()

    frame.next_button = ttk.Button(buttons, text='下一步', command=on_next)
    frame.back_button = ttk.Button(buttons, text='上一步', command=on_back)
    frame.skip_button = ttk.Button(buttons, text='跳过教程', command=on_skip)
    frame.done_button = ttk.Button(buttons, text='完成', command=on_done)
    frame.reopen_button = ttk.Button(buttons, text='重新打开', command=on_reopen)
    for button in (frame.next_button, frame.back_button, frame.skip_button, frame.done_button, frame.reopen_button):
        button.pack(side='left', padx=3)
    refresh()
    return frame

def hook_drop_native(window, inbox):
    """Native WndProc must never re-enter Tcl; deliver paths through a Python queue."""
    import ctypes as c
    from ctypes import wintypes as w
    user = c.WinDLL('user32', use_last_error=True)
    shell = c.WinDLL('shell32', use_last_error=True)
    integer = c.c_ssize_t
    pointer = c.c_void_p
    user.GetParent.argtypes = [pointer]
    user.GetParent.restype = pointer
    hwnd = user.GetParent(window.winfo_id()) or window.winfo_id()
    getter = user.GetWindowLongPtrW if c.sizeof(pointer)==8 else user.GetWindowLongW
    setter = user.SetWindowLongPtrW if c.sizeof(pointer)==8 else user.SetWindowLongW
    getter.argtypes = [pointer,c.c_int]
    getter.restype = integer
    setter.argtypes = [pointer,c.c_int,integer]
    setter.restype = integer
    user.CallWindowProcW.argtypes = [pointer,pointer,w.UINT,c.c_size_t,integer]
    user.CallWindowProcW.restype = integer
    shell.DragQueryFileW.argtypes = [pointer,w.UINT,w.LPWSTR,w.UINT]
    shell.DragQueryFileW.restype = w.UINT
    shell.DragAcceptFiles.argtypes = [pointer,w.BOOL]
    shell.DragAcceptFiles.restype = None
    shell.DragFinish.argtypes = [pointer]
    shell.DragFinish.restype = None
    old = getter(hwnd,-4)
    if not old:
        raise c.WinError(c.get_last_error())
    def receive(h,msg,wp,lp):
        if msg==0x233:
            files=[]
            try:
                for i in range(shell.DragQueryFileW(wp,0xffffffff,None,0)):
                    size=shell.DragQueryFileW(wp,i,None,0)
                    buf=c.create_unicode_buffer(size+1)
                    shell.DragQueryFileW(wp,i,buf,size+1)
                    files.append(buf.value)
            except Exception as exc:
                inbox.put(('log','拖拽读取失败：'+str(exc)))
            finally:
                shell.DragFinish(wp)
            # This callback executes inside Tk's native event dispatch, outside its
            # normal Tcl/Python bridge. Even window.after() here can abort Python.
            inbox.put(('drop',files))
            return 0
        return user.CallWindowProcW(old,h,msg,wp,lp)
    window._drop_proc=c.WINFUNCTYPE(integer,pointer,w.UINT,c.c_size_t,integer)(receive)
    c.set_last_error(0)
    result = setter(hwnd,-4,c.cast(window._drop_proc,pointer).value)
    if not result and c.get_last_error():
        raise c.WinError(c.get_last_error())
    window._drop_native = (user,shell,setter,hwnd,old)
    shell.DragAcceptFiles(hwnd,True)

def unhook_drop_native(window):
    state = getattr(window,'_drop_native',None)
    if state:
        user,shell,setter,hwnd,old = state
        shell.DragAcceptFiles(hwnd,False)
        setter(hwnd,-4,old)
        window._drop_native = None

def dispose_tk_variables(root):
    """Unset Tk variables on their owner thread and sever late-GC interpreter refs."""
    def belongs_to_root(widget):
        seen=set()
        current=widget
        while current is not None and id(current) not in seen:
            if current is root:
                return True
            seen.add(id(current))
            current=getattr(current,'master',None)
        return False

    pending=[root]
    seen_widgets=set()
    seen_containers=set()
    variables={}
    while pending:
        widget=pending.pop()
        if id(widget) in seen_widgets:
            continue
        seen_widgets.add(id(widget))
        try:
            pending.extend(widget.winfo_children())
        except (AttributeError,tk.TclError):
            pass
        values=list(vars(widget).values()) if hasattr(widget,'__dict__') else []
        containers=list(values)
        while containers:
            value=containers.pop()
            if isinstance(value,tk.Variable):
                variables[id(value)]=value
            elif isinstance(value,tk.Misc) and belongs_to_root(value):
                pending.append(value)
            elif isinstance(value,dict):
                if id(value) not in seen_containers:
                    seen_containers.add(id(value))
                    containers.extend(value.values())
            elif isinstance(value,(list,tuple,set)):
                if id(value) not in seen_containers:
                    seen_containers.add(id(value))
                    containers.extend(value)
    for variable in variables.values():
        if getattr(variable,'_tk',None) is None:
            continue
        try:
            variable.__del__()
        except (tk.TclError,RuntimeError):
            # A destroyed child may already have removed its Tcl variable.
            pass
        finally:
            variable._tk=None
            variable._root=None

class AIMusicProcessorApp(tk.Tk):
    def __init__(self,defer_runtime_verification=False):
        super().__init__()
        self._tk_owner_thread=threading.get_ident()
        self._tk_teardown_complete=False
        self.title('AI Music Processor — 本地音乐工作台')
        self.geometry('1000x760')
        self.minsize(880,660)
        self.events=queue.Queue()
        self.runtime_gate=None
        self.runtime_check_running=False
        self.runtime_action_buttons=[]
        if defer_runtime_verification:
            from core.runtime_startup import RuntimeGate
            self.runtime_gate=RuntimeGate()
        self._background_workers=set()
        self._background_workers_lock=threading.Lock()
        self._closing=False
        self.control=Control()
        self.is_running=False
        self.processing_started_at=None
        self.progress_stage=None
        self.progress_stage_started_at=None
        self.progress_stage_message=''
        self.voice_range_running=False
        self.voice_range_button=None
        self.waveform_running=False
        self.waveform_token=0
        self.waveform_window=None
        self.waveform_canvas=None
        self.last_manifest=None
        self.settings=load_settings()
        from core.session_actions import CompletionActions
        self.completion_actions=CompletionActions()
        self._completion_batch_ids=()
        self._completion_dispatch_nonce=None
        self._completion_statuses={}
        self._completion_tracking_to_key={}
        self._sleep_guard_requested=False
        self.models=get_available_rvc_models()
        self.config_warnings=get_config_warnings()
        self.result_manifests={}
        self.pending_paths=set()
        self.scan_cancel=threading.Event()
        self.scan_thread=None
        self.component_install_running=False
        self.component_install_button=None
        self.component_size_running=False
        self.component_size_cache=None
        self.component_size_token=0
        self.component_size_button=None
        self.component_size_tree=None
        self.component_size_status=None
        self.model_import_running=False
        self.model_import_button=None
        self.model_inspection_running=False
        self.model_inspection_pending=set()
        self.model_inspection_cache={}
        self.model_inspection_reports={}
        self.cache_stats_token=0
        self.cache_cleanup_token=0
        self.queue_dirty=False
        cache_policy=self.settings.get('cache_policy',{})
        self.cache_enabled=tk.BooleanVar(master=self,value=cache_policy.get('enabled',True))
        self.cache_quota=tk.StringVar(master=self,value='' if cache_policy.get('quota_bytes',20*1024**3) is None else
                                       str(round(float(cache_policy.get('quota_bytes',20*1024**3))/1024**3,2)))
        self.cache_ttl=tk.StringVar(master=self,value='' if cache_policy.get('ttl_days',30) is None else
                                     str(cache_policy.get('ttl_days',30)))
        self.cache_preview_paths=[]
        if self.runtime_gate is not None:
            from core.runtime_startup import DeferredPipeline
            from core.storage import JobStore
            self.pipeline=DeferredPipeline(self.runtime_gate,JobStore(),
                log_callback=lambda msg:self.events.put(('log',msg)),
                progress_callback=lambda s,p,m:self.events.put(('progress',(s,p,m))))
        else:
            self.pipeline=AudioPipeline(log_callback=lambda msg:self.events.put(('log',msg)),
                                        progress_callback=lambda s,p,m:self.events.put(('progress',(s,p,m))))
        self.mode=tk.StringVar(master=self,value=self.settings.get('mode','polish'))
        execution=self.settings.get('execution_policy') or {}
        self.device_choice=tk.StringVar(master=self,value=self.settings.get('device',execution.get('device','auto')))
        self.performance_profile=tk.StringVar(master=self,value=self.settings.get('performance_profile',execution.get('performance_profile','balanced')))
        self.postprocess_profile=tk.StringVar(master=self,value=self.settings.get('rvc_postprocess_profile','off'))
        self.adaptive_mix=tk.BooleanVar(master=self,value=bool(self.settings.get('adaptive_mix',False)))
        self.mix_bus_profile=tk.StringVar(master=self,value=self.settings.get('mix_bus_profile','off'))
        self.external_editor_choice=tk.StringVar(master=self,value='lmms')
        self.candidate_tta=tk.BooleanVar(master=self,value=False)
        self.input_channel_mode=tk.StringVar(master=self,value=self.settings.get('input_channel_mode','auto'))
        self.output_channels=tk.StringVar(master=self,value=str(self.settings.get('output_channels',2)))
        self.detailed_analysis=tk.BooleanVar(master=self,value=self.settings.get('detailed_analysis',True))
        self.mix_vocal_gain=tk.StringVar(master=self,value=str(self.settings.get('mix_vocal_gain_db',.5)))
        self.mix_instrumental_gain=tk.StringVar(master=self,value=str(self.settings.get('mix_instrumental_gain_db',-1)))
        self.transcribe_stem=tk.StringVar(master=self,value=self.settings.get('transcribe_stem','vocal'))
        self.clean=tk.StringVar(master=self,value=self.settings.get('clean_preset','light'))
        self.master_preset_var=tk.StringVar(master=self,value=self.settings.get('master_preset','natural'))
        self.model=tk.StringVar(master=self,value=self.settings.get('rvc_model_key',next(iter(self.models),'')))
        self.pitch=tk.StringVar(master=self,value=str(self.settings.get('pitch_shift',0)))
        self.f0=tk.StringVar(master=self,value=self.settings.get('f0_method','harvest'))
        self.index=tk.StringVar(master=self,value=str(self.settings.get('index_rate',.65)))
        self.bpm=tk.StringVar(master=self,value=str(self.settings.get('bpm',120)))
        self.sr=tk.StringVar(master=self,value=str(self.settings.get('output_sample_rate',48000)))
        self.midi=tk.BooleanVar(master=self,value=self.settings.get('generate_midi',False))
        midi_cleanup=self.settings.get('midi_cleanup',{})
        self.midi_cleanup_enabled=tk.BooleanVar(master=self,value=bool(midi_cleanup.get('enabled',False)) if isinstance(midi_cleanup,dict) else False)
        self.midi_cleanup_quantize=tk.BooleanVar(master=self,value=bool(midi_cleanup.get('quantize',False)) if isinstance(midi_cleanup,dict) else False)
        self.midi_cleanup_merge=tk.BooleanVar(master=self,value=bool(midi_cleanup.get('merge_overlaps',False)) if isinstance(midi_cleanup,dict) else False)
        self.mix_ducking=tk.BooleanVar(master=self,value=bool(self.settings.get('mix_ducking',False)))
        self.quality_assist=tk.BooleanVar(master=self,value=self.settings.get('quality_assist',False))
        self.parameter_search_enabled=tk.BooleanVar(master=self,value=self.settings.get('parameter_search_enabled',False))
        self.parameter_search_budget=tk.StringVar(master=self,value=self.settings.get('parameter_search_budget','fast'))
        self.vocal_restoration_effect=tk.StringVar(master=self,value=self.settings.get('vocal_restoration_effect','off'))
        restore_profile=self.settings.get('vocal_restore_profile',{'mode':'auto'})
        restore_profile=restore_profile if isinstance(restore_profile,dict) else {'mode':'auto'}
        self.vocal_restore_mode=tk.StringVar(master=self,value=restore_profile.get('mode','auto'))
        self.vocal_restore_hpf=tk.BooleanVar(master=self,value=restore_profile.get('highpass_enabled',False))
        self.vocal_restore_hpf_hz=tk.StringVar(master=self,value=str(restore_profile.get('highpass_hz',60)))
        self.vocal_restore_deesser=tk.BooleanVar(master=self,value=restore_profile.get('deesser_enabled',False))
        self.vocal_restore_deesser_intensity=tk.StringVar(master=self,value=str(restore_profile.get('deesser_intensity',.08)))
        self.vocal_restore_deesser_reduction=tk.StringVar(master=self,value=str(restore_profile.get('deesser_max_reduction',.15)))
        self.vocal_restore_normalize=tk.BooleanVar(master=self,value=restore_profile.get('normalize_enabled',False))
        self.vocal_restore_peak=tk.StringVar(master=self,value=str(restore_profile.get('normalize_peak',.8)))
        self.pitch_correction_mode=tk.StringVar(master=self,value=self.settings.get('pitch_correction_mode','off'))
        self.pitch_correction_key=tk.StringVar(master=self,value=self.settings.get('pitch_correction_key') or 'auto')
        self.source_hint=tk.StringVar(master=self,value=self.settings.get('source_hint','auto'))
        self.source_preclean_enabled=tk.BooleanVar(master=self,value=self.settings.get('source_preclean_enabled',False))
        self.prevent_sleep_var=tk.BooleanVar(master=self,value=self.settings.get('prevent_sleep',False))
        action_value=self.settings.get('completion_action','none')
        self.completion_action_var=tk.StringVar(master=self,value=action_value if action_value in ('none','exit','sleep','shutdown') else 'none')
        self.notify_in_app_var=tk.BooleanVar(master=self,value=self.settings.get('notify_in_app',True))
        self.notify_sound_var=tk.BooleanVar(master=self,value=self.settings.get('notify_sound',False))
        self.notify_toast_var=tk.BooleanVar(master=self,value=self.settings.get('notify_toast',False))
        self.mp3=tk.BooleanVar(master=self,value='mp3' in self.settings.get('output_formats',['wav','mp3']))
        self.flac=tk.BooleanVar(master=self,value='flac' in self.settings.get('output_formats',[]))
        self.output=tk.StringVar(master=self,value=self.settings.get('custom_output_dir',OUTPUT_DIR))
        from core.polish_policy import STRENGTH_LABELS
        stored_strength=self.settings.get('polish_profile','balanced')
        self.polish_strength=tk.StringVar(master=self,value=STRENGTH_LABELS.get(stored_strength,'推荐'))
        self.polish_reference=tk.StringVar(master=self,value=self.settings.get('polish_reference') or '')
        self.polish_tier=tk.StringVar(master=self,value='基础')
        self.polish_declick=tk.BooleanVar(master=self,value=True)
        self.polish_declip=tk.BooleanVar(master=self,value=True)
        self.polish_denoise=tk.BooleanVar(master=self,value=True)
        self.polish_auto_eq=tk.BooleanVar(master=self,value=True)
        self.polish_dynamic_eq=tk.BooleanVar(master=self,value=True)
        self.polish_reference_match=tk.BooleanVar(master=self,value=True)
        self.polish_multiband=tk.BooleanVar(master=self,value=True)
        self.polish_transient=tk.BooleanVar(master=self,value=True)
        self.polish_stereo=tk.BooleanVar(master=self,value=True)
        self.polish_expert={name:tk.StringVar(master=self,value='') for name in (
            'threshold','ratio','attack','release','Q','frequency','max correction',
            'band crossover','correlation guard','noise threshold','debug trace')}
        self.pending={}
        self.active_key=None
        self.wizard_window=None
        self.wizard_frame=None
        self.tutorial_window=None
        self.tutorial_frame=None
        self.tutorial_session=None
        self.mode_buttons={}
        self.build_ui()
        if self.runtime_gate is not None:
            guarded_labels={'开始处理','运行诊断','分析音域','波形预览','同起点 A/B','独立分离候选实验',
                            '20 秒音频真实整链基准（当前模式）','试听原曲','试听母带',
                            'Audacity','NeuralNote','MuseScore','LMMS'}
            stack=[self]
            while stack:
                widget=stack.pop();stack.extend(widget.winfo_children())
                if isinstance(widget,ttk.Button) and widget.cget('text') in guarded_labels:
                    if widget not in self.runtime_action_buttons:self.runtime_action_buttons.append(widget)
                    widget.state(['disabled'])
        self.result_surface='queue'
        self.tree.bind('<<TreeviewSelect>>',lambda event:setattr(self,'result_surface','queue'))
        self.tree.bind('<<TreeviewSelect>>',self.preview_pending_source,add='+')
        self.tree.bind('<<TreeviewSelect>>',self.on_select_result,add='+')
        self.tree.bind('<Double-Button-1>',lambda e:self.show_qc_details())
        self.history_tree.bind('<<TreeviewSelect>>',lambda event:setattr(self,'result_surface','history'))
        self.history_tree.bind('<<TreeviewSelect>>',self.on_select_result,add='+')
        self.history_tree.bind('<Double-Button-1>',lambda e:self.show_qc_details())
        self.load_queue()
        self.refresh_history()
        for warning in self.config_warnings:
            self.log('配置警告：'+warning)
        self.after(100,self.poll_events)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.bind_all('<Control-KeyPress>',self._handle_quick_action)
        self.update_idletasks()
        if os.name=='nt':
            try:
                hook_drop_native(self,self.events)
            except Exception as exc:
                self.log('拖拽初始化失败，仍可选择文件：'+str(exc))
        if self.runtime_gate is not None:
            self.after(0,self.begin_runtime_check)
        else:
            self.after(0,self._maybe_open_onboarding)

    def build_ui(self):
        startup=ttk.Frame(self,padding=(14,7))
        startup.pack(fill='x')
        self.runtime_status=ttk.Label(startup,text='运行环境：已就绪' if self.runtime_gate is None else '运行环境：等待后台校验，可先设置参数')
        self.runtime_status.pack(side='left',fill='x',expand=True)
        self.runtime_progress=ttk.Progressbar(startup,mode='indeterminate',length=130)
        if self.runtime_gate is not None:
            self.runtime_progress.pack(side='left',padx=6)
        self.runtime_retry_button=ttk.Button(startup,text='重新校验',command=self.begin_runtime_check)
        self.runtime_retry_button.pack(side='right')
        notebook=ttk.Notebook(self)
        self.notebook=notebook
        notebook.pack(fill='both',expand=True,padx=14,pady=12)
        home_shell=ttk.Frame(notebook)
        self.home_canvas=tk.Canvas(home_shell,highlightthickness=0,borderwidth=0)
        home_scroll=ttk.Scrollbar(home_shell,orient='vertical',command=self.home_canvas.yview)
        self.home_canvas.configure(yscrollcommand=home_scroll.set)
        home_scroll.pack(side='right',fill='y');self.home_canvas.pack(side='left',fill='both',expand=True)
        home=ttk.Frame(self.home_canvas,padding=12)
        home_item=self.home_canvas.create_window((0,0),window=home,anchor='nw')
        home.bind('<Configure>',lambda event:self.home_canvas.configure(scrollregion=self.home_canvas.bbox('all')))
        def _resize_home(event):
            if getattr(self, '_last_home_canvas_w', None) != event.width:
                self._last_home_canvas_w = event.width
                self.home_canvas.itemconfigure(home_item,width=event.width)
        self.home_canvas.bind('<Configure>',_resize_home)
        def home_wheel(event):
            class_query=getattr(event.widget,'winfo_class',None)
            if class_query is None:return
            try:
                if class_query() in ('TCombobox','Text','Treeview','Listbox'):return
            except tk.TclError:return
            current=event.widget
            while current is not None:
                if current==home:
                    self.home_canvas.yview_scroll(-int(event.delta/120),'units');return 'break'
                if current is self.__dict__.get('tools_content'):
                    self.tools_canvas.yview_scroll(-int(event.delta/120),'units');return 'break'
                current=getattr(current,'master',None)
        self.bind_all('<MouseWheel>',home_wheel,add='+')
        history=ttk.Frame(notebook,padding=12)
        tools_shell=ttk.Frame(notebook)
        self.tools_canvas=tk.Canvas(tools_shell,highlightthickness=0)
        tools_scroll=ttk.Scrollbar(tools_shell,orient='vertical',command=self.tools_canvas.yview)
        self.tools_canvas.configure(yscrollcommand=tools_scroll.set)
        tools_scroll.pack(side='right',fill='y');self.tools_canvas.pack(side='left',fill='both',expand=True)
        tools=ttk.Frame(self.tools_canvas,padding=12)
        self.tools_content=tools
        tools_item=self.tools_canvas.create_window((0,0),window=tools,anchor='nw')
        tools.bind('<Configure>',lambda _event:self.tools_canvas.configure(scrollregion=self.tools_canvas.bbox('all')))
        def _resize_tools(event):
            if getattr(self, '_last_tools_canvas_w', None) != event.width:
                self._last_tools_canvas_w = event.width
                self.tools_canvas.itemconfigure(tools_item,width=event.width)
        self.tools_canvas.bind('<Configure>',_resize_tools)
        models=ttk.Frame(notebook,padding=12)
        self.settings_tab=models
        notebook.add(home_shell,text='处理与队列')
        notebook.add(history,text='历史与恢复')
        notebook.add(tools_shell,text='诊断与工具')
        notebook.add(models,text='模型与预设')
        box=ttk.LabelFrame(home,text='当前模式的真实步骤',padding=10)
        box.pack(fill='x')
        row=ttk.Frame(box);row.pack(fill='x')
        self.mode_buttons={}
        for text,val in [('精修','polish'),('换声重制','voice_conversion'),('扒谱四轨 + MIDI','transcribe')]:
            button=ttk.Radiobutton(row,text=text,value=val,variable=self.mode,command=self.refresh_mode_flow)
            button.pack(side='left',padx=8)
            self.mode_buttons[val]=button
        self.mode_flow=tk.Frame(box)
        self.mode_flow.pack(fill='x',pady=(6,0))
        stored_route=self.settings.get('separator_profile')
        self.separator_route=tk.StringVar(master=self,value='hp2' if isinstance(stored_route, dict) and stored_route.get('mode')=='hp2' else 'dual')
        self.fields=[]
        def field(parent,label,var,values=None,width=15):
            caption=ttk.Label(parent,text=label)
            caption.pack(side='left',padx=(8,3))
            widget=ttk.Combobox(parent,textvariable=var,values=values,state='readonly',width=width) if values else ttk.Entry(parent,textvariable=var,width=width)
            widget.pack(side='left');self.fields.append(widget)
            return caption,widget
        row=ttk.Frame(box);row.pack(fill='x',pady=5)
        self.clean_label,self.clean_widget=field(row,'清理',self.clean,list(CLEAN_PRESETS),10)
        self.master_label=ttk.Label(box,text='母带')
        self.master_widget=ttk.Combobox(box,textvariable=self.master_preset_var,values=list(MASTER_PRESETS),state='readonly',width=12)
        self.fields.append(self.master_widget)
        self.master_label.pack(in_=row,side='left',padx=(8,3))
        self.master_widget.pack(in_=row,side='left')
        self.model_label,self.model_widget=field(row,'模型',self.model,list(self.models),20)
        self.model_widget.bind('<<ComboboxSelected>>',self.apply_profile)
        self.pitch_label,self.pitch_widget=field(row,'半音',self.pitch,width=5)
        self.voice_note=ttk.Label(box,text='轻度为默认；换声时按实际音域调整半音，先用短片段试听。扒谱结果需人工修谱。')
        self.voice_note.pack(anchor='w')
        execution_row=ttk.Frame(box);execution_row.pack(fill='x',pady=3)
        field(execution_row,'推理设备',self.device_choice,['auto','cpu','cuda'],9)
        field(execution_row,'资源档',self.performance_profile,['compatibility','balanced','quality'],14)
        ttk.Label(execution_row,text='纯 DSP 仍用 CPU；quality 增加计算量，听感需试听').pack(side='left',padx=8)
        channels_row=ttk.Frame(box);channels_row.pack(fill='x',pady=3)
        field(channels_row,'输入处理',self.input_channel_mode,['auto','mono','stereo'],8)
        field(channels_row,'导出声道数',self.output_channels,['1','2'],4)
        ttk.Checkbutton(channels_row,text='估计 BPM/调性（必要安全分析始终执行）',variable=self.detailed_analysis).pack(side='left',padx=6)
        advanced=ttk.LabelFrame(box,text='高级设置',padding=4)
        self.voice_advanced=advanced
        advanced.pack(fill='x',pady=5)
        transcribe_options=ttk.Frame(box);transcribe_options.pack(fill='x',pady=3)
        field(transcribe_options,'扒谱 / 生成 MIDI 使用轨',self.transcribe_stem,['vocal','bass','other'],8)
        ttk.Label(transcribe_options,text='（旋律草稿/需人工修谱，不声称自动还原整曲总谱）').pack(side='left',padx=6)
        stem_row=ttk.Frame(advanced);stem_row.pack(fill='x',pady=3)
        ttk.Label(stem_row,text='混音：人声 / 伴奏 dB（-6～+6）').pack(side='left',padx=5)
        ttk.Entry(stem_row,textvariable=self.mix_vocal_gain,width=6).pack(side='left')
        ttk.Entry(stem_row,textvariable=self.mix_instrumental_gain,width=6).pack(side='left',padx=4)
        dsp_row=ttk.Frame(advanced);dsp_row.pack(fill='x')
        field(dsp_row,'换声后轻修',self.postprocess_profile,['off','gentle'],8)
        field(dsp_row,'总线轻修',self.mix_bus_profile,['off','gentle'],8)
        ttk.Checkbutton(dsp_row,text='按实测响度自适应混音（默认关）',variable=self.adaptive_mix).pack(side='left',padx=6)
        row=ttk.Frame(advanced);row.pack(fill='x')
        _,_=field(row,'F0',self.f0,['harvest','pm','crepe','rmvpe'],9)
        ttk.Label(advanced,text='F0 支持 harvest、pm、crepe 与 RMVPE 适配器；缺权重时会在任务快照中记录 harvest 回退。新换声任务默认双模型 CPU ONNX 分离，可显式选择 HP2。听审未做。').pack(anchor='w')
        route=ttk.Frame(advanced);route.pack(fill='x')
        ttk.Radiobutton(route,text='分离：双模型 Kim Vocal 2 人声 + Inst HQ 3 伴奏（CPU ONNX）',value='dual',variable=self.separator_route).pack(side='left',padx=4)
        ttk.Radiobutton(route,text='分离：快速 HP2',value='hp2',variable=self.separator_route).pack(side='left',padx=4)
        ttk.Checkbutton(advanced,text='音质辅助（默认关；仅削波或 F0 不稳时最多再推理一次，失败保留第一次）',variable=self.quality_assist).pack(anchor='w')
        search_row=ttk.Frame(advanced);search_row.pack(fill='x')
        ttk.Checkbutton(search_row,text='参数候选搜索（默认关；只生成独立候选，不替换基线）',variable=self.parameter_search_enabled,command=self.refresh_mode_flow).pack(side='left')
        ttk.Label(search_row,text='预算').pack(side='left',padx=(8,3))
        ttk.Combobox(search_row,textvariable=self.parameter_search_budget,values=['fast','balanced','quality'],state='readonly',width=10).pack(side='left')
        restore_profile_row=ttk.Frame(advanced);restore_profile_row.pack(fill='x',pady=3)
        ttk.Label(restore_profile_row,text='VocalRestore 模式').pack(side='left')
        self.vocal_restore_mode_widget=ttk.Combobox(restore_profile_row,textvariable=self.vocal_restore_mode,
                    values=['off','auto','light','strong','expert'],state='readonly',width=10)
        self.vocal_restore_mode_widget.pack(side='left',padx=6)
        self.vocal_restore_mode_widget.bind('<<ComboboxSelected>>',lambda _event:self.refresh_mode_flow())
        expert_restore=ttk.Frame(advanced);expert_restore.pack(fill='x',pady=2)
        ttk.Label(expert_restore,text='Expert effects').pack(side='left',padx=(4,3))
        ttk.Checkbutton(expert_restore,text='HPF Hz',variable=self.vocal_restore_hpf).pack(side='left')
        ttk.Entry(expert_restore,textvariable=self.vocal_restore_hpf_hz,width=5).pack(side='left',padx=(0,5))
        ttk.Checkbutton(expert_restore,text='Deesser i/m',variable=self.vocal_restore_deesser).pack(side='left')
        ttk.Entry(expert_restore,textvariable=self.vocal_restore_deesser_intensity,width=5).pack(side='left',padx=(0,2))
        ttk.Entry(expert_restore,textvariable=self.vocal_restore_deesser_reduction,width=5).pack(side='left',padx=(0,5))
        ttk.Checkbutton(expert_restore,text='衰减峰值',variable=self.vocal_restore_normalize).pack(side='left')
        ttk.Entry(expert_restore,textvariable=self.vocal_restore_peak,width=5).pack(side='left')
        ttk.Label(advanced,text='Auto：20–60 Hz 能量占比 >0.15 时启用 60 Hz 高通；峰值 >0.95 时仅衰减至 0.8；自动齿音检测关闭。').pack(anchor='w')
        restoration_row=ttk.Frame(advanced);restoration_row.pack(fill='x',pady=3)
        ttk.Label(restoration_row,text='VocalRestore 外部权重效果').pack(side='left')
        try:
            from core.vocal_restoration import restoration_catalog
            restoration_choices=['off']+[row['id'] for row in restoration_catalog()['effects']
                if row.get('available_by_job')]
        except Exception:
            restoration_choices=['off']
        if self.vocal_restoration_effect.get() not in restoration_choices:
            self.vocal_restoration_effect.set('off')
        self.restoration_widget=ttk.Combobox(restoration_row,textvariable=self.vocal_restoration_effect,
                                             values=restoration_choices,state='readonly',width=24)
        self.restoration_widget.pack(side='left',padx=6)
        self.restoration_widget.bind('<<ComboboxSelected>>',lambda _event:self.refresh_mode_flow())
        correction_row=ttk.Frame(advanced);correction_row.pack(fill='x',pady=3)
        ttk.Label(correction_row,text='PitchCorrection（默认关；仅稳定明显偏音校正）').pack(side='left')
        self.pitch_correction_widget=ttk.Combobox(correction_row,textvariable=self.pitch_correction_mode,
                     values=['off','light','medium','strong'],state='readonly',width=10)
        self.pitch_correction_widget.pack(side='left',padx=6)
        self.pitch_correction_widget.bind('<<ComboboxSelected>>',lambda _event:self.refresh_mode_flow())
        correction_keys=['auto']+[tonic+' '+kind for tonic in
            ('C','C#','D','D#','E','F','F#','G','G#','A','A#','B') for kind in ('major','minor')]
        ttk.Label(correction_row,text='调性').pack(side='left',padx=(8,3))
        ttk.Combobox(correction_row,textvariable=self.pitch_correction_key,
                     values=correction_keys,state='readonly',width=12).pack(side='left')
        ttk.Label(advanced,text='新换声任务会按每首歌清理后的人声峰值计算 RVC 输入余量；旧任务仍按原冻结增益恢复。').pack(anchor='w')
        source_row=ttk.Frame(box);source_row.pack(fill='x',pady=3)
        ttk.Label(source_row,text='来源提示').pack(side='left',padx=(8,3))
        ttk.Combobox(source_row,textvariable=self.source_hint,
                     values=['auto','suno','udio','studio_master','mp3','youtube','live','vocal_only'],
                     state='readonly',width=16).pack(side='left')
        ttk.Checkbutton(source_row,text='启用有界源预处理：仅测得明显 DC 且有余量时修正；削波/高频滚降只提示',
                        variable=self.source_preclean_enabled,command=self.refresh_mode_flow).pack(side='left',padx=8)
        _,_=field(row,'Index',self.index,width=5)
        _,_=field(row,'MIDI BPM',self.bpm,width=5)
        _,_=field(row,'WAV 24-bit Hz',self.sr,['44100','48000'],7)
        ttk.Checkbutton(row,text='换声同时扒谱',variable=self.midi,command=self.refresh_mode_flow).pack(side='left',padx=8)
        midi_cleanup_row=ttk.Frame(box);midi_cleanup_row.pack(fill='x',pady=2)
        ttk.Checkbutton(midi_cleanup_row,text='扒谱后执行安全 MIDI 清理',variable=self.midi_cleanup_enabled,
                        command=self.refresh_mode_flow).pack(side='left',padx=8)
        ttk.Checkbutton(midi_cleanup_row,text='量化到 1/4 拍网格',variable=self.midi_cleanup_quantize).pack(side='left',padx=4)
        ttk.Checkbutton(midi_cleanup_row,text='合并同音重叠',variable=self.midi_cleanup_merge).pack(side='left',padx=4)
        ttk.Checkbutton(box,text='换声时按人声侧链轻压伴奏（Ducking，默认关闭）',
                        variable=self.mix_ducking,command=self.refresh_mode_flow).pack(anchor='w',padx=8,pady=2)
        self.polish_row=ttk.Frame(box)
        self.polish_row.pack(fill='x',pady=4)
        ttk.Label(self.polish_row,text='精修强度').pack(side='left',padx=(8,3))
        ttk.Combobox(self.polish_row,textvariable=self.polish_strength,values=['轻柔','推荐','加强'],state='readonly',width=8).pack(side='left')
        ttk.Label(self.polish_row,text='参考曲').pack(side='left',padx=(12,3))
        ttk.Entry(self.polish_row,textvariable=self.polish_reference,width=28).pack(side='left')
        ttk.Button(self.polish_row,text='选择参考曲',command=self.choose_polish_reference).pack(side='left',padx=4)
        ttk.Label(self.polish_row,text='界面').pack(side='left',padx=(12,3))
        ttk.Combobox(self.polish_row,textvariable=self.polish_tier,values=['基础','高级','专家'],state='readonly',width=8).pack(side='left')
        self.polish_advanced=ttk.LabelFrame(box,text='精修高级',padding=4)
        advanced_row=ttk.Frame(self.polish_advanced);advanced_row.pack(fill='x')
        for label,var in (('DeClick',self.polish_declick),('DeClip',self.polish_declip),('Denoise',self.polish_denoise),
                          ('Auto EQ',self.polish_auto_eq),('Dynamic EQ',self.polish_dynamic_eq),('Reference Match',self.polish_reference_match),
                          ('Multiband',self.polish_multiband),('Transient',self.polish_transient),('Stereo',self.polish_stereo)):
            ttk.Checkbutton(advanced_row,text=label,variable=var).pack(side='left',padx=3)
        self.polish_level_row=ttk.Frame(self.polish_advanced);self.polish_level_row.pack(fill='x',pady=3)
        ttk.Label(self.polish_level_row,text='Target LUFS / True Peak').pack(side='left',padx=(4,6))
        self.polish_expert_frame=ttk.LabelFrame(box,text='精修专家',padding=4)
        expert_row=ttk.Frame(self.polish_expert_frame);expert_row.pack(fill='x')
        for name,var in self.polish_expert.items():
            ttk.Label(expert_row,text=name).pack(side='left',padx=(6,2))
            ttk.Entry(expert_row,textvariable=var,width=6).pack(side='left')
        self.polish_tier.trace_add('write',lambda *_:self._apply_polish_visibility())
        row=ttk.Frame(box);row.pack(fill='x')
        ttk.Label(row,text='导出：WAV（必选）').pack(side='left')
        ttk.Checkbutton(row,text='MP3 320k',variable=self.mp3).pack(side='left',padx=8)
        ttk.Checkbutton(row,text='FLAC',variable=self.flac).pack(side='left')
        self.output_button=ttk.Button(row,text='输出目录',command=self.choose_output)
        self.output_button.pack(side='left',padx=8)
        ttk.Label(row,textvariable=self.output).pack(side='left')
        bar=ttk.Frame(home);bar.pack(fill='x',pady=8)
        for text,cmd in [('添加音频',self.choose_file),('添加文件夹',self.choose_folder),
                         ('开始处理',self.start),('暂停 / 继续',self.pause),('取消',self.cancel),
                         ('移除待处理',self.remove_pending)]:
            action=ttk.Button(bar,text=text,command=cmd)
            action.pack(side='left',padx=3)
            if text=='开始处理':self.runtime_action_buttons.append(action)
        self.tree=ttk.Treeview(home,columns=('file','state','mode'),show='headings',height=5)
        self.source_preview_label=ttk.Label(home,text='选择待处理文件后异步分析时长、声道、电平与 BPM / 调性',wraplength=800)
        self.source_preview_label.pack(fill='x',pady=4)
        for key,title,width in [('file','文件 / 可拖入整个文件夹',480),('state','状态',140),('mode','模式',160)]:
            self.tree.heading(key,text=title);self.tree.column(key,width=width)
        self.tree.pack(fill='x')
        self.status=ttk.Label(home,text='添加音频后点击开始；处理中的参数修改仅影响后续新任务')
        self.status.pack(anchor='w',pady=5)
        self.progress=ttk.Progressbar(home,maximum=100);self.progress.pack(fill='x')
        self.logbox=tk.Text(home,height=6,wrap='word');self.logbox.pack(fill='both',expand=True,pady=6)
        row=ttk.Frame(home);row.pack(fill='x')
        self.play_master_button=None
        for label,cmd in [('打开项目',self.open_output),('查看日志',self.show_logs_dialog),('试听原曲',lambda:self.play('source')),
                          ('试听母带',lambda:self.play('master')),('同起点 A/B',self.show_ab_preview),('换声质量诊断',self.show_stage_trace),
                          ('质检详情',self.show_qc_details),('查看报告',self.open_report),
                          ('分析音域',self.analyze_selected_voice_range),
                          ('波形预览',self.preview_selected_waveform),
                          ('Audacity',lambda:self.editor('audacity')),
                          ('NeuralNote',lambda:self.editor('neuralnote')),('MuseScore',lambda:self.editor('musescore')),
                          ('LMMS',lambda:self.editor('lmms'))]:
            button=ttk.Button(row,text=label,command=cmd)
            button.pack(side='left',padx=3)
            if label=='试听母带':
                self.play_master_button=button
            if label=='分析音域':
                self.voice_range_button=button
        wrap_button_row(row)
        self.history_tree=ttk.Treeview(history,columns=('file','mode','status','stage'),show='headings')
        for key,title in [('file','歌曲'),('mode','模式'),('status','状态'),('stage','步骤')]:
            self.history_tree.heading(key,text=title)
        self.history_tree.pack(fill='both',expand=True)
        row=ttk.Frame(history);row.pack(fill='x',pady=8)
        for text,cmd in [('刷新',self.refresh_history),('继续 / 重试',self.resume_history),
                         ('新建重跑',self.rerun_history),('按当前设置重跑',self.rerun_history_current),
                         ('打开目录',self.open_history),('质检详情',self.show_qc_details),
                         ('查看报告',self.open_report),('复制设置',self.copy_history),
                         ('删除记录',self.hide_history),('显示已删除 / 恢复',self.toggle_deleted_history)]:
            ttk.Button(row,text=text,command=cmd).pack(side='left',padx=4)
        ttk.Label(history,text='新建重跑严格沿用历史快照；“按当前设置重跑”会明确使用当前界面参数。').pack(anchor='w')
        ttk.Button(tools,text='运行诊断',command=self.diagnose).pack(anchor='w')
        ttk.Button(tools,text='20 秒音频真实整链基准（当前模式）',command=self.run_benchmark).pack(anchor='w',pady=4)
        ttk.Button(tools,text='取消基准',command=self.cancel_benchmark).pack(anchor='w')
        ttk.Button(tools,text='组件与首次向导',command=self.show_platform_wizard).pack(anchor='w',pady=4)
        self.component_install_button=ttk.Button(tools,text='从同版本离线目录安装/修复',command=self.import_offline_components)
        self.component_install_button.pack(anchor='w',pady=4)
        self.component_package_install_button=ttk.Button(tools,text='导入 .ampkg 离线组件包',command=self.import_component_package)
        self.component_package_install_button.pack(anchor='w',pady=4)
        setup=ttk.LabelFrame(tools,text='组件安装管理（轻量发布 / 按需安装）',padding=6)
        setup.pack(fill='x',pady=5)
        self.component_setup_choice=tk.StringVar(master=self,value='polish')
        ttk.Combobox(setup,textvariable=self.component_setup_choice,
            values=['polish','voice_conversion','transcribe','all','tool.audacity','tool.lmms','tool.musescore','tool.neuralnote'],
            state='readonly',width=22).pack(side='left')
        ttk.Button(setup,text='查看组件与下载源',command=self.show_component_install_plan).pack(side='left',padx=4)
        self.component_online_install_button=ttk.Button(setup,text='打开安装器',command=self.start_component_installer)
        self.component_online_install_button.pack(side='left',padx=4)
        self.component_status=ttk.Label(tools,text='模式状态：尚未刷新')
        self.component_status.pack(anchor='w',pady=4)
        size_box=ttk.LabelFrame(tools,text='组件体积（安装 / 下载 / 缓存）',padding=6)
        size_box.pack(fill='x',pady=5)
        self.component_size_tree=ttk.Treeview(
            size_box,columns=('component','install','download','cache'),show='headings',height=6)
        for key,title,width in [('component','组件',290),('install','安装（字节）',180),
                                ('download','下载（字节）',160),('cache','缓存（字节）',160)]:
            self.component_size_tree.heading(key,text=title)
            self.component_size_tree.column(key,width=width,stretch=key=='component',anchor='w')
        size_scroll=ttk.Scrollbar(size_box,orient='vertical',command=self.component_size_tree.yview)
        self.component_size_tree.configure(yscrollcommand=size_scroll.set)
        self.component_size_tree.pack(side='left',fill='x',expand=True)
        size_scroll.pack(side='right',fill='y')
        size_actions=ttk.Frame(tools);size_actions.pack(fill='x',pady=(0,3))
        self.component_size_button=ttk.Button(size_actions,text='测量组件体积',command=self.measure_component_sizes)
        self.component_size_button.pack(side='left')
        self.component_size_status=ttk.Label(size_actions,text='未测量；下载源和独立缓存目录未配置时显示“未知”')
        self.component_size_status.pack(side='left',padx=8)
        ttk.Button(tools,text='新手教程',command=self.show_tutorial).pack(anchor='w')
        backup_row=ttk.Frame(tools);backup_row.pack(fill='x',pady=3)
        ttk.Button(backup_row,text='导出配置备份',command=self.export_configuration_backup).pack(side='left')
        ttk.Button(backup_row,text='预览并恢复配置备份',command=self.import_configuration_backup).pack(side='left',padx=6)
        location_row=ttk.Frame(tools);location_row.pack(fill='x',pady=3)
        ttk.Combobox(location_row,textvariable=self.external_editor_choice,
                     values=['audacity','musescore','lmms','neuralnote'],state='readonly',width=14).pack(side='left')
        ttk.Button(location_row,text='选择外部编辑器 EXE',command=self.choose_external_editor).pack(side='left',padx=6)
        ttk.Button(location_row,text='安装完整包到新位置',command=self.install_into_new_location).pack(side='left')
        version_row=ttk.Frame(tools);version_row.pack(fill='x',pady=3)
        ttk.Button(version_row,text='安装并启用新版本完整包',command=self.update_application_version).pack(side='left')
        ttk.Button(version_row,text='验证并回退上一版本',command=self.rollback_application_version).pack(side='left',padx=6)
        ttk.Button(version_row,text='取消版本操作',command=self.cancel_version_operation).pack(side='left')
        experiment_row=ttk.Frame(tools);experiment_row.pack(fill='x',pady=3)
        ttk.Checkbutton(experiment_row,text='候选 HP2 TTA（增加计算量）',variable=self.candidate_tta).pack(side='left')
        ttk.Button(experiment_row,text='独立分离候选实验',command=self.run_separator_experiment).pack(side='left',padx=6)
        action_box=ttk.LabelFrame(tools,text='批次完成动作与通知',padding=6)
        action_box.pack(fill='x',pady=5)
        action_row=ttk.Frame(action_box);action_row.pack(fill='x')
        ttk.Label(action_row,text='任务完成后').pack(side='left')
        self.completion_action_widget=ttk.Combobox(
            action_row,textvariable=self.completion_action_var,
            values=['none','exit','sleep','shutdown'],state='readonly',width=12)
        self.completion_action_widget.pack(side='left',padx=5)
        ttk.Checkbutton(action_row,text='处理时防止系统休眠',variable=self.prevent_sleep_var).pack(side='left',padx=6)
        notify_row=ttk.Frame(action_box);notify_row.pack(fill='x')
        ttk.Checkbutton(notify_row,text='程序内提醒',variable=self.notify_in_app_var).pack(side='left')
        ttk.Checkbutton(notify_row,text='提示音',variable=self.notify_sound_var).pack(side='left',padx=6)
        ttk.Checkbutton(notify_row,text='Windows 桌面通知',variable=self.notify_toast_var).pack(side='left',padx=6)
        ttk.Label(action_box,text='退出、睡眠或关机只对本次确认的批次生效；关机由 Windows 延迟 60 秒执行。').pack(anchor='w')
        self.diagnostics=tk.Text(tools,wrap='word');self.diagnostics.pack(fill='both',expand=True,pady=8)
        ttk.Label(models,text='模型目录：models/rvc/<名称>/model.pth + 可选 model.index / profile.json').pack(anchor='w')
        self.model_tree=ttk.Treeview(models,columns=('name','availability','f0','index','license'),show='headings')
        for key,title in [('name','模型'),('availability','安全检查 / 可推理'),('f0','F0'),('index','索引'),('license','商用记录')]:
            self.model_tree.heading(key,text=title)
        self.model_tree.column('availability',width=220,stretch=True)
        self.model_tree.pack(fill='both',expand=True,pady=8)
        row=ttk.Frame(models);row.pack(fill='x')
        for text,cmd in [('刷新模型',self.refresh_models),('保存当前模型参数',self.save_profile),
                         ('打开 Profile 目录',self.open_profiles),('保存当前预设',self.save_preset),
                         ('导入预设',self.import_preset),('登记授权',self.record_license),
                         ('缓存统计',self.cache_info)]:
            ttk.Button(row,text=text,command=cmd).pack(side='left',padx=3)
        self.model_import_button=ttk.Button(row,text='安全导入用户模型',command=self.import_user_model)
        self.model_import_button.pack(side='left',padx=3)
        wrap_button_row(row)
        ttk.Label(models,text='commercial 未记录时为 Unknown；处理完成不会自动获得模型或声音授权。').pack(anchor='w',pady=8)
        cache_box=ttk.LabelFrame(models,text='缓存回收策略（仅清理已完成任务的 .work）',padding=6)
        cache_box.pack(fill='x',pady=6)
        ttk.Checkbutton(cache_box,text='启用新任务前自动回收',variable=self.cache_enabled).pack(side='left')
        ttk.Label(cache_box,text='容量 GB（空=不限）').pack(side='left',padx=(10,3))
        ttk.Entry(cache_box,textvariable=self.cache_quota,width=8).pack(side='left')
        ttk.Label(cache_box,text='TTL 天（空=不按时间）').pack(side='left',padx=(10,3))
        ttk.Entry(cache_box,textvariable=self.cache_ttl,width=8).pack(side='left')
        ttk.Button(cache_box,text='保存策略',command=self.save_cache_policy).pack(side='left',padx=6)
        ttk.Button(cache_box,text='预览并确认回收',command=self.preview_cache_cleanup).pack(side='left')
        self.refresh_mode_flow()
        self._apply_polish_visibility()
        self.refresh_models()
        self._refresh_component_mutation_controls()

    def open_settings(self):
        """Ctrl+, opens the tab containing saved profiles and app settings."""
        notebook=self.__dict__.get('notebook')
        target=self.__dict__.get('settings_tab')
        if notebook is not None and target is not None:
            notebook.select(target)

    def _handle_quick_action(self,event):
        key=str(getattr(event,'keysym','')).lower()
        if key == 'o':
            self.choose_file()
        elif key in ('return','kp_enter'):
            self.start()
        elif key == 'comma':
            self.open_settings()
        else:
            return None
        return 'break'

    def log(self,msg):
        self.logbox.insert('end',str(msg)+'\n');self.logbox.see('end')

    def _start_background_worker(self,target,args=(),*,daemon=True,name=None):
        thread=threading.Thread(target=target,args=args,daemon=daemon,name=name)
        with self._background_workers_lock:
            self._background_workers.add(thread)
        try:
            thread.start()
        except Exception:
            with self._background_workers_lock:
                self._background_workers.discard(thread)
            raise
        return thread

    def _active_background_workers(self):
        with self._background_workers_lock:
            active={thread for thread in self._background_workers if thread.is_alive()}
            self._background_workers=active
        return tuple(active)

    def _dispose_tk_variables(self):
        """Release every Tk variable while its interpreter is still on this thread."""
        dispose_tk_variables(self)

    def parameters(self):
        params=dict(mode=self.mode.get(),clean_preset=self.clean.get(),master_preset=self.master_preset_var.get(),
                    device=self.device_choice.get(),performance_profile=self.performance_profile.get(),
                    input_channel_mode=self.input_channel_mode.get(),output_channels=int(self.output_channels.get()),
                    detailed_analysis=bool(self.detailed_analysis.get()),
                    rvc_model_key=self.model.get(),pitch_shift=int(self.pitch.get()),f0_method=self.f0.get(),
                    index_rate=float(self.index.get()),bpm=float(self.bpm.get()),
                    generate_midi=self.midi.get(),output_sample_rate=int(self.sr.get()),
                    output_formats=['wav']+(['mp3'] if self.mp3.get() else [])+(['flac'] if self.flac.get() else []),
                    custom_output_dir=self.output.get(), source_hint=self.source_hint.get(),
                    source_preclean_enabled=bool(self.source_preclean_enabled.get()))
        if params['mode']=='voice_conversion':
            params.update(rvc_postprocess_profile=self.postprocess_profile.get(),
                          mix_vocal_gain_db=float(self.mix_vocal_gain.get()),
                          mix_instrumental_gain_db=float(self.mix_instrumental_gain.get()),
                          adaptive_mix=bool(self.adaptive_mix.get()),mix_bus_profile=self.mix_bus_profile.get())
        if params['mode']=='transcribe' or (params['mode']=='voice_conversion' and params['generate_midi']):
            params['transcribe_stem']=self.transcribe_stem.get()
            params['midi_cleanup']={
                'enabled':bool(self.midi_cleanup_enabled.get()),
                'quantize':bool(self.midi_cleanup_quantize.get()),
                'grid_division':4,
                'merge_overlaps':bool(self.midi_cleanup_merge.get()),
                'velocity_range':None,
            }
        if self.quality_assist.get():
            params['quality_assist']=True
            params['quality_retry_limit']=1
        if params['mode']=='voice_conversion':
            params['rvc_input_headroom_strategy']='auto_peak'
            params['mix_ducking']=bool(self.mix_ducking.get())
            from core.vocal_restore_profile import normalize_profile
            restore_profile={'mode':self.vocal_restore_mode.get()}
            if restore_profile['mode']=='expert':
                restore_profile.update({
                    'highpass_enabled':bool(self.vocal_restore_hpf.get()),
                    'highpass_hz':float(self.vocal_restore_hpf_hz.get()),
                    'deesser_enabled':bool(self.vocal_restore_deesser.get()),
                    'deesser_intensity':float(self.vocal_restore_deesser_intensity.get()),
                    'deesser_max_reduction':float(self.vocal_restore_deesser_reduction.get()),
                    'normalize_enabled':bool(self.vocal_restore_normalize.get()),
                    'normalize_peak':float(self.vocal_restore_peak.get()),
                })
            params['vocal_restore_profile']=normalize_profile(restore_profile)
            params['parameter_search_enabled']=bool(self.parameter_search_enabled.get())
            params['parameter_search_budget']=self.parameter_search_budget.get()
            if self.vocal_restoration_effect.get()!='off':
                params['vocal_restoration_effect']=self.vocal_restoration_effect.get()
            params['pitch_correction_mode']=self.pitch_correction_mode.get()
            selected_key=self.pitch_correction_key.get()
            params['pitch_correction_key']=None if selected_key=='auto' else selected_key
            from core.separator_routing import dual_profile, hp2_profile
            params['separator_profile']=dual_profile() if self.separator_route.get()=='dual' else hp2_profile()
        if not -24<=params['pitch_shift']<=24 or not 0<=params['index_rate']<=1 or not 30<=params['bpm']<=300:
            raise ValueError('检查半音(-24~24)、Index(0~1)和 BPM(30~300)')
        if params['mode']=='polish':
            from core.polish_policy import PROFILE_LABELS
            label=self.polish_strength.get()
            if label not in PROFILE_LABELS:
                raise ValueError('精修强度只能是轻柔、推荐或加强')
            params['polish_profile']=PROFILE_LABELS[label]
            reference=self.polish_reference.get().strip()
            if reference:
                params['polish_reference']=reference
            if self.polish_tier.get()!='基础':
                options={}
                repair={}
                if not self.polish_declick.get():repair['declick']='off'
                if not self.polish_declip.get():repair['declip']='off'
                if repair:options['repair']=repair
                if not self.polish_denoise.get():options['denoise']={'enabled':False,'mode':'off'}
                if not self.polish_auto_eq.get():options['eq']={'enabled':False}
                if not self.polish_dynamic_eq.get():options['dynamic_eq']={'enabled':False}
                if not self.polish_reference_match.get():options['reference']={'enabled':False}
                if not self.polish_multiband.get():options['multiband']={'enabled':False}
                if not self.polish_transient.get():options['transient']={'enabled':False}
                if not self.polish_stereo.get():options['stereo']={'enabled':False}
                if self.polish_tier.get()=='专家':
                    expert={name:var.get().strip() for name,var in self.polish_expert.items() if var.get().strip()}
                    if expert:options['expert']=expert
                if options:params['polish_options']=options
        return params

    def _insert_pending_paths(self, paths):
        """Insert a deduplicated batch on the Tk thread."""
        if self.__dict__.get('cache_collect_running',False):
            self.scan_results_rejected=True
            self.status.config(text='缓存操作期间未添加扫描批次；请完成后重新扫描目录')
            return
        for raw_path in paths:
            path=str(Path(os.fsdecode(raw_path)).resolve())
            if path in self.pending_paths or not os.path.isfile(path):
                continue
            if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            item_id=self.tree.insert('', 'end', values=(Path(path).name, '等待', self.mode.get()))
            self.pending[item_id]=dict(path=path,resume=None,dispatch_id=uuid.uuid4().hex)
            self.pending_paths.add(path)
            if not self.tree.selection():self.tree.selection_set(item_id)

    def preview_pending_source(self,event=None):
        keys=self.tree.selection()
        if not keys or keys[0] not in self.pending:return
        path=self.pending[keys[0]]['path']
        self.source_preview_requested_path=path
        if self.__dict__.get('source_preview_running',False):
            if self.__dict__.get('source_preview_path')!=path:self.source_preview_control.cancel()
            return
        if self.__dict__.get('source_preview_path')==path:return
        self.source_preview_path=path
        old=self.__dict__.get('source_preview_control')
        if old is not None:old.cancel()
        control=Control();self.source_preview_control=control
        token=uuid.uuid4().hex;self.source_preview_token=token
        self.source_preview_running=True
        self.source_preview_label.config(text='正在只读分析：'+os.path.basename(path))
        def task():
            try:
                from core.source_preview import preview_source
                report=preview_source(path,TEMP_DIR,control)
                self.events.put(('source_preview',(token,report,None)))
            except Exception as exc:self.events.put(('source_preview',(token,{},exc)))
        self._start_background_worker(task,daemon=True,name='source-preview')

    def _scan_folder(self, token, folders, cancelled):
        """Walk a folder off the UI thread and deliver bounded batches."""
        batch=[]
        try:
            for folder in folders:
                for item in Path(folder).rglob('*'):
                    if cancelled.is_set() or token != getattr(self, '_scan_token', token):
                        break
                    try:
                        if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS:
                            batch.append(str(item.resolve()))
                    except OSError:
                        continue
                    if len(batch) >= 100:
                        self.events.put(('scan_batch',(token,batch)))
                        batch=[]
                if cancelled.is_set() or token != getattr(self, '_scan_token', token):
                    break
            if batch and not cancelled.is_set() and token == getattr(self, '_scan_token', token):
                self.events.put(('scan_batch',(token,batch)))
            self.events.put(('scan_done',(token,','.join(folders),None if not cancelled.is_set() else '用户取消扫描')))
        except Exception as exc:
            self.events.put(('scan_done',(token,folder,exc)))

    def add_files(self,paths):
        if self.__dict__.get('cache_collect_running',False):
            self.status.config(text='缓存回收正在核验文件引用，请完成后再添加文件')
            return
        direct=[];folders=[]
        for p in paths:
            p=os.fsdecode(p)
            if os.path.isdir(p):folders.append(p)
            else:direct.append(p)
        self._insert_pending_paths(direct)
        if folders:
            self.scan_cancel.set()
            self.scan_cancel=threading.Event()
            self._scan_token=uuid.uuid4().hex
            token=self._scan_token
            # One worker owns folder enumeration; Tk only consumes batches.
            self.scan_thread=self._start_background_worker(
                self._scan_folder,args=(token,folders,self.scan_cancel),
                daemon=True,name='music-folder-scan')
            self.status.config(text='正在后台扫描文件夹；扫描完成后再开始处理')
        self._try_save_queue()

    def choose_file(self):
        self.add_files(filedialog.askopenfilenames(filetypes=[('音频','*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.aac *.aiff *.aif')]))
    def choose_folder(self):
        path=filedialog.askdirectory()
        if path:self.add_files([path])
    def choose_output(self):
        path=filedialog.askdirectory(initialdir=self.output.get())
        if path:self.output.set(path)

    def _read_cache_policy(self):
        try:
            quota_text=self.cache_quota.get().strip()
            ttl_text=self.cache_ttl.get().strip()
            quota=None if not quota_text else int(float(quota_text)*1024**3)
            ttl=None if not ttl_text else int(ttl_text)
            if quota is not None and quota<=0:
                raise ValueError('缓存容量必须为正数，留空表示不限')
            if ttl is not None and ttl<0:
                raise ValueError('TTL 天数不能为负数，留空表示不按时间回收')
            return dict(enabled=bool(self.cache_enabled.get()),quota_bytes=quota,ttl_days=ttl)
        except (TypeError,ValueError) as exc:
            raise ValueError('缓存策略无效：'+str(exc))

    def _persist_settings(self, values=None):
        settings=dict(self.settings)
        if values:
            settings.update(values)
            if 'mode' in values and 'separator_profile' not in values:
                settings.pop('separator_profile', None)
        settings.update({
            'prevent_sleep':bool(self.prevent_sleep_var.get()),
            'completion_action':self.completion_action_var.get(),
            'notify_in_app':bool(self.notify_in_app_var.get()),
            'notify_sound':bool(self.notify_sound_var.get()),
            'notify_toast':bool(self.notify_toast_var.get()),
        })
        settings['cache_policy']=self._read_cache_policy()
        save_settings(settings)
        self.settings=settings
        return settings

    def save_cache_policy(self):
        try:self._persist_settings()
        except Exception as exc:
            messagebox.showerror('缓存策略保存失败',str(exc));return
        self.status.config(text='缓存策略已保存；新任务开始前会按此策略回收')

    def save_queue(self):
        path=os.path.join(CONFIG_DIR,'queue.json')
        with open(path+'.tmp','w',encoding='utf-8') as f:json.dump(list(self.pending.values()),f,ensure_ascii=False,indent=2)
        os.replace(path+'.tmp',path)
        self.queue_dirty=False

    def _try_save_queue(self):
        try:
            self.save_queue()
            return True
        except Exception as exc:
            self.queue_dirty=True
            self.log('队列保存失败，将继续处理并稍后重试：'+str(exc))
            self.status.config(text='队列保存失败；当前处理仍会继续，请检查配置目录权限')
            return False

    def _abort_scheduled_shutdown(self, reason):
        try:
            result=self.completion_actions.abort_scheduled_shutdown()
        except Exception as exc:
            self.log('无法取消本程序之前安排的关机（%s）：%s'%(reason,exc))
            return False
        if result.get('status')=='ABORTED':
            self.log('已取消本程序之前安排的 60 秒关机：'+reason)
            return True
        return result.get('status')=='NOT_SCHEDULED'

    def _arm_completion_for_batch(self, items):
        if not self._abort_scheduled_shutdown('开始新批次'):
            self.status.config(text='无法取消之前安排的关机；已停止新批次')
            return False
        self.completion_actions.disarm()
        batch_ids=[]
        self._completion_tracking_to_key={}
        self._completion_statuses={}
        for key,item in items:
            dispatch_id=uuid.uuid4().hex
            item['dispatch_id']=dispatch_id
            batch_ids.append(dispatch_id)
            self._completion_tracking_to_key[dispatch_id]=key
            self._completion_statuses[dispatch_id]='PENDING'
        nonce=uuid.uuid4().hex
        action=self.completion_action_var.get()
        if action not in ('none','exit','sleep','shutdown'):
            action='none'
            self.completion_action_var.set(action)
        if action!='none':
            labels={'exit':'退出程序','sleep':'让电脑睡眠','shutdown':'安排 60 秒后关机'}
            prompt=('当前批次包含 %d 项。全部成功完成且没有其他任务等待时，将执行“%s”。'
                    '\n此授权只用于这次批次，是否继续？'%(len(batch_ids),labels[action]))
            if not messagebox.askyesno('确认批次完成动作',prompt,parent=self):
                action='none'
                self.status.config(text='本批次未授权完成后动作；任务仍会继续')
        try:
            armed=self.completion_actions.arm(action,batch_ids,confirmed=True,dispatch_nonce=nonce)
        except Exception as exc:
            self.log('批次完成动作未启用：'+str(exc))
            self.status.config(text='完成动作未启用；任务仍会继续')
            self.completion_actions.disarm()
            self._completion_batch_ids=tuple(batch_ids)
            self._completion_dispatch_nonce=nonce
            self._sleep_guard_requested=bool(self.prevent_sleep_var.get())
            return True
        self._completion_batch_ids=tuple(batch_ids)
        self._completion_dispatch_nonce=armed.get('dispatch_nonce')
        self._sleep_guard_requested=bool(self.prevent_sleep_var.get())
        return True

    def _show_session_notification(self, title, message):
        text=title+'：'+message
        self.status.config(text=text)
        self.log(text)

    def _finish_completion_batch(self):
        batch_ids=self._completion_batch_ids
        if not batch_ids:
            return
        statuses={identifier:self._completion_statuses.get(identifier,'CANCELLED')
                  for identifier in batch_ids}
        for identifier,status in list(statuses.items()):
            if status in ('PENDING','RUNNING'):
                statuses[identifier]='CANCELLED'
                self._completion_statuses[identifier]='CANCELLED'
        other_pending=any(item.get('dispatch_id') not in batch_ids for item in self.pending.values())
        maintenance=bool(self.component_install_running or self.model_import_running or
                         (self.scan_thread and self.scan_thread.is_alive()))
        try:
            action_result=self.completion_actions.finish(
                statuses,self._completion_dispatch_nonce,pending=other_pending,
                active=self.is_running,maintenance=maintenance,
                quit_callback=lambda:self.after(0,self.close))
        except Exception as exc:
            action_result={'status':'FAILED','reason':str(exc)}
        if action_result.get('status')=='WAITING':
            self.completion_actions.disarm()
            self.log('本批次完成动作已取消：另有排队任务或维护工作；不会在之后的批次自动触发')
        elif action_result.get('status') in ('EXECUTED','SCHEDULED'):
            action=action_result.get('action')
            if action=='shutdown':
                self.log('已安排 Windows 在 60 秒后关机；期间开始新批次或取消会尝试取消本程序的关机请求')
            elif action=='sleep':
                self.log('已根据本批次确认请求系统睡眠')
            elif action=='exit':
                self.log('已根据本批次确认请求退出程序')
        elif action_result.get('status') in ('REFUSED','FAILED'):
            self.log('完成动作未执行：'+str(action_result.get('reason','操作失败')))

        all_done=bool(statuses) and all(status=='DONE' for status in statuses.values())
        failed=any(status in ('FAILED','CANCELLED','CANCELED','ERROR') for status in statuses.values())
        try:
            from core.session_actions import notify_failure,notify_success
            callback=self._show_session_notification if self.notify_in_app_var.get() else None
            if all_done:
                result=notify_success(callback=callback,beep=bool(self.notify_sound_var.get()),
                                      toast=bool(self.notify_toast_var.get()))
            elif failed:
                result=notify_failure(callback=callback,toast=bool(self.notify_toast_var.get()))
            else:
                result={}
            toast=result.get('toast') if isinstance(result,dict) else None
            if isinstance(toast,dict) and toast.get('status')=='DISABLED_BY_SYSTEM_POLICY':
                self.log('Windows 已关闭桌面通知；程序内状态提醒仍可用（未更改系统设置）')
            elif isinstance(toast,dict) and toast.get('status') not in ('SUBMITTED',):
                self.log('Windows 桌面通知未确认：'+str(toast.get('status','UNKNOWN')))
        except Exception as exc:
            self.log('批次通知失败：'+str(exc))
        self._completion_batch_ids=()
        self._completion_dispatch_nonce=None
        self._completion_statuses={}
        self._completion_tracking_to_key={}
    def load_queue(self):
        path=os.path.join(CONFIG_DIR,'queue.json')
        if not os.path.isfile(path):return
        try:
            with open(path,encoding='utf-8') as f:items=json.load(f)
            for item in items:
                if os.path.isfile(item['path']):
                    if not item.get('resume') and item.get('dispatch_id'):
                        for job in self.pipeline.storage.list_jobs(10000):
                            if job['parameters'].get('dispatch_id')==item['dispatch_id']:
                                item['resume']=job['id'];break
                    saved_params=item.get('params')
                    saved_mode=saved_params.get('mode') if isinstance(saved_params,dict) else None
                    queued_mode=item.get('source_mode') or saved_mode or self.mode.get()
                    key=self.tree.insert('','end',values=(os.path.basename(item['path']),'等待',queued_mode))
                    self.pending[key]=item
                    self.pending_paths.add(str(Path(item['path']).resolve()))
        except Exception as exc:self.log('队列恢复失败：'+str(exc))

    def start(self):
        if self.is_running:return
        wizard=self.__dict__.get('wizard_frame')
        if wizard is not None and wizard.__dict__.get('self_test_running',False):
            self.status.config(text='首次向导自检正在执行，请完成后再开始处理');return
        if self.__dict__.get('version_update_running',False):
            self.status.config(text='版本更新或回退正在验证，请完成或取消后再开始处理');return
        if self.__dict__.get('separator_experiment_running',False):
            self.status.config(text='候选分离实验正在执行，请完成后再开始处理');return
        if self.__dict__.get('benchmark_running',False):
            self.status.config(text='真实基准正在执行，请完成或取消后再开始处理');return
        if self.__dict__.get('runtime_restart_required',False):
            self.status.config(text='组件已改变，请关闭并重新启动程序，完成运行时验证后再处理')
            return
        if self.__dict__.get('cache_collect_running',False):
            self.status.config(text='缓存回收正在核验文件引用，请完成后再开始处理')
            return
        if self.component_install_running:
            self.status.config(text='离线组件正在安装或修复，请完成后再开始处理')
            return
        if self.scan_thread is not None:
            self.status.config(text='文件夹仍在后台扫描，请扫描完成后再开始处理')
            return
        if not self.pending:
            messagebox.showinfo('提示','先添加音频或从历史选择恢复');return
        items=list(self.pending.items())
        needs_current_params=any(
            not item.get('frozen_spec') and not item.get('resume') and
            not isinstance(item.get('params'),dict)
            for _key,item in items)
        try:params=self.parameters() if needs_current_params else {}
        except Exception as exc:
            messagebox.showerror('参数错误',str(exc));return
        from core.history_rerun import preflight_queue_items,resolve_queue_item
        resolved={}
        try:
            for key,item in items:
                mode,queued_params,is_frozen=resolve_queue_item(
                    item,params,storage=self.pipeline.storage)
                resolved[key]=(mode,queued_params,is_frozen)
                if not is_frozen and not item.get('resume'):
                    item['params']=queued_params
                    item['source_mode']=mode
                if not is_frozen and not item.get('resume') and mode=='voice_conversion':
                    reason=self._rvc_model_run_guard(queued_params)
                    if reason:
                        messagebox.showerror('RVC 模型尚未可用',reason)
                        self.status.config(text=reason)
                        return
        except Exception as exc:
            messagebox.showerror('任务快照无效',str(exc));return
        try:
            from core.component_platform import build_registry
            registry=build_registry(BASE_DIR)
            blocked=preflight_queue_items(items,params,registry,storage=self.pipeline.storage,
                                         resolved=resolved)
        except Exception as exc:
            messagebox.showerror('预检未通过',str(exc));return
        if blocked:
            decision=blocked['decision']
            messagebox.showerror('预检未通过',decision['message']);return
        if needs_current_params:
            try:self._persist_settings(params)
            except Exception as exc:
                messagebox.showerror('设置保存失败',str(exc));return
        for key,item in items:
            mode=item.get('source_mode') or (item.get('params') or {}).get('mode') or params.get('mode')
            self.tree.set(key,'state','排队')
            self.tree.set(key,'mode',mode)
        if not self._arm_completion_for_batch(items):
            return
        self.is_running=True
        self.processing_started_at=time.monotonic()
        self.progress_stage=None
        self.progress_stage_started_at=None
        self.progress_stage_message=''
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        self._refresh_component_mutation_controls()
        self.control=Control()
        self.dispatch_existing={job['id'] for job in self.pipeline.storage.list_jobs(1000)}
        self._try_save_queue()
        self.thread=self._start_background_worker(
            self.work,args=(items,),daemon=False,name='batch-processing')

    def work(self,items):
        from core.session_actions import SleepGuard
        def terminal_status(item, fallback, manifest=None):
            job_id=(manifest or {}).get('job_id') or item.get('resume')
            try:
                row=self.pipeline.storage.get_job(job_id) if job_id else None
                if row is None and item.get('dispatch_id'):
                    row=next((candidate for candidate in self.pipeline.storage.list_jobs(1000)
                              if candidate.get('parameters',{}).get('dispatch_id')==item.get('dispatch_id')),None)
                if row and row.get('status') in ('DONE','FAILED','CANCELLED','CANCELED'):
                    return 'CANCELLED' if row.get('status') in ('CANCELLED','CANCELED') else row['status']
            except Exception as exc:
                self.events.put(('log','无法从任务记录确认批次终态：'+str(exc)))
            return fallback

        guard=SleepGuard(enabled=self._sleep_guard_requested)
        try:
            guard.__enter__()
        except Exception as exc:
            self.events.put(('sleep_guard',('FAILED',str(exc))))
            guard=SleepGuard(enabled=False)
        if self._sleep_guard_requested and not guard.active:
            self.events.put(('sleep_guard',(guard.reason,None)))
        try:
            for key,item in items:
                if self.control.cancelled.is_set():break
                flow_parameters=copy.deepcopy(item.get('frozen_spec') or item.get('params') or {})
                flow_mode=item.get('source_mode') or flow_parameters.get('mode')
                self.events.put(('item',(key,'处理中',{'mode':flow_mode,'parameters':flow_parameters,
                                                     'frozen':isinstance(item.get('frozen_spec'),dict)})))
                try:
                    if item.get('new_job_from_snapshot'):
                        from core.history_rerun import create_history_rerun_job
                        created=create_history_rerun_job(
                            self.pipeline.storage,item['path'],item['source_mode'],
                            item['frozen_spec'],original_project_dir=item.get('original_project_dir'))
                        item['resume']=created['job_id']
                        params={'mode':created['mode'],'resume_job_id':created['job_id']}
                    else:
                        params=processing_parameters(item.get('params') or {})
                        if item.get('dispatch_id'):params['dispatch_id']=item['dispatch_id']
                        if item.get('resume'):params['resume_job_id']=item['resume']
                    manifest=self.pipeline.process_file(item['path'],control=self.control,**params)
                    manifest['source_path']=item['path']
                    terminal=terminal_status(item,'FAILED',manifest)
                    if terminal!='DONE':
                        raise RuntimeError('pipeline 返回成品，但 JobStore 未确认 DONE 终态')
                    self.events.put(('terminal',(item.get('dispatch_id'),terminal,manifest.get('job_id'))))
                    self.events.put(('done',(key,manifest)))
                except ProcessCancelled as exc:
                    self.events.put(('terminal',(item.get('dispatch_id'),
                                                 terminal_status(item,'CANCELLED'),item.get('resume'))))
                    self.events.put(('item',(key,'已取消')))
                    self.events.put(('log',str(exc)))
                    break
                except Exception as exc:
                    self.events.put(('terminal',(item.get('dispatch_id'),
                                                 terminal_status(item,'FAILED'),item.get('resume'))))
                    self.events.put(('failed',(key,str(exc))))
        finally:
            if guard.active:
                try:guard.__exit__(None,None,None)
                except Exception as exc:self.events.put(('sleep_guard',('RESTORE_FAILED',str(exc))))
        self.events.put(('idle',None))

    def _drain_events(self):
        for _event_index in range(20):
            try:kind,data=self.events.get_nowait()
            except queue.Empty:break
            if kind=='runtime_check_progress':
                self.runtime_status.config(text='运行环境：'+data)
                import re
                counts=re.search(r'已校验\s+(\d+)/(\d+)',data)
                if counts:
                    self.runtime_progress.stop()
                    self.runtime_progress.configure(mode='determinate',maximum=int(counts[2]),value=int(counts[1]))
            elif kind=='runtime_check_done':
                self.runtime_check_running=False
                self.runtime_progress.stop()
                if self.runtime_progress.winfo_ismapped():
                    self.runtime_progress.pack_forget()
                self.runtime_gate.finish(data)
                self.runtime_retry_button.state(['!disabled'])
                if self.runtime_gate.ready:
                    self.runtime_status.config(text='运行环境：校验通过，可以开始处理')
                    for button in self.runtime_action_buttons:button.state(['!disabled'])
                    self.model_inspection_pending.clear()
                    self.refresh_models()
                    self._maybe_open_onboarding()
                else:
                    self.runtime_status.config(text='运行环境：校验未通过，请在工具页安装/修复组件或重新校验')
                    self.log('运行校验失败：'+json.dumps(data,ensure_ascii=False))
                self._refresh_component_mutation_controls()
            elif kind=='log':self.log(data)
            elif kind=='terminal':
                dispatch_id,status,job_id=data
                if dispatch_id in self._completion_statuses:
                    self._completion_statuses[dispatch_id]=status
                if job_id:
                    self.log('批次终态已由任务记录确认：%s → %s'%(str(job_id),status))
            elif kind=='sleep_guard':
                state,error=data
                if state=='system_idle_sleep_prevented':
                    self.log('本批次期间已启用系统防休眠，结束时会恢复原状态')
                elif error:
                    self.log('系统防休眠未能启用或恢复：'+str(error))
                    self.status.config(text='音频任务继续处理，但系统防休眠未生效；请保持电脑唤醒')
                else:
                    self.log('系统防休眠不可用：'+str(state))
            elif kind=='drop':
                try:self.add_files(data)
                except Exception as exc:self.log('拖拽添加失败：'+str(exc))
            elif kind=='scan_batch':
                token,paths=data
                if token==getattr(self,'_scan_token',None):
                    self._insert_pending_paths(paths)
                    self._try_save_queue()
            elif kind=='scan_done':
                token,folder,error=data
                if token==getattr(self,'_scan_token',None):
                    self.scan_thread=None
                    if self.__dict__.pop('scan_results_rejected',False):
                        self.log('缓存操作期间扫描批次未添加，请重新扫描目录')
                        self.status.config(text='扫描批次未添加，请在缓存操作完成后重新扫描目录')
                    elif error:
                        self.log('文件夹扫描失败：'+str(error))
                        self.status.config(text='文件夹扫描失败；可继续添加文件或重试')
                    elif not self.scan_cancel.is_set():
                        self._try_save_queue()
                        self.status.config(text='文件夹扫描完成；检查队列后即可开始处理')
            elif kind=='progress':
                s,p,m=data
                if s != self.progress_stage:
                    self.progress_stage=s
                    self.progress_stage_started_at=time.monotonic()
                self.progress_stage_message=str(m or '')
                if isinstance(p,(int,float)) and 0 <= float(p) <= 1:
                    self.progress.stop()
                    self.progress.configure(mode='determinate')
                    self.progress['value']=float(p)*100
                else:
                    self.progress.configure(mode='indeterminate')
                    self.progress.start(12)
                self._update_progress_elapsed()
                self.highlight_running_stage(s,m)
            elif kind=='voice_range':
                self.voice_range_running=False
                if self.voice_range_button is not None:
                    self.voice_range_button.configure(state='normal')
                source_kind,report,error=data
                if error:
                    self.status.config(text='音域分析失败：'+str(error))
                    self.log('音域分析失败：'+str(error))
                else:
                    observed=report.get('observed_range_hz')
                    if source_kind=='mixed_music':
                        detail='整曲周期观察不能作为人声音域或半音建议'
                    elif observed:
                        detail='分离人声约 %.1f–%.1f Hz（中位 %.1f Hz）'%(observed['low_hz'],observed['high_hz'],observed['median_hz'])
                    else:
                        detail='未取得可靠的人声音高范围'
                    detail+='；目标音域未知，不建议自动改半音'
                    if report.get('report_path'):
                        detail+='；报告已保存'
                    self.status.config(text=detail)
                    self.log('音域观察：'+detail)
            elif kind=='waveform':
                token,record,error=data
                if token==self.waveform_token:
                    self.waveform_running=False
                    if error:
                        self._draw_waveform_error(str(error))
                    else:
                        self._draw_waveform(record)
            elif kind=='ab_preview':
                token,side,record,error=data
                if token==getattr(self,'ab_worker_token',None):self.ab_preparing=False
                if record:
                    self.__dict__.setdefault('ab_owned_previews',[]).append(record['preview_path'])
                if token==getattr(self,'ab_token',None) and not self._closing:
                    self.ab_preparing=False
                    if error:
                        self.ab_status.configure(text='试听准备失败：'+str(error))
                    else:
                        try:
                            from core.ab_preview import play_ab_preview
                            play_ab_preview(record)
                            self.ab_status.configure(text=f'正在试听 {side}：同起点 {record["start_seconds"]:.2f}s，片段 {record["duration_seconds"]:.2f}s；听感未评价')
                        except Exception as error:
                            self.ab_status.configure(text='声音播放失败：'+str(error))
            elif kind=='new_install':
                result,error=data;self.new_install_running=False
                self.status.config(text='安装失败：'+str(error) if error else '完整包已校验安装到 '+result['target_root']+'；原位置和用户数据保留')
            elif kind=='separator_experiment':
                result,error=data;self.separator_experiment_running=False
                self.status.config(text='候选实验失败：'+str(error) if error else '候选实验产物已生成，音质需人工试听：'+result['manifest_path'])
            elif kind=='item':
                self.tree.set(data[0],'state',data[1])
                if data[1]=='处理中':
                    if len(data)>2:
                        self.running_flow=data[2]
                        self.refresh_mode_flow()
                    self.active_key=data[0]
                    self.processing_started_at=time.monotonic()
                    self.progress_stage=None
                    self.progress_stage_started_at=None
            elif kind=='done':
                key,manifest=data
                self.last_manifest=manifest
                self.result_manifests[key]=manifest
                if not self.tree.selection() and not (self.__dict__.get('history_tree') and self.history_tree.selection()):
                    self.tree.selection_set(key)
                qc_info = manifest.get('qc', {}) if isinstance(manifest, dict) else {}
                qc_status = qc_info.get('status', 'Unknown')
                integrity = qc_info.get('processing_integrity', 'INTACT')
                degraded = qc_info.get('degraded_stages') or []
                if integrity == 'DEGRADED' or degraded:
                    deg_names = [d.get('name', d.get('stage', '')) for d in degraded]
                    deg_str = f" ({','.join(deg_names)})" if deg_names else ""
                    self.tree.set(key, 'state', f"完成 · 降级警告{deg_str}")
                    self.status.config(text=f"完成（带降级警告）：阶段异常已保留原音频，QC {qc_status}；查看质检详情或报告")
                elif qc_status == 'WARN':
                    self.tree.set(key, 'state', f"完成 · QC WARN")
                    self.status.config(text=f"完成（带警告）：QC WARN；请试听或查看报告")
                else:
                    self.tree.set(key, 'state', f"完成 · QC {qc_status}")
                    self.status.config(text=f"完成：QC {qc_status}")
                pending_item=self.pending.pop(key,None)
                if pending_item:
                    self.pending_paths.discard(str(Path(pending_item['path']).resolve()))
                self._try_save_queue()
                self.active_key=None
            elif kind=='failed':
                key,error=data;self.tree.set(key,'state','失败')
                self.log(error)
                self.last_failure_detail=str(error)
                self.status.config(text='任务失败，请点击下方【查看日志】查看原因')
                pending_item=self.pending.pop(key,None)
                if pending_item:
                    self.pending_paths.discard(str(Path(pending_item['path']).resolve()))
                self._try_save_queue()
                self.active_key=None
            elif kind=='idle':
                self.is_running=False;self.refresh_history()
                self.running_flow=None
                self.refresh_mode_flow()
                self.progress.stop()
                self.progress.configure(mode='determinate')
                self.progress['value']=0
                self.processing_started_at=None
                self.progress_stage=None
                self.progress_stage_started_at=None
                self._refresh_component_mutation_controls()
                self.active_key=None
                self.status.config(text=('本轮结束，但队列保存失败，请检查配置目录权限' if self.queue_dirty else
                                         '本轮结束；查看每项状态及 QC 报告，失败可在历史继续'))
                self._finish_completion_batch()
            elif kind=='cache_info':
                token,total,count,root,error=data
                if token==self.cache_stats_token:
                    if error:
                        self.log('缓存统计失败：'+str(error))
                        self.status.config(text='缓存统计失败；可稍后重试')
                    else:
                        messagebox.showinfo('输出与缓存',
                            '项目目录共 %.2f GB（%d 个文件）。缓存依赖项目 .work；删除已完成项目会使对应缓存失效，下次自动重跑。'
                            %(total/1024**3,count))
                        self.status.config(text='缓存统计完成')
            elif kind=='cache_preview':
                token,policy,result,error=data
                if token!=self.cache_cleanup_token:continue
                self.cache_collect_running=False
                if error:
                    self.log('缓存预览失败：'+str(error));self.status.config(text='缓存预览失败')
                    continue
                paths=list(result.get('selected_paths',[]))
                self.cache_preview_paths=paths
                if not paths:
                    messagebox.showinfo('缓存预览','当前策略没有可回收的 .work 文件')
                    self.status.config(text='缓存预览完成：没有可回收文件')
                    continue
                sample='\n'.join(paths[:20])
                if len(paths)>20:sample+=f'\n…及其他 {len(paths)-20} 个文件'
                detail=('将按当前策略回收以下已完成任务缓存：\n\n'+sample+
                        f'\n\n预计释放 {result.get("reclaimed_bytes",0)/1024**3:.2f} GB。')
                if messagebox.askyesno('确认缓存回收',detail):
                    self.status.config(text='正在后台执行已确认的缓存回收…')
                    self._start_cache_collect(policy,False,paths)
                else:
                    self.status.config(text='已取消缓存回收')
            elif kind=='cache_cleanup':
                token,result,error=data
                if token!=self.cache_cleanup_token:continue
                self.cache_collect_running=False
                if error:
                    self.log('缓存回收失败：'+str(error));self.status.config(text='缓存回收失败')
                elif result.get('changed') is False:
                    self.log('缓存清单在确认后发生变化，未执行删除：'+str(result.get('reason','')))
                    self.status.config(text='缓存清单已变化，未执行删除；请重新预览')
                else:
                    self.status.config(text='缓存回收完成，释放 %.2f GB'%(
                        result.get('reclaimed_bytes',0)/1024**3))
            elif kind=='cache_cleanup_changed':
                token,reason=data
                if token==self.cache_cleanup_token:
                    self.cache_collect_running=False
                    self.log('缓存回收已取消：'+reason)
                    self.status.config(text='缓存清单已变化，未执行删除；请重新预览')
            elif kind=='diagnostic':
                self.diagnostics.delete('1.0','end');self.diagnostics.insert('end',data)
            elif kind=='source_preview':
                token,report,error=data
                if token!=self.__dict__.get('source_preview_token'):continue
                self.source_preview_running=False
                if self.__dict__.get('source_preview_requested_path')!=self.source_preview_path:
                    if not self.__dict__.get('_closing',False):self.preview_pending_source()
                    continue
                if error:self.source_preview_label.config(text='源信息分析未完成：'+str(error))
                else:
                    depth=str(report['bit_depth'])+' bit' if report['bit_depth'] else '压缩音频（位深不适用）'
                    self.source_preview_label.config(text=(f"{report['duration']:.2f} 秒 · {report['sample_rate']} Hz · {depth} · "
                        f"{report['channels']} 声道 · {report['integrated_lufs']:.2f} LUFS · {report['true_peak_dbtp']:.2f} dBTP · "
                        f"BPM 估计 {report['bpm_estimate']:.1f} · 调性估计 {report['key_estimate']}（最多首90秒）"))
            elif kind=='runtime_benchmark':
                report,path,error=data
                self.benchmark_running=False
                if error:self.status.config(text='真实基准未完成：'+str(error))
                else:
                    self.status.config(text=f"真实基准 {report['status']}，20 秒合成输入，耗时 {report['elapsed_seconds']:.2f} 秒；配置未自动改变")
                    self.log('基准阶段、设备与结果记录：'+str(path))
            elif kind=='version_operation':
                result,error=data
                self.version_update_running=False
                self._refresh_component_mutation_controls()
                if error:
                    self.log('版本操作失败：'+str(error));self.status.config(text='版本操作未完成；当前程序仍可用，失败证据见日志')
                elif not self.__dict__.get('_closing',False):
                    try:
                        if result.get('verification_kind')!='CURRENT_TRUSTED_FULL_RUNTIME_VERIFIER':
                            raise ValueError('缺少真实完整运行时验证，拒绝启用版本')
                        os.startfile(result['launcher_path'])
                        self.runtime_restart_required=True
                        self.status.config(text='已向 Windows 提交已验证版本的启动请求，正在关闭当前版本')
                        self.after(0,self.close)
                    except Exception as exc:
                        self.log('版本已验证，启动请求失败：'+str(exc))
                        self.status.config(text='新版本完整校验通过，但启动请求失败；请打开：'+result['launcher_path'])
            elif kind=='benchmark_progress':
                if self.__dict__.get('benchmark_running',False):
                    stage,p,msg=data
                    self.status.config(text=f'真实基准 {stage}：{msg}')
            elif kind=='model_inspection':
                key,path,signature,report,error=data
                self.model_inspection_pending.discard(path)
                if self._model_stat_signature(path)==signature:
                    self.model_inspection_cache[path]=(signature,report,error)
                    self.model_inspection_reports[key]=report
                self.refresh_models()
            elif kind=='model_import':
                result,error=data
                self.model_import_running=False
                self._refresh_component_mutation_controls()
                if error:
                    self.status.config(text='用户模型安全检查或导入失败；未提交到用户模型目录')
                    self.log('用户模型安全导入失败：'+str(error))
                else:
                    self.status.config(text='用户模型已通过后台安全检查并导入；正在刷新可推理状态')
                    self.log('用户模型导入完成：'+str(result.get('imported')))
                    self.refresh_models()
            elif kind=='model_inspection_done':
                self.model_inspection_running=False
                for path in list(self.model_inspection_pending):
                    cached=self.model_inspection_cache.get(path)
                    if not cached or cached[0]!=self._model_stat_signature(path):
                        self.model_inspection_pending.discard(path)
                self.refresh_models()
            elif kind=='component_sizes':
                token,report,error=data
                if token!=self.component_size_token:continue
                self.component_size_running=False
                self._refresh_component_mutation_controls()
                if error:
                    self.component_size_status.config(text='测量失败：'+str(error))
                    self.log('组件体积测量失败：'+str(error))
                else:
                    self.component_size_cache=report
                    self._render_component_sizes(report)
                    errors=sum(len(row.get('errors') or []) for row in report.get('components',{}).values())
                    self.component_size_status.config(text='测量完成；不可确认的下载/缓存来源显示为未知；错误 %d'%errors)
            elif kind=='component_import':
                result,registry,hardware,error,refresh_error=data
                self.component_install_running=False
                self.component_size_cache=None
                self._refresh_component_mutation_controls()
                if error:
                    self.log('离线组件安装或修复失败：'+str(error))
                    self.status.config(text='离线组件安装或修复失败；请检查日志中的校验或回滚结果')
                else:
                    if registry is not None:
                        from core.component_platform import capability_report
                        gate_res=(self.__dict__.get('runtime_gate') and self.runtime_gate.result) or None
                        modes=capability_report(registry,runtime_verification=gate_res).get('modes',{})
                        status_map={'READY':'就绪','AVAILABLE':'就绪','MISSING_COMPONENTS':'缺少依赖','USER_IMPORT_REQUIRED':'需导入模型','FAILED':'校验未通过','BLOCKED':'不可用'}
                        mode_labels={'polish':'精修','voice_conversion':'换声','transcribe':'扒谱'}
                        text='模式状态：'+'；'.join(mode_labels.get(m,m)+' '+status_map.get((modes.get(m) or {}).get('display_status') or (modes.get(m) or {}).get('status','未知'),'未知') for m in ('polish','voice_conversion','transcribe'))
                        self.component_status.config(text=text)
                        if self.wizard_frame is not None:
                            self.wizard_frame.session.registry=registry
                            self.wizard_frame.session.hardware=hardware or self.wizard_frame.session.hardware
                            self.wizard_frame.refresh()
                    installed=len(result.get('installed',[])) if isinstance(result,dict) else 0
                    status=(result or {}).get('status','INSTALLED') if isinstance(result,dict) else 'INSTALLED'
                    if isinstance(result,dict) and result.get('restart_required'):
                        self.runtime_restart_required=True
                        status+='；必须重新启动并通过运行时验证'
                        self._refresh_component_mutation_controls()
                    self.status.config(text=f'离线目录检查完成：{status}，更新 {installed} 个托管文件；音质仍待人工听审')
                    if refresh_error:
                        self.log('组件已处理，但模式状态刷新失败：'+str(refresh_error))
        if self.is_running and self.active_key in self.pending:
            item=self.pending[self.active_key]
            if not item.get('resume'):
                for job in self.pipeline.storage.list_jobs(5):
                    if item.get('dispatch_id') and job['parameters'].get('dispatch_id')==item['dispatch_id']:
                        item['resume']=job['id'];self._try_save_queue();break

    def poll_events(self):
        try:
            self._drain_events()
        except Exception as exc:
            try:
                self.log('界面事件处理失败，轮询会继续：'+str(exc))
                self.status.config(text='事件处理异常，请查看日志；任务轮询仍在继续')
            except Exception:
                pass
        finally:
            if self.__dict__.get('is_running',False):
                self._update_progress_elapsed()
            try:self.after(100,self.poll_events)
            except tk.TclError:pass

    def begin_runtime_check(self):
        if self.runtime_gate is None or self.runtime_check_running:return
        if self.__dict__.get('runtime_restart_required'):
            self.runtime_status.config(text='运行环境：组件已改变，请关闭后重新启动')
            return
        if self.__dict__.get('is_running'):
            self.runtime_status.config(text='运行环境：正在处理，结束后才能重新校验')
            return
        self.runtime_gate.state='PENDING'
        self.runtime_gate.cancelled.clear()
        self.runtime_check_running=True
        if not self.runtime_progress.winfo_ismapped():
            self.runtime_progress.pack(side='left',padx=6,after=self.runtime_status)
        self.runtime_progress.configure(mode='indeterminate')
        self.runtime_progress.start(12)
        self.runtime_retry_button.state(['disabled'])
        for button in self.runtime_action_buttons:button.state(['disabled'])
        self._refresh_component_mutation_controls()
        self.runtime_status.config(text='运行环境：后台校验中，可先浏览、添加文件和设置参数')
        if '--verify-and-run' in sys.argv:
            index=sys.argv.index('--startup-token') if '--startup-token' in sys.argv else -1
            token=sys.argv[index+1] if index>=0 and index+1<len(sys.argv) else ''
            if len(token)==64 and all(c in '0123456789ABCDEF' for c in token):
                print('AMP_GUI_READY:'+token,flush=True)
        def task():
            from core.runtime_verify import verify_runtime
            try:
                def progress(message):
                    if self.runtime_gate.cancelled.is_set():raise RuntimeError('用户已关闭界面，校验已取消')
                    self.events.put(('runtime_check_progress',message))
                result=verify_runtime(root=_APP_ROOT,timeout=900,progress=progress,
                                      cancel_event=self.runtime_gate.cancelled)
            except Exception as exc:result={'status':'FAIL','error':str(exc)}
            try:
                report=Path(LOG_DIR)/'startup-runtime-check.json'
                report.parent.mkdir(parents=True,exist_ok=True)
                temporary=report.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                os.replace(temporary,report)
            except OSError as exc:self.events.put(('log','启动校验报告写入失败：'+str(exc)))
            self.events.put(('runtime_check_done',result))
        self._start_background_worker(task,daemon=True,name='startup-runtime-check')

    def _update_progress_elapsed(self):
        if not self.__dict__.get('is_running',False):
            return
        now=time.monotonic()
        if self.progress_stage is not None:
            started=self.progress_stage_started_at or self.processing_started_at or now
            elapsed=max(0,int(now-started))
            detail=self.progress_stage_message
            suffix=(' · '+detail) if detail else ''
            self.status.config(text=f'{self.progress_stage}：已运行 {elapsed} 秒{suffix}')
        elif self.processing_started_at is not None:
            elapsed=max(0,int(now-self.processing_started_at))
            self.status.config(text=f'处理中：已运行 {elapsed} 秒 · 当前阶段尚未报告进度')

    def pause(self):
        if not self.is_running:return
        if self.control.paused.is_set():
            self.control.resume();self.status.config(text='继续处理')
        else:
            self.control.pause();self.status.config(text='当前子步骤完成后暂停')
    def cancel(self):
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_cancel.set()
            self.status.config(text='正在取消文件夹扫描…')
        self._abort_scheduled_shutdown('用户取消当前工作')
        self.completion_actions.disarm()
        if self.is_running:self.control.cancel()
    def remove_pending(self):
        if self.is_running:return
        for key in self.tree.selection():
            item=self.pending.pop(key,None)
            if item:self.pending_paths.discard(str(Path(item['path']).resolve()))
            self.tree.delete(key)
        self._try_save_queue()

    def refresh_history(self):
        self.history_tree.delete(*self.history_tree.get_children())
        for job in self.pipeline.storage.list_jobs(include_hidden=self.__dict__.get('show_deleted_history',False)):
            status=job['status']
            if job.get('hidden'):status='已移出历史'
            stage=job['current_stage']
            if job.get('snapshot_error'):
                status='参数快照损坏'
                stage='请新建重跑'
            elif status == 'DONE' and job.get('project_dir'):
                pjson = os.path.join(job['project_dir'], 'project.json')
                if os.path.isfile(pjson):
                    try:
                        with open(pjson, 'r', encoding='utf-8-sig') as f:
                            mdata = json.load(f)
                        qc = mdata.get('qc', {})
                        if qc.get('processing_integrity') == 'DEGRADED' or qc.get('degraded_stages'):
                            status = '完成 (降级警告)'
                        elif qc.get('status') == 'WARN':
                            status = '完成 (带警告)'
                        elif qc.get('status') == 'PASS':
                            status = '完成'
                    except Exception:
                        pass
            self.history_tree.insert('','end',iid=job['id'],values=(os.path.basename(job['source_path']),job['mode'],status,stage))
    def hide_history(self):
        job=self.selected_job()
        if not job:return
        if not messagebox.askyesno('删除历史记录','从历史列表移除此记录。源文件、成品和任务快照会保留，可通过“显示已删除 / 恢复”恢复记录。'):return
        try:self.pipeline.storage.set_history_hidden(job['id'],True)
        except Exception as exc:messagebox.showerror('无法删除记录',str(exc));return
        self.refresh_history()
    def toggle_deleted_history(self):
        job=self.selected_job()
        if job and job.get('hidden'):
            try:self.pipeline.storage.set_history_hidden(job['id'],False)
            except Exception as exc:messagebox.showerror('无法恢复记录',str(exc));return
        else:self.show_deleted_history=not self.__dict__.get('show_deleted_history',False)
        self.refresh_history()
    def selected_job(self):
        keys=self.history_tree.selection()
        if not keys:return None
        try:
            return self.pipeline.storage.get_job(keys[0])
        except Exception as exc:
            # list_jobs deliberately returns a safe snapshot_error row for a
            # corrupt parameters_json; keep history usable and block resume.
            for job in self.pipeline.storage.list_jobs(10000):
                if job.get('id')==keys[0]:return job
            self.log('历史任务读取失败：'+str(exc))
            return None
    def resume_history(self):
        if self.__dict__.get('cache_collect_running',False):
            self.status.config(text='请等待缓存回收完成后再添加历史任务');return
        if self.is_running:return
        job=self.selected_job()
        if not job:return
        if job.get('snapshot_error'):
            messagebox.showwarning('无法继续恢复',
                '原任务参数快照损坏，不能按历史参数恢复或新建重跑。请保留旧项目；如需当前设置，请重新添加源音频或使用“按当前设置重跑”。')
            return
        params=processing_parameters(job['parameters']);params['mode']=job['mode']
        key=self.tree.insert('','end',values=(os.path.basename(job['source_path']),'等待恢复',job['mode']))
        self.pending[key]=dict(path=job['source_path'],resume=job['id'],params=params,
                               frozen_spec=copy.deepcopy(job['parameters']),source_mode=job['mode'])
        self.pending_paths.add(str(Path(job['source_path']).resolve()))
        self._try_save_queue();self.start()
    def rerun_history(self):
        if self.__dict__.get('cache_collect_running',False):
            self.status.config(text='请等待缓存回收完成后再添加历史任务');return
        if self.is_running:return
        job=self.selected_job()
        if not job:return
        try:
            from core.history_rerun import clone_history_spec
            stages=self.pipeline.storage.list_stages(job['id'])
            frozen_spec=clone_history_spec(job,stages)
            mode=job['mode']
        except Exception as exc:
            messagebox.showerror('新建重跑参数错误',str(exc));return
        key=self.tree.insert('','end',values=(os.path.basename(job['source_path']),'等待新建重跑',mode))
        self.pending[key]=dict(path=job['source_path'],resume=None,
                               frozen_spec=frozen_spec,source_mode=mode,
                               new_job_from_snapshot=True,original_project_dir=job.get('project_dir'),
                               history_source_job=job['id'],dispatch_id=uuid.uuid4().hex)
        self.pending_paths.add(str(Path(job['source_path']).resolve()))
        self._try_save_queue();self.start()
    def rerun_history_current(self):
        if self.__dict__.get('cache_collect_running',False):
            self.status.config(text='请等待缓存回收完成后再添加历史任务');return
        """Explicitly drop the selected history snapshot and use current GUI settings."""
        if self.is_running:return
        job=self.selected_job()
        if not job:return
        if not os.path.isfile(job.get('source_path') or ''):
            messagebox.showerror('无法重跑','历史源文件不存在；请先恢复源文件。')
            return
        try:
            params=self.parameters()
        except Exception as exc:
            messagebox.showerror('当前参数错误',str(exc));return
        try:self._persist_settings(params)
        except Exception as exc:
            messagebox.showerror('设置保存失败',str(exc));return
        mode=params['mode']
        key=self.tree.insert('','end',values=(os.path.basename(job['source_path']),'等待按当前设置重跑',mode))
        self.pending[key]=dict(path=job['source_path'],resume=None,params=params,
                               source_mode=mode,dispatch_id=uuid.uuid4().hex,
                               current_parameters_explicit=True)
        self.pending_paths.add(str(Path(job['source_path']).resolve()))
        self._try_save_queue();self.start()
    def open_history(self):
        job=self.selected_job()
        if job and job['project_dir'] and os.path.isdir(job['project_dir']):os.startfile(job['project_dir'])
    def copy_history(self):
        job=self.selected_job()
        if job:self.apply_params(dict(job['parameters'],mode=job['mode']))
    def apply_params(self,p):
        self.mix_vocal_gain.set(str(p.get('mix_vocal_gain_db',.5)))
        self.mix_instrumental_gain.set(str(p.get('mix_instrumental_gain_db',-1)))
        self.transcribe_stem.set(p.get('transcribe_stem','vocal'))
        execution=p.get('execution_policy') or {}
        self.device_choice.set(p.get('device',execution.get('device','auto')))
        self.performance_profile.set(p.get('performance_profile',execution.get('performance_profile','balanced')))
        self.postprocess_profile.set(p.get('rvc_postprocess_profile','off'))
        self.adaptive_mix.set(bool(p.get('adaptive_mix',False)))
        self.mix_bus_profile.set(p.get('mix_bus_profile','off'))
        self.input_channel_mode.set(p.get('input_channel_mode','auto'))
        self.output_channels.set(str(p.get('output_channels',2)))
        self.detailed_analysis.set(p.get('detailed_analysis',True))
        for key,var in [('mode',self.mode),('clean_preset',self.clean),('master_preset',self.master_preset_var),
                        ('rvc_model_key',self.model),('pitch_shift',self.pitch),('f0_method',self.f0),
                        ('index_rate',self.index),('bpm',self.bpm),('output_sample_rate',self.sr),
                        ('generate_midi',self.midi),('custom_output_dir',self.output)]:
            if key in p:var.set(p[key])
        if 'polish_profile' in p:
            from core.polish_policy import STRENGTH_LABELS
            self.polish_strength.set(STRENGTH_LABELS.get(p['polish_profile'],'推荐'))
        if 'polish_reference' in p:
            self.polish_reference.set(p.get('polish_reference') or '')
        if 'pitch_correction_mode' in p:
            self.pitch_correction_mode.set(p.get('pitch_correction_mode') or 'off')
        if 'pitch_correction_key' in p:
            self.pitch_correction_key.set(p.get('pitch_correction_key') or 'auto')
        if 'vocal_restoration_effect' in p:
            self.vocal_restoration_effect.set(p.get('vocal_restoration_effect') or 'off')
        if isinstance(p.get('vocal_restore_profile'),dict):
            profile=p['vocal_restore_profile']
            self.vocal_restore_mode.set(profile.get('mode','auto'))
            self.vocal_restore_hpf.set(bool(profile.get('highpass_enabled',False)))
            self.vocal_restore_hpf_hz.set(str(profile.get('highpass_hz',60)))
            self.vocal_restore_deesser.set(bool(profile.get('deesser_enabled',False)))
            self.vocal_restore_deesser_intensity.set(str(profile.get('deesser_intensity',.08)))
            self.vocal_restore_deesser_reduction.set(str(profile.get('deesser_max_reduction',.15)))
            self.vocal_restore_normalize.set(bool(profile.get('normalize_enabled',False)))
            self.vocal_restore_peak.set(str(profile.get('normalize_peak',.8)))
        if isinstance(p.get('midi_cleanup'),dict):
            cleanup=p['midi_cleanup']
            self.midi_cleanup_enabled.set(bool(cleanup.get('enabled',False)))
            self.midi_cleanup_quantize.set(bool(cleanup.get('quantize',False)))
            self.midi_cleanup_merge.set(bool(cleanup.get('merge_overlaps',False)))
        if 'mix_ducking' in p:
            self.mix_ducking.set(bool(p['mix_ducking']))
        self.mp3.set('mp3' in p.get('output_formats',['wav','mp3']))
        self.flac.set('flac' in p.get('output_formats',[]))
        chosen=p.get('separator_profile') if isinstance(p, dict) else None
        if isinstance(chosen, dict):
            self.separator_route.set('hp2' if chosen.get('mode')=='hp2' else 'dual')
        else:
            self.separator_route.set('hp2' if p.get('mode')=='voice_conversion' else 'dual')
        self.refresh_mode_flow()

    def choose_polish_reference(self):
        path=filedialog.askopenfilename(filetypes=[('音频','*.wav *.mp3 *.flac *.m4a'),('所有文件','*.*')])
        if path:
            self.polish_reference.set(path)

    def _apply_polish_visibility(self):
        if not self.__dict__.get('polish_row') or not self.__dict__.get('polish_advanced'):
            return
        polish=self.mode.get()=='polish'
        tier=self.polish_tier.get()
        top=self.clean_widget.master
        for widget in (self.clean_label,self.clean_widget,self.master_label,self.master_widget,
                       self.model_label,self.model_widget,self.pitch_label,self.pitch_widget):
            widget.pack_forget()
        self.voice_note.pack_forget()
        self.voice_advanced.pack_forget()
        self.polish_advanced.pack_forget()
        self.polish_expert_frame.pack_forget()
        if polish:
            self.polish_row.pack(fill='x',pady=4)
        else:
            self.clean_label.pack(in_=top,side='left',padx=(8,3))
            self.clean_widget.pack(in_=top,side='left')
            self.master_label.pack(in_=top,side='left',padx=(8,3))
            self.master_widget.pack(in_=top,side='left')
            self.model_label.pack(in_=top,side='left',padx=(8,3))
            self.model_widget.pack(in_=top,side='left')
            self.pitch_label.pack(in_=top,side='left',padx=(8,3))
            self.pitch_widget.pack(in_=top,side='left')
            self.voice_note.pack(anchor='w',after=top)
            self.voice_advanced.pack(fill='x',pady=5,after=self.voice_note)
            self.polish_row.pack_forget()
            return
        if tier in ('高级','专家'):
            self.polish_advanced.pack(fill='x',pady=4)
            self.master_label.pack(in_=self.polish_level_row,side='left',padx=(8,3))
            self.master_widget.pack(in_=self.polish_level_row,side='left')
        if tier=='专家':
            self.polish_expert_frame.pack(fill='x',pady=4)

    def selected_manifest(self):
        """Resolve result actions from the currently selected queue/history job."""
        if self.__dict__.get('result_surface','queue') == 'queue':
            keys=self.tree.selection()
            for key in keys:
                manifest=self.result_manifests.get(key)
                if manifest:return manifest
            if keys:return None
        job=self.selected_job()
        if not job or not job.get('project_dir'):
            return None
        manifest_path=os.path.join(job['project_dir'],'project.json')
        try:
            with open(manifest_path,encoding='utf-8-sig') as handle:
                manifest=json.load(handle)
            if isinstance(manifest,dict) and manifest.get('output_paths'):
                manifest.setdefault('source_path',job.get('source_path'))
                return manifest
        except (OSError,ValueError,json.JSONDecodeError) as exc:
            self.log('项目结果读取失败：'+str(exc))
        return None

    def on_select_result(self, event=None):
        manifest = self.selected_manifest()
        if not manifest:
            return
        qc = manifest.get('qc', {})
        if not qc:
            return
        status = qc.get('status', 'Unknown')
        integrity = qc.get('processing_integrity', 'INTACT')
        degraded = qc.get('degraded_stages') or []
        source_name = os.path.basename(manifest.get('source_path') or '当前项目')
        if integrity == 'DEGRADED' or degraded:
            stage_names = ', '.join([d.get('name', d.get('stage', '')) for d in degraded])
            self.status.config(text=f"【{source_name}】完成但有降级警告：[{stage_names}] 异常已保留原音频；QC {status}；双击或点击“质检详情”查看")
        elif status == 'WARN':
            self.status.config(text=f"【{source_name}】完成但有指标警告：QC WARN；点击“质检详情”或“查看报告”")
        elif status == 'PASS':
            self.status.config(text=f"【{source_name}】处理完成：QC PASS (客观指标通过)")

    def open_report(self):
        manifest = self.selected_manifest()
        if not manifest:
            messagebox.showinfo('未选择结果', '请在队列或历史中选择一个已完成任务')
            return
        reports_dir = (manifest.get('output_paths') or {}).get('reports_dir')
        candidates = []
        if reports_dir:
            candidates.append(os.path.join(reports_dir, 'process_report.md'))
        job = self.selected_job()
        if job and job.get('project_dir'):
            candidates.append(os.path.join(job['project_dir'], 'reports', 'process_report.md'))
        for c in candidates:
            if os.path.isfile(c):
                os.startfile(c)
                return
        messagebox.showinfo('未找到报告', '未找到质检报告 process_report.md')

    def show_qc_details(self):
        manifest = self.selected_manifest()
        if not manifest:
            messagebox.showinfo('未选择结果', '请在队列或历史中选择一个已完成任务')
            return
        qc = manifest.get('qc', {})
        if not qc:
            messagebox.showinfo('无质检数据', '该任务尚未记录质检数据')
            return
        window = tk.Toplevel(self)
        window.title('质检与阶段执行状态')
        window.geometry('680x520')

        status = qc.get('status', 'Unknown')
        integrity = qc.get('processing_integrity', 'INTACT')
        degraded = qc.get('degraded_stages') or []
        audio_metrics_passed = qc.get('audio_metrics_passed', status != 'FAIL')

        header_frame = ttk.Frame(window, padding=10)
        header_frame.pack(fill='x')
        ttk.Label(header_frame, text=f"任务：{os.path.basename(manifest.get('source_path') or '当前项目')}", font=('', 10, 'bold')).pack(anchor='w')
        ttk.Label(header_frame, text=f"综合 QC 状态：{status}   |   处理完整度：{integrity}   |   物理指标：{'通过' if audio_metrics_passed else '未通过'}").pack(anchor='w', pady=4)

        body = tk.Text(window, wrap='word', padx=10, pady=10)
        body.pack(fill='both', expand=True, padx=10, pady=5)

        lines = []
        if integrity == 'DEGRADED' or degraded:
            lines.append("【阶段降级警告】")
            lines.append("以下处理阶段在运行中捕获异常并自动降级，已安全保留原音频，未写入破坏数据：\n")
            for deg in degraded:
                lines.append(f"• 阶段：{deg.get('name', deg.get('stage'))}")
                lines.append(f"  - 预定任务：{deg.get('intended_action', '未知')}")
                lines.append(f"  - 降级原因：{deg.get('reason', '未知异常')}")
                lines.append(f"  - 音频保护：{'已保留前阶段有效音频' if deg.get('source_preserved', True) else '未确认'}")
                lines.append(f"  - 重试建议：{'可重新运行该阶段' if deg.get('retryable', True) else '不可自动重试'}\n")
        else:
            lines.append("【处理完整度正常】全部阶段完整执行，无异常降级。\n")

        issues = qc.get('issues') or []
        failures = qc.get('failures') or []
        if failures:
            lines.append("【客观阻断失败 (Failures)】")
            for f in failures:
                lines.append(f"• {f}")
            lines.append("")
        if issues:
            lines.append("【客观警告与提示 (Issues)】")
            for iss in issues:
                lines.append(f"• {iss}")
            lines.append("")

        body.insert('1.0', '\n'.join(lines))
        body.config(state='disabled')

        btn_bar = ttk.Frame(window, padding=10)
        btn_bar.pack(fill='x')
        ttk.Button(btn_bar, text='打开完整 Markdown 报告', command=self.open_report).pack(side='left')
        ttk.Button(btn_bar, text='关闭', command=window.destroy).pack(side='right')

    def show_logs_dialog(self):
        """打开运行日志弹窗，并提供一键定位日志文件与日志目录入口"""
        win = tk.Toplevel(self)
        win.title('运行日志与诊断')
        win.geometry('720x520')
        win.transient(self)
        win.minsize(500, 350)

        top_frame = ttk.Frame(win, padding=(10, 8))
        top_frame.pack(fill='x')
        last_err = getattr(self, 'last_failure_detail', None)
        if last_err:
            ttk.Label(top_frame, text='最近错误诊断：', font=('', 9, 'bold'), foreground='red').pack(anchor='w')
            err_box = tk.Text(top_frame, height=3, wrap='word', foreground='darkred', relief='groove')
            err_box.insert('1.0', str(last_err))
            err_box.configure(state='disabled')
            err_box.pack(fill='x', pady=(2, 6))
        else:
            ttk.Label(top_frame, text='运行日志记录（包含处理过程、调试信息与阶段状态）：').pack(anchor='w')

        body_frame = ttk.Frame(win, padding=(10, 0))
        body_frame.pack(fill='both', expand=True)
        text = tk.Text(body_frame, wrap='word', height=16)
        scroll = ttk.Scrollbar(body_frame, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(side='left', fill='both', expand=True)

        log_content = self.logbox.get('1.0', 'end').strip()
        if not log_content and last_err:
            log_content = str(last_err)
        if not log_content:
            log_content = '（暂无日志记录）'
        text.insert('1.0', log_content)
        text.see('end')

        btn_row = ttk.Frame(win, padding=(10, 10))
        btn_row.pack(fill='x')

        def copy_to_clipboard():
            self.clipboard_clear()
            self.clipboard_append(text.get('1.0', 'end'))
            messagebox.showinfo('提示', '日志内容已复制到剪贴板', parent=win)

        def open_log_dir():
            from core.config import LOG_DIR
            if os.path.isdir(LOG_DIR):
                os.startfile(LOG_DIR)
            else:
                os.makedirs(LOG_DIR, exist_ok=True)
                os.startfile(LOG_DIR)

        def open_project_dir():
            manifest = self.selected_manifest()
            target_dir = None
            if manifest:
                master = manifest.get('output_paths', {}).get('master_wav')
                if master:
                    target_dir = os.path.dirname(os.path.dirname(master))
            if not target_dir:
                job = self.selected_job() if hasattr(self, 'selected_job') else None
                if job and job.get('project_dir'):
                    target_dir = job.get('project_dir')
            if not target_dir:
                target_dir = self.output.get()
            if target_dir and os.path.isdir(target_dir):
                os.startfile(target_dir)
            else:
                messagebox.showinfo('提示', '未找到当前选中的任务产物目录', parent=win)

        ttk.Button(btn_row, text='复制日志', command=copy_to_clipboard).pack(side='left', padx=4)
        ttk.Button(btn_row, text='打开日志目录', command=open_log_dir).pack(side='left', padx=4)
        ttk.Button(btn_row, text='打开当前任务目录', command=open_project_dir).pack(side='left', padx=4)
        ttk.Button(btn_row, text='关闭', command=win.destroy).pack(side='right', padx=4)

    def open_output(self):
        manifest=self.selected_manifest()
        if not manifest:
            messagebox.showinfo('未选择结果','请在队列或历史中选择一个已完成任务')
            return
        master=manifest.get('output_paths',{}).get('master_wav')
        path=os.path.dirname(os.path.dirname(master)) if master else self.output.get()
        if os.path.isdir(path):os.startfile(path)
        else:messagebox.showerror('结果目录不存在',path)
    def play(self,kind):
        manifest=self.selected_manifest()
        if not manifest:
            messagebox.showinfo('未选择结果','请在队列或历史中选择一个已完成任务')
            return
        path=manifest.get('source_path') if kind=='source' else manifest.get('output_paths',{}).get('master_wav')
        if path and os.path.isfile(path):os.startfile(path)
        else:messagebox.showerror('音频不存在',path or '未记录路径')

    def analyze_selected_voice_range(self):
        if self.voice_range_running:
            return
        source_path=None
        project_dir=None
        source_kind='mixed_music'
        try:
            if self.result_surface=='history':
                job=self.selected_job()
                if job:
                    project_dir=job.get('project_dir')
                    stage=self.pipeline.storage.get_stage(job['id'],'Separate')
                    vocal=(stage or {}).get('outputs',{}).get('vocal')
                    if isinstance(vocal,str) and os.path.isfile(vocal):
                        source_path=vocal
                        source_kind='isolated_vocal'
                    else:
                        source_path=job.get('source_path')
            else:
                keys=self.tree.selection()
                if keys:
                    item=self.pending.get(keys[0]) or {}
                    source_path=item.get('path')
                    manifest=self.result_manifests.get(keys[0])
                    if manifest:
                        source_path=manifest.get('source_path') or source_path
                        project_dir=os.path.dirname(os.path.dirname(
                            (manifest.get('output_paths') or {}).get('master_wav','')))
                        job_id=manifest.get('job_id')
                        stage=self.pipeline.storage.get_stage(job_id,'Separate') if job_id else None
                        vocal=(stage or {}).get('outputs',{}).get('vocal')
                        if isinstance(vocal,str) and os.path.isfile(vocal):
                            source_path=vocal
                            source_kind='isolated_vocal'
            if not source_path or not os.path.isfile(source_path):
                messagebox.showinfo('未选择音频','请先选择队列文件或历史任务')
                return
            report_path=None
            if project_dir and os.path.isdir(project_dir):
                report_path=os.path.join(project_dir,'reports','voice_range.json')
            self.voice_range_running=True
            if self.voice_range_button is not None:
                self.voice_range_button.configure(state='disabled')
            self.status.config(text='正在后台观察音高；不会修改音频或半音设置')
            def worker(path=source_path,kind=source_kind,report=report_path):
                try:
                    from core.voice_range import analyze_voice_range
                    value=analyze_voice_range(path,source_kind=kind,target_range=None,report_path=report)
                    self.events.put(('voice_range',(kind,value,None)))
                except Exception as exc:
                    self.events.put(('voice_range',(kind,None,str(exc))))
            self._start_background_worker(worker,daemon=True,name='voice-range-observation')
        except Exception as exc:
            self.voice_range_running=False
            if self.voice_range_button is not None:
                self.voice_range_button.configure(state='normal')
            messagebox.showerror('音域分析无法启动',str(exc))

    def preview_selected_waveform(self):
        if self.waveform_running:
            return
        source_path=None
        manifest=self.selected_manifest()
        if manifest:
            master=(manifest.get('output_paths') or {}).get('master_wav')
            source_path=master if master and os.path.isfile(master) else manifest.get('source_path')
        if not source_path:
            if self.result_surface=='history':
                job=self.selected_job()
                source_path=job.get('source_path') if job else None
            else:
                keys=self.tree.selection()
                item=self.pending.get(keys[0]) if keys else None
                source_path=(item or {}).get('path')
        if not source_path or not os.path.isfile(source_path):
            messagebox.showinfo('未选择音频','请先选择队列文件或历史任务')
            return
        self.waveform_token+=1
        token=self.waveform_token
        window=tk.Toplevel(self)
        window.title('波形预览')
        window.geometry('760x300')
        self.waveform_window=window
        ttk.Label(window,text=os.path.basename(source_path)+' · 预览采样 1 kHz 单声道包络').pack(anchor='w',padx=12,pady=8)
        canvas=tk.Canvas(window,height=210,background='#111827',highlightthickness=0)
        canvas.pack(fill='both',expand=True,padx=12,pady=(0,10))
        self.waveform_canvas=canvas
        canvas.create_text(12,18,anchor='w',fill='#cbd5e1',text='正在解码波形…')
        self.waveform_running=True
        def worker(path=source_path,request=token):
            try:
                from core.waveform_preview import waveform_envelope
                value=waveform_envelope(path)
                self.events.put(('waveform',(request,value,None)))
            except Exception as exc:
                self.events.put(('waveform',(request,None,str(exc))))
        self._start_background_worker(worker,daemon=True,name='waveform-preview')

    def _draw_waveform_error(self,message):
        canvas=self.waveform_canvas
        if canvas is not None and canvas.winfo_exists():
            canvas.delete('all')
            canvas.create_text(14,22,anchor='w',fill='#fca5a5',text='无法生成预览：'+message,width=700)

    def _draw_waveform(self,record):
        canvas=self.waveform_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete('all')
        canvas.update_idletasks()
        width=max(100,canvas.winfo_width())
        height=max(80,canvas.winfo_height())
        mid=height/2
        peak=max(float(record.get('peak') or 0),1e-5)
        points=record.get('points') or []
        for index,(low,high) in enumerate(points):
            x=12+(width-24)*index/max(1,len(points)-1)
            y1=mid-(float(high)/peak)*(height*.42)
            y2=mid-(float(low)/peak)*(height*.42)
            canvas.create_line(x,y1,x,y2,fill='#38bdf8',width=1)
        canvas.create_line(12,mid,width-12,mid,fill='#475569')
        canvas.create_text(14,12,anchor='nw',fill='#cbd5e1',
                           text='%.1f 秒 · 振幅缩放到本段峰值；仅作预览，不代表音质验收'%record.get('duration_seconds',0))
    def show_stage_trace(self):
        manifest=self.selected_manifest()
        if not manifest:
            messagebox.showinfo('未选择结果','请在队列或历史中选择一个已完成任务')
            return
        from core.stage_quality_trace import (
            audition_rows, diagnose_stages, load_trace, project_dir_from_manifest, write_listening_marks,
        )
        project=project_dir_from_manifest(manifest)
        trace_path=os.path.join(project,'reports','stage_quality_trace.json') if project else ''
        if not trace_path or not os.path.isfile(trace_path):
            messagebox.showinfo('没有阶段追踪','这个任务还没有阶段质量追踪。完成一次换声后会写到 reports。')
            return
        try:
            trace=load_trace(trace_path)
        except (OSError,ValueError) as exc:
            messagebox.showerror('阶段追踪无法读取',str(exc))
            return
        window=tk.Toplevel(self)
        window.title('换声质量诊断')
        window.geometry('760x640')
        diagnosis=trace.get('diagnosis') or {}
        summary=tk.Label(window,text=diagnosis.get('conclusion') or '没有结论',wraplength=720,justify='left')
        summary.pack(anchor='w',padx=12,pady=8)
        marks={}
        body=ttk.Frame(window)
        body.pack(fill='both',expand=True,padx=12)
        for row in audition_rows(trace):
            line=ttk.Frame(body)
            line.pack(fill='x',pady=2)
            ttk.Label(line,text=row['label'],width=22).pack(side='left')
            if row.get('official', True):
                choice=tk.StringVar(master=self,value='未听')
                marks[row['id']]=choice
                ttk.Combobox(line,textvariable=choice,values=('未听','正常','变差'),state='readonly',width=8).pack(side='left',padx=4)
            else:
                ttk.Label(line,text='未采用的分离输出').pack(side='left',padx=4)
            def play_row(path=row['path'],present=row['present']):
                if present:os.startfile(path)
                else:messagebox.showinfo('这一阶段没有文件',path or '未记录路径')
            ttk.Button(line,text='试听',command=play_row).pack(side='left')
            note=row['note']
            if note:ttk.Label(line,text=note).pack(side='left',padx=6)
        def apply_marks(save=False):
            translated={'未听':'not_assessed','正常':'ok','变差':'degraded'}
            labels={key:translated[var.get()] for key,var in marks.items()}
            try:
                result=diagnose_stages(trace['stages'],labels)
            except ValueError as exc:
                messagebox.showerror('标记无效',str(exc))
                return
            summary.config(text=result['conclusion'])
            if save and project:
                write_listening_marks(project,labels,trace['stages'])
                self.log('已保存听审标记。音质验收仍是 NOT_ASSESSED。')
        bar=ttk.Frame(window)
        bar.pack(fill='x',padx=12,pady=8)
        ttk.Button(bar,text='按标记判断',command=apply_marks).pack(side='left')
        ttk.Button(bar,text='保存标记',command=lambda:apply_marks(True)).pack(side='left',padx=6)
    def editor(self,kind):
        manifest=self.selected_manifest()
        paths=manifest.get('output_paths',{}) if manifest else {}
        midi=next(iter(Path(paths['midi_dir']).glob('*.mid')),None) if paths.get('midi_dir') else None
        audio=paths.get('master_wav')
        if kind=='neuralnote':
            audio=manifest.get('source_path') if manifest else None
            if audio and not os.path.isfile(audio):audio=None
            try:success=launch_neuralnote(audio)
            except ValueError as error:
                messagebox.showerror('外部编辑器路径无效',str(error));return
            if success:self.status.config(text='NeuralNote 已启动；扒谱导入状态请在 NeuralNote 内确认')
            else:messagebox.showerror('工具缺失或输入无效','诊断页检查 NeuralNote 路径')
            return
        if kind in ('musescore','lmms') and not midi:
            messagebox.showinfo('尚无 MIDI','使用扒谱模式，或勾选换声同时扒谱');return
        if kind=='audacity' and not audio:
            messagebox.showinfo('尚无成品','请先完成一个任务');return
        valid,reason=validate_editor_input(kind, str(midi) if kind in ('musescore','lmms') else audio)
        if not valid:
            messagebox.showerror('导入输入无效',reason);return
        try:
            success={'audacity':lambda:open_in_audacity(audio),'musescore':lambda:open_in_musescore(str(midi)),
                     'lmms':lambda:open_in_lmms(str(midi))}[kind]()
        except ValueError as error:
            messagebox.showerror('乐谱导入未完成',str(error));return
        if not success:
            messagebox.showerror('工具缺失或启动失败','诊断页检查组件路径')
        elif kind=='lmms':
            self.status.config(text='LMMS 已启动；已提交保留时序的独立工程，载入完成请在 LMMS 内确认')
        elif kind=='musescore':
            self.status.config(text=f'{kind} 已启动；已提交 MIDI 导入，导入完成请在外部应用确认')
        else:
            self.status.config(text='Audacity 已启动；音频已提交，项目载入完成请在外部应用确认')
    def refresh_models(self):
        try:
            self.models=get_available_rvc_models()
        except Exception as exc:
            self.log('模型扫描失败：'+str(exc))
            self.models={}
        self.model_tree.delete(*self.model_tree.get_children())
        inspections=[]
        self.model_inspection_reports={}
        for key,info in self.models.items():
            p=info['profile']
            path=info.get('pth')
            signature=self._model_stat_signature(path) if path else None
            cached=self.model_inspection_cache.get(path) if path else None
            report=None
            if cached and cached[0]==signature:
                report,error=cached[1],cached[2]
                if report is not None:
                    self.model_inspection_reports[key]=report
                availability=self._model_availability_text(report,error)
            else:
                availability='安全检查中…' if signature else '文件缺失'
                if path and signature and path not in self.model_inspection_pending:
                    self.model_inspection_pending.add(path)
                    inspections.append((key,path,signature,info.get('index'),dict(info)))
            warning='；'.join(info.get('profile_warnings',[]))
            licenses=self.pipeline.storage.list_licenses('model',key)
            license_value=licenses[0]['commercial_status'] if len(licenses)==1 else 'Unknown'
            if warning:
                license_value='配置警告'
                self.log(warning)
            self.model_tree.insert('','end',iid=key,values=(info['name'],availability,p.get('f0_method','harvest'),'有' if info['index'] else '无',license_value))
        if inspections and not self.model_inspection_running and (self.__dict__.get('runtime_gate') is None or self.runtime_gate.ready):
            self.model_inspection_running=True
            def inspect_models(batch):
                try:
                    from core.rvc_model_inspector import read_checkpoint,read_index,inspect_model
                    for key,path,signature,index_path,model_info in batch:
                        try:
                            checkpoint=read_checkpoint(path)
                            index_facts=(read_index(index_path) if index_path else
                                         {'readable':False,'dimension':None,'count':None,'bytes':None,'reason':'index not installed'})
                            report=inspect_model(key,model_info,checkpoint,index_facts)
                            error=None
                        except Exception as exc:
                            report=None
                            error=type(exc).__name__+': '+str(exc)
                        self.events.put(('model_inspection',(key,path,signature,report,error)))
                finally:
                    self.events.put(('model_inspection_done',None))
            self._start_background_worker(inspect_models,args=(inspections,),daemon=True,
                                          name='rvc-model-safe-inspection')

    @staticmethod
    def _model_stat_signature(path):
        try:
            info=os.stat(path)
            return (os.path.normcase(os.path.realpath(path)),info.st_size,
                    getattr(info,'st_mtime_ns',int(info.st_mtime*1e9)),
                    getattr(info,'st_ctime_ns',int(info.st_ctime*1e9)),getattr(info,'st_ino',None))
        except (OSError,TypeError):
            return None

    @staticmethod
    def _model_availability_text(report,error=None):
        if error:
            return '安全检查失败'
        if not isinstance(report,dict) or not isinstance(report.get('checkpoint'),dict):
            return '安全检查失败'
        checkpoint=report['checkpoint']
        if not checkpoint.get('readable'):
            return '不可用 · 安全解析拒绝'
        if checkpoint.get('inference_compatible') is not True:
            return '文件存在 · 权重不完整/不兼容'
        index=report.get('index') or {}
        if index.get('dimension_relation')=='DIMENSION_MISMATCH':
            return '可推理 · 索引维度不匹配'
        if index.get('readable'):
            return '可推理 · 索引已安全验证'
        return '可推理 · 无可用索引'

    def _rvc_model_run_guard(self,params):
        key=params.get('rvc_model_key')
        info=self.models.get(key,{})
        path=info.get('pth')
        signature=self._model_stat_signature(path) if path else None
        cached=self.model_inspection_cache.get(path) if path else None
        if not cached or cached[0]!=signature:
            return '所选模型仍在后台安全检查；完成后请刷新模型状态再开始。'
        report,error=cached[1],cached[2]
        if error or not isinstance(report,dict):
            return '所选模型无法完成安全检查：'+str(error or '无检查报告')
        checkpoint=report.get('checkpoint') or {}
        if checkpoint.get('inference_compatible') is not True:
            return '文件存在不代表可推理；所选权重未通过完整 RVC 形状与资源预算验证。'
        index=report.get('index') or {}
        if params.get('index_rate',0)>0 and info.get('index') and (
                not index.get('readable') or index.get('dimension_relation')=='DIMENSION_MISMATCH'):
            return '所选索引未通过受限解析或维度不匹配；请修复索引或把 Index 设为 0。'
        return None

    def import_user_model(self,source=None):
        reason=self._audio_pipeline_busy_reason()
        if reason or self.__dict__.get('component_install_running') or self.__dict__.get('model_import_running'):
            message=reason or '托管组件更新或模型导入正在进行中'
            self.status.config(text=message)
            self.log(message)
            return
        if source is None:
            source=filedialog.askopenfilename(
                title='安全导入用户 RVC 权重或索引',
                filetypes=[('RVC 模型 / 索引','*.pth *.index')])
        if not source:
            return
        source=os.path.abspath(os.fspath(source))
        if os.path.splitext(source)[1].lower() not in ('.pth','.index'):
            messagebox.showerror('模型格式','只支持 .pth 和受限 Flat/IVFFlat .index')
            return
        if not self._abort_scheduled_shutdown('安全导入用户模型'):
            self.status.config(text='无法取消之前安排的关机；已停止模型导入')
            return
        self.model_import_running=True
        self._refresh_component_mutation_controls()
        self.status.config(text='正在后台安全验证并导入用户模型…')
        def task():
            result=None
            error=None
            try:
                from core.install_platform import import_user_model
                from core.config import RVC_ROOT
                managed_dir=os.path.join(RVC_ROOT,'weights')
                managed_names=os.listdir(managed_dir) if os.path.isdir(managed_dir) else []
                result=import_user_model(source,os.path.basename(source),
                    os.path.join(BASE_DIR,'models','rvc'),managed_dir,managed_names)
            except Exception as exc:
                error=type(exc).__name__+': '+str(exc)
            self.events.put(('model_import',(result,error)))
        self._start_background_worker(task,daemon=True,name='safe-user-rvc-import')

    def apply_profile(self,event=None):
        info=self.models.get(self.model.get())
        if info:
            p=info['profile'];self.pitch.set(p.get('pitch',0));self.index.set(p.get('index_rate',.65))
            self.f0.set(p.get('f0_method','harvest'))
    def save_profile(self):
        try:p=self.parameters()
        except Exception as exc:messagebox.showerror('参数',str(exc));return
        info=self.models.get(self.model.get())
        if not info:return
        profile=dict(info['profile'],pitch=p['pitch_shift'],f0_method=p['f0_method'],index_rate=p['index_rate'])
        try:
            save_model_profile(info['profile_path'],profile)
        except Exception as exc:
            messagebox.showerror('Profile 保存失败',str(exc));return
        self.refresh_models()
    def record_license(self):
        """Record evidence for one explicit subject; never infer project rights."""
        subject_type=simpledialog.askstring(
            '授权主体类型','请输入 model / voice / input / sample：',parent=self)
        if not subject_type:return
        subject_type=subject_type.strip().lower()
        if subject_type not in {'model','voice','input','sample'}:
            messagebox.showerror('授权主体类型','只支持 model、voice、input、sample');return
        subject_id=''
        if subject_type in {'model','voice'}:
            subject_id=self.model.get().strip()
            if not subject_id:
                messagebox.showerror('授权主体','请先选择模型');return
        elif subject_type=='input':
            manifest=self.selected_manifest()
            source=manifest.get('source_path') if manifest else None
            if not source:
                job=self.selected_job();source=job.get('source_path') if job else None
            if not source or not os.path.isfile(source):
                messagebox.showerror('输入主体','请先在队列或历史中选择存在的源文件');return
            try:
                from core.storage import sha256_file
                subject_id=sha256_file(source)
            except Exception as exc:
                messagebox.showerror('输入校验失败',str(exc));return
            self.log('input 授权主体使用源文件 SHA256：'+subject_id)
        else:
            subject_id=(simpledialog.askstring('sample 主体 ID','请输入样本档案 ID：',parent=self) or '').strip()
            if not subject_id:return
        status=(simpledialog.askstring('商用状态','请输入 Ready / Warning / Unknown：',
                                       initialvalue='Unknown',parent=self) or 'Unknown').strip().title()
        if status not in {'Ready','Warning','Unknown'}:
            messagebox.showerror('商用状态','只支持 Ready、Warning、Unknown');return
        fields={}
        for key,label in [('name','名称'),('license_name','许可证'),('source_url','来源 URL'),
                          ('author','作者 / 权利人'),('note','证据备注')]:
            fields[key]=(simpledialog.askstring('授权记录',label+'（可留空）：',parent=self) or '').strip() or None
        try:
            self.pipeline.storage.put_license(subject_type,subject_id,
                commercial_status=status,**fields)
        except Exception as exc:
            messagebox.showerror('授权记录保存失败',str(exc));return
        self.refresh_models()
        self.status.config(text=f'已记录 {subject_type} 主体授权；它不会自动推导其他主体或整项目可商用')
    def open_profiles(self):
        path=os.path.join(CONFIG_DIR,'model-profiles');os.makedirs(path,exist_ok=True);os.startfile(path)
    def export_configuration_backup(self):
        from core.settings_backup import export_settings_backup
        target=filedialog.asksaveasfilename(defaultextension='.ampbackup',filetypes=[('配置备份','*.ampbackup')])
        if not target:return
        try:
            profiles={}
            for path in Path(CONFIG_DIR,'model-profiles').glob('*.json'):
                if path.stat().st_size>256*1024:raise ValueError('Profile 过大：'+path.name)
                profiles[path.stem]=json.loads(path.read_text(encoding='utf-8-sig'))
            settings=dict(self.settings);settings.update(self.parameters())
            external_path=Path(CONFIG_DIR,'external-editors.json')
            if external_path.is_file():
                if external_path.stat().st_size>64*1024:raise ValueError('外部编辑器设置过大')
                settings['external_editor_locations']=json.loads(external_path.read_text(encoding='utf-8'))
            selection_path=Path(CONFIG_DIR,'component-selection.json')
            selection=json.loads(selection_path.read_text(encoding='utf-8')) if selection_path.is_file() else ['mode.'+self.mode.get()]
            metadata={key:{field:value for field,value in model.items() if field in
                ('name','gender','profile','artifact_hashes','original_pth','original_index','pth','index')}
                for key,model in self.models.items()}
            result=export_settings_backup(target,settings,profiles,selection,metadata)
            self.status.config(text='配置备份已导出；不包含模型权重：'+result['path'])
        except Exception as error:messagebox.showerror('配置备份失败',str(error))
    def import_configuration_backup(self):
        from core.settings_backup import plan_settings_restore,restore_settings_backup
        if self._audio_pipeline_busy_reason():
            messagebox.showinfo('配置恢复',self._audio_pipeline_busy_reason());return
        source=filedialog.askopenfilename(filetypes=[('配置备份','*.ampbackup')])
        if not source:return
        try:
            plan=plan_settings_restore(source,CONFIG_DIR)
            summary='\n'.join(row['relative_path'] for row in plan['changes']) or '没有文件变化'
            if not messagebox.askyesno('检查配置恢复',summary+'\n\n原配置将保留备份。模型权重不会导入；恢复后重启生效。'):
                return
            result=restore_settings_backup(source,CONFIG_DIR,expected_plan=plan)
            self.status.config(text='配置已恢复，重启后生效；原配置保存在 '+result['backup_directory'])
        except Exception as error:messagebox.showerror('配置恢复失败',str(error))
    def choose_external_editor(self):
        from core.component_location import save_editor_location
        path=filedialog.askopenfilename(filetypes=[('Windows 程序','*.exe')])
        if not path:return
        try:
            result=save_editor_location(CONFIG_DIR,self.external_editor_choice.get(),path)
            self.status.config(text='外部编辑器路径已保存并核验 PE 文件：'+result['path']+'；导入状态以编辑器为准')
        except Exception as error:messagebox.showerror('编辑器路径无效',str(error))
    def install_into_new_location(self):
        from core.component_location import install_verified_bundle
        from core.paths import is_release_mode
        if not is_release_mode(BASE_DIR):
            messagebox.showinfo('完整包安装','请先生成 Portable 包，并从已通过运行门禁的完整包启动后选择新位置');return
        if self._audio_pipeline_busy_reason() or getattr(self,'new_install_running',False):
            messagebox.showinfo('完整包安装','请等待当前处理或安装结束');return
        parent=filedialog.askdirectory(title='选择新安装位置的父目录')
        if not parent:return
        target=Path(parent)/('AI-Music-Processor-'+uuid.uuid4().hex[:8])
        self.new_install_running=True
        self.status.config(text='正在校验并复制完整包；目标为 '+str(target))
        def task():
            result,error=None,None
            try:result=install_verified_bundle(BASE_DIR,target)
            except Exception as exc:error=exc
            self.events.put(('new_install',(result,error)))
        self._start_background_worker(task,daemon=True,name='portable-install')
    def run_separator_experiment(self):
        from core.separator_benchmark import run_ensemble_experiment
        if self._audio_pipeline_busy_reason() or getattr(self,'separator_experiment_running',False):
            messagebox.showinfo('候选实验','请等待当前音频处理或候选实验结束');return
        source=filedialog.askopenfilename(title='选择候选比较的同一源音频')
        if not source:return
        target=Path(self.output.get())/'separator-experiments'/uuid.uuid4().hex
        tta=bool(self.candidate_tta.get())
        self.separator_experiment_running=True
        self.status.config(text='正在生成 HP2、双模型和组合独立候选；产物与实验记录保存在 '+str(target))
        def task():
            result,error=None,None
            try:
                experiment=run_ensemble_experiment(source,target,device='cpu',hp2_tta=tta)
                result={'manifest_path':str(target/'experiment.json'),'status':experiment['status']}
            except Exception as exc:error=exc
            self.events.put(('separator_experiment',(result,error)))
        self._start_background_worker(task,daemon=True,name='separator-candidate-experiment')
    def show_ab_preview(self):
        from core.ab_preview import project_audio_roles,prepare_ab_preview,stop_ab_preview
        try:roles=project_audio_roles(self.selected_manifest())
        except Exception as error:messagebox.showinfo('A/B 试听',str(error));return
        previous=getattr(self,'ab_window',None)
        if previous is not None and previous.winfo_exists():
            dispose_tk_variables(previous);previous.destroy()
        stop_ab_preview()
        window=tk.Toplevel(self);window.title('同起点 A/B 片段试听');window.geometry('700x250')
        self.ab_window=window;self.ab_token=uuid.uuid4().hex
        self.ab_preparing=bool(getattr(self,'ab_preparing',False))
        a=tk.StringVar(window,value='原始输入' if '原始输入' in roles else next(iter(roles)))
        b=tk.StringVar(window,value='最终母带' if '最终母带' in roles else list(roles)[1])
        start=tk.StringVar(window,value='0')
        ttk.Label(window,text='原始输入、母带及实际 Stage 音频；每次切换从相同起点开始，最多 30 秒。').pack(anchor='w',padx=12,pady=8)
        ttk.Label(window,text='不做响度补偿，不据此自动评价音质。').pack(anchor='w',padx=12)
        row=ttk.Frame(window);row.pack(fill='x',padx=12,pady=8)
        for text,var in [('A',a),('B',b)]:
            ttk.Label(row,text=text).pack(side='left',padx=4)
            ttk.Combobox(row,textvariable=var,values=list(roles),state='readonly',width=25).pack(side='left')
        position=ttk.Frame(window);position.pack(fill='x',padx=12)
        ttk.Label(position,text='共同起点（秒）').pack(side='left')
        ttk.Entry(position,textvariable=start,width=10).pack(side='left',padx=6)
        self.ab_status=ttk.Label(window,text='选择 A 或 B 开始试听；停止按钮会结束当前片段')
        self.ab_status.pack(anchor='w',padx=12,pady=4)
        match_loudness_var=tk.BooleanVar(window,value=True)
        ttk.Checkbutton(window,text='启用 A/B 响度匹配（避免“更响显得更好听”偏差）',variable=match_loudness_var).pack(anchor='w',padx=12,pady=(0,4))
        buttons=ttk.Frame(window);buttons.pack(fill='x',padx=12)
        def play(side,var):
            if self.ab_preparing:return
            try:
                offset=float(start.get());path=roles[var.get()]
                if not __import__('math').isfinite(offset) or offset<0:raise ValueError('起点必须是非负有限数')
            except Exception as error:self.ab_status.configure(text=str(error));return
            self.ab_preparing=True;token=self.ab_token;self.ab_worker_token=token
            self.ab_status.configure(text='正在准备片段；源音频不会修改')
            target_rms=getattr(self,'ab_last_rms',None) if match_loudness_var.get() else None
            def task():
                record,error=None,None
                try:
                    record=prepare_ab_preview(path,Path(TEMP_DIR)/'ab-previews',offset,30.,target_rms=target_rms)
                    if record and record.get('rms'):self.ab_last_rms=record['rms']
                except Exception as exc:error=exc
                self.events.put(('ab_preview',(token,side,record,error)))
            self._start_background_worker(task,daemon=True,name='ab-preview')
        ttk.Button(buttons,text='试听 A',command=lambda:play('A',a)).pack(side='left')
        ttk.Button(buttons,text='试听 B',command=lambda:play('B',b)).pack(side='left',padx=6)
        def stop():
            stop_ab_preview();self.ab_token=uuid.uuid4().hex
            self.ab_status.configure(text='已停止；下次试听仍从共同起点开始')
        ttk.Button(buttons,text='停止',command=stop).pack(side='left')
        def record_eval():
            from core.polish_listening import DEFECT_TAGS,create_listening_evaluation,save_listening_evaluation
            eval_win=tk.Toplevel(window);eval_win.title('记录人工听审评价');eval_win.geometry('420x360')
            ttk.Label(eval_win,text='试听者姓名 / 标识:').pack(anchor='w',padx=12,pady=(8,2))
            listener_entry=ttk.Entry(eval_win);listener_entry.insert(0,'Tester');listener_entry.pack(fill='x',padx=12)
            ttk.Label(eval_win,text='主观偏好 (Blind Choice):').pack(anchor='w',padx=12,pady=(6,2))
            pref_var=tk.StringVar(eval_win,value='A')
            pref_frame=ttk.Frame(eval_win);pref_frame.pack(fill='x',padx=12)
            for val,txt in [('A','偏好 A'),('B','偏好 B'),('EQUAL','两者相近'),('NEITHER','均不合格')]:
                ttk.Radiobutton(pref_frame,text=txt,value=val,variable=pref_var).pack(side='left',padx=2)
            ttk.Label(eval_win,text='观察到的问题标签 (可多选):').pack(anchor='w',padx=12,pady=(6,2))
            defect_vars={}
            defect_labels={'distortion':'破音/过载','robotic':'电音/机械感','sibilance':'齿音过重','bleed':'串音残留','muffled':'发闷','sync_drift':'节奏不同步'}
            tag_frame=ttk.Frame(eval_win);tag_frame.pack(fill='x',padx=12)
            for idx,(tag,label) in enumerate(defect_labels.items()):
                v=tk.BooleanVar(eval_win,value=False);defect_vars[tag]=v
                ttk.Checkbutton(tag_frame,text=label,variable=v).grid(row=idx//2,column=idx%2,sticky='w',padx=6,pady=2)
            ttk.Label(eval_win,text='评价备注:').pack(anchor='w',padx=12,pady=(6,2))
            notes_entry=ttk.Entry(eval_win);notes_entry.pack(fill='x',padx=12)
            def save_eval():
                try:
                    import datetime
                    case_id=(self.selected_manifest() or {}).get('job_id') or 'current_job'
                    selected_tags=[t for t,v in defect_vars.items() if v.get()]
                    record=create_listening_evaluation(case_id=case_id,listener=listener_entry.get(),
                        date=datetime.date.today().isoformat(),choice_a_role=a.get(),choice_b_role=b.get(),
                        preferred_choice=pref_var.get(),defect_tags=selected_tags,notes=notes_entry.get())
                    saved_path=save_listening_evaluation(record,USER_DATA_DIR)
                    eval_win.destroy()
                    self.ab_status.configure(text='已保存人工听审评价: '+Path(saved_path).name)
                except Exception as ex:messagebox.showerror('保存听审评价失败',str(ex))
            ttk.Button(eval_win,text='保存听审记录',command=save_eval).pack(pady=10)
        ttk.Button(buttons,text='记录听审评价',command=record_eval).pack(side='left',padx=6)
        def close_ab():
            stop();dispose_tk_variables(window);window.destroy();self.ab_window=None
        window.protocol('WM_DELETE_WINDOW',close_ab)
    def save_preset(self):
        try:p=self.parameters()
        except Exception as exc:messagebox.showerror('参数',str(exc));return
        path=filedialog.asksaveasfilename(initialdir=CONFIG_DIR,defaultextension='.json')
        if path:
            with open(path,'w',encoding='utf-8') as f:json.dump(p,f,ensure_ascii=False,indent=2)
    def import_preset(self):
        path=filedialog.askopenfilename(filetypes=[('JSON','*.json')])
        if path:
            try:
                with open(path,encoding='utf-8-sig') as f:self.apply_params(json.load(f))
            except Exception as exc:messagebox.showerror('预设错误',str(exc))
    def refresh_mode_flow(self):
        if not self.__dict__.get('mode_flow'):
            return
        for child in self.mode_flow.winfo_children():
            child.destroy()
        running=self.__dict__.get('running_flow')
        if self.__dict__.get('is_running') and isinstance(running,dict):
            from core.ux_flow import queue_flow_options
            self.mode_flow_panel=build_mode_flow_panel(self.mode_flow,running['mode'],
                **queue_flow_options(running['mode'],running['parameters'],running['frozen']))
            self.mode_flow_panel.pack(fill='x')
            return
        midi=bool(self.midi.get()) if self.mode.get()=='voice_conversion' else False
        self.mode_flow_panel=build_mode_flow_panel(
            self.mode_flow, self.mode.get(), generate_midi=midi,
            source_preclean=bool(self.source_preclean_enabled.get()),
            vocal_restore=(self.mode.get()=='voice_conversion' or
                           self.vocal_restore_mode.get()!='off' or
                           self.vocal_restoration_effect.get()!='off'),
            parameter_search=bool(self.parameter_search_enabled.get()),
            # New frozen jobs always carry these optional policies, including
            # explicit Off. Show their stage slots so live SKIPPED is visible.
            pitch_correction=self.mode.get()=='voice_conversion',
            midi_cleanup=(self.mode.get()=='transcribe' or
                          (self.mode.get()=='voice_conversion' and midi)),
            mix_ducking=bool(self.mix_ducking.get()) if self.mode.get()=='voice_conversion' else None)
        self.mode_flow_panel.pack(fill='x')
        self._apply_polish_visibility()
        session=self.__dict__.get('tutorial_session')
        frame=self.__dict__.get('tutorial_frame')
        if session is not None:
            session.set_mode(self.mode.get())
            if frame is not None:
                frame.refresh()
    def highlight_running_stage(self, stage, message=None):
        panel=self.__dict__.get('mode_flow_panel')
        if panel is not None:
            highlight_mode_stage(panel, stage, message)
    def _save_onboarding(self, **flags):
        from core.ux_flow import remember_onboarding
        self.settings=remember_onboarding(self.settings, **flags)
        save_settings(self.settings)
        return self.settings

    def _audio_pipeline_busy_reason(self):
        if self.__dict__.get('runtime_check_running',False):
            return '运行环境正在后台校验，请完成后再处理或变更组件'
        for flag,message in (
                ('component_install_running','离线组件正在安装或修复'),
                ('component_size_running','组件体积正在核验'),
                ('cache_collect_running','缓存回收正在核验源引用')):
            if self.__dict__.get(flag,False):return message+'，请完成后再执行其它维护或音频操作'
        scan=self.__dict__.get('scan_thread')
        if scan is not None:return '文件夹扫描结果尚未完成接收，请完成后再执行维护或音频操作'
        if self.__dict__.get('version_update_running',False):
            return '版本更新或回退正在验证，请完成或取消后再操作组件'
        if self.__dict__.get('runtime_restart_required',False):
            return '托管组件已改变，请重新启动并通过运行时验证'
        if self.__dict__.get('benchmark_running',False):
            return '真实基准正在执行，请完成或取消后再切换组件或模型'
        wizard=self.__dict__.get('wizard_frame')
        if wizard is not None and wizard.__dict__.get('self_test_running',False):
            return '首次向导自检正在执行，请完成后再执行其它音频任务或修改组件'
        if self.__dict__.get('new_install_running'):
            return '完整包正在校验安装，暂不改组件或模型'
        if self.__dict__.get('separator_experiment_running'):
            return '候选实验正在使用托管组件，暂不修改模型或启动其它音频任务'
        if self.__dict__.get('is_running') or self.__dict__.get('is_processing'):
            return '音频任务正在运行，暂不切换托管组件或模型文件'
        if self.__dict__.get('model_import_running'):
            return '用户模型正在安全导入，暂不切换其他组件或模型文件'
        pipeline=self.__dict__.get('pipeline')
        storage=getattr(pipeline,'storage',None)
        if storage is None:
            return '无法确认任务队列状态，暂不切换托管组件或模型文件'
        try:
            running=any(str(job.get('status','')).upper()=='RUNNING' for job in storage.list_jobs(10000))
        except Exception as exc:
            return '无法确认任务队列状态，暂不切换托管组件或模型文件：'+str(exc)
        return '任务队列中仍有 RUNNING 作业，暂不切换托管组件或模型文件' if running else None

    def _refresh_component_mutation_controls(self):
        reason=self._audio_pipeline_busy_reason()
        busy=bool(reason or self.__dict__.get('component_install_running') or
                  self.__dict__.get('component_size_running'))
        buttons=[self.__dict__.get('component_install_button'),self.__dict__.get('component_package_install_button'),self.__dict__.get('component_size_button'),self.__dict__.get('component_online_install_button'),
                 self.__dict__.get('model_import_button')]
        wizard=self.__dict__.get('wizard_frame')
        if wizard is not None:
            buttons.append(getattr(wizard,'import_button',None))
            buttons.append(getattr(wizard,'import_package_button',None))
            if self.__dict__.get('runtime_restart_required',False):
                wizard.runtime_blocked=True
                buttons.append(getattr(wizard,'self_test_button',None))
        for button in buttons:
            if button is None:
                continue
            try:
                button.state(['disabled'] if busy else ['!disabled'])
            except tk.TclError:
                pass

    @staticmethod
    def _component_size_text(value):
        if type(value) is not int or value < 0:
            return '未知'
        unit=value
        suffix='字节'
        for next_suffix in ('KiB','MiB','GiB','TiB'):
            if unit < 1024:
                break
            unit/=1024.0
            suffix=next_suffix
        return '%s (%s 字节)'%(('%d'%unit if isinstance(unit,int) else '%.2f'%unit),format(value,','))

    def _render_component_sizes(self, report):
        tree=self.__dict__.get('component_size_tree')
        if tree is None:
            return
        from core.component_platform import catalog_path,load_catalog
        try:
            catalog=load_catalog(catalog_path(BASE_DIR))
        except Exception:
            catalog={'components':[]}
        names={row['id']:row.get('name',row['id']) for row in catalog.get('components',[])}
        rows=(report or {}).get('components') or {}
        try:
            tree.delete(*tree.get_children())
            for component_id in names:
                measured=rows.get(component_id,{})
                label='%s · %s'%(names[component_id],component_id)
                tree.insert('', 'end', values=(label,
                    self._component_size_text(measured.get('install_bytes')),
                    self._component_size_text(measured.get('download_bytes')),
                    self._component_size_text(measured.get('cache_bytes'))))
        except tk.TclError:
            pass

    def measure_component_sizes(self):
        if self.__dict__.get('component_size_running'):
            return
        if self.__dict__.get('component_size_cache') is not None:
            self._render_component_sizes(self.component_size_cache)
            self.component_size_status.config(text='显示本次 UI 会话缓存的实测结果；再次点击重新测量')
            return
        if not self._abort_scheduled_shutdown('测量组件体积'):
            self.component_size_status.config(text='无法取消之前安排的关机；已停止测量')
            return
        self.component_size_running=True
        self.component_size_token=int(self.__dict__.get('component_size_token',0))+1
        token=self.component_size_token
        self.component_size_status.config(text='正在后台按实际文件统计组件体积…')
        self._refresh_component_mutation_controls()

        def task():
            report=None
            error=None
            try:
                from core.component_platform import catalog_path,load_catalog
                from core.component_sizes import measure_component_sizes
                catalog=load_catalog(catalog_path(BASE_DIR))
                # There is no configured verified download source or component
                # cache location in this workspace, so both remain unknown.
                report=measure_component_sizes(BASE_DIR,catalog,cache_root=None,download_files=None)
            except Exception as exc:
                error=type(exc).__name__+': '+str(exc)
            self.events.put(('component_sizes',(token,report,error)))

        self._start_background_worker(task,daemon=True,name='component-size-measurement')

    def show_component_install_plan(self):
        try:
            from core.online_components import plan
            result=plan(_APP_ROOT,_APP_ROOT/'packaging/download-sources.json',self.component_setup_choice.get())
            self.diagnostics.delete('1.0','end')
            self.diagnostics.insert('end',json.dumps(result,ensure_ascii=False,indent=2))
            self.status.config(text='安装计划已显示；只有已核验来源才允许下载，用户模型由你明确导入')
        except Exception as exc:self.status.config(text='组件计划不可用：'+str(exc))

    def start_component_installer(self):
        reason=self._audio_pipeline_busy_reason()
        if reason:self.status.config(text=reason);return
        if (_APP_ROOT/'runtime-manifest/runtime.json').is_file() and not self.component_setup_choice.get().startswith('tool.'):
            self.status.config(text='完整包请使用下方同版本 .ampkg 或离线目录修复；在线安装器用于轻量版')
            return
        script=_APP_ROOT/'scripts/install_components.ps1'
        if not script.is_file():self.status.config(text='此目录没有组件安装脚本，请使用轻量发布包或离线组件包');return
        import subprocess
        shell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            subprocess.Popen([str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                '-Root',str(_APP_ROOT),'-Components',self.component_setup_choice.get()],cwd=str(_APP_ROOT),
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.runtime_restart_required=True
            if self.runtime_gate is not None:self.runtime_gate.state='FAILED'
            self.runtime_status.config(text='运行环境：安装器已启动，完成后请重新启动并校验')
            for button in self.runtime_action_buttons:button.state(['disabled'])
            self._refresh_component_mutation_controls()
            self.status.config(text='安装器已打开并显示实际进度。完成后请重新启动并校验；当前暂停新的处理任务')
        except Exception as exc:self.status.config(text='安装器未启动：'+str(exc))

    def import_component_package(self):
        reason=self._audio_pipeline_busy_reason()
        if reason:
            self.status.config(text=reason);return
        path=filedialog.askopenfilename(parent=self,title='选择同版本 .ampkg 组件包',filetypes=[('离线组件包','*.ampkg')])
        if path:self.import_offline_components(folder=path)

    def import_offline_components(self, folder=None, live_root=None):
        reason=self._audio_pipeline_busy_reason()
        if reason or self.__dict__.get('component_install_running'):
            message=reason or '离线组件操作已在进行中'
            self.log(message)
            self.status.config(text=message)
            self._refresh_component_mutation_controls()
            return
        if folder is None:
            folder=filedialog.askdirectory(title='选择同版本完整离线目录')
        if not folder:
            return
        target_root=os.path.abspath(os.fspath(live_root or BASE_DIR))
        reason=self._audio_pipeline_busy_reason()
        if reason:
            self.log(reason)
            self.status.config(text=reason)
            self._refresh_component_mutation_controls()
            return
        in_use=bool(reason)
        if not self._abort_scheduled_shutdown('安装或修复托管组件'):
            self.status.config(text='无法取消之前安排的关机；已停止离线组件操作')
            return
        self.component_install_running=True
        self._refresh_component_mutation_controls()
        self.status.config(text='正在校验并安装/修复同版本托管文件…')

        def task():
            result=None
            error=None
            registry=None
            hardware=None
            refresh_error=None
            try:
                from core.install_platform import install_offline_package
                result=install_offline_package(folder,target_root,in_use=in_use)
            except Exception as exc:
                error=exc
            if error is None:
                try:
                    from core.component_platform import build_registry, scan_hardware
                    registry=build_registry(target_root)
                    hardware=scan_hardware(target_root)
                except Exception as exc:
                    refresh_error=exc
            self.events.put(('component_import',(result,registry,hardware,error,refresh_error)))

        self._start_background_worker(task,daemon=True,name='offline-component-install')

    def _maybe_open_onboarding(self):
        from core.ux_flow import onboarding_flags
        flags=onboarding_flags(self.settings)
        if flags['open_wizard']:
            self.show_platform_wizard()
        elif flags['open_tutorial']:
            self.show_tutorial()
    def _tutorial_target(self, title):
        mapping={
            '三种模式': self.mode_buttons.get(self.mode.get()),
            '质量档': getattr(self, 'clean_label', None),
            '目标歌手': getattr(self, 'model_label', None),
            '输出': getattr(self, 'output_button', None),
            '试听': getattr(self, 'play_master_button', None),
        }
        return mapping.get(title)
    def update_application_version(self):
        reason=self._audio_pipeline_busy_reason()
        if reason:messagebox.showinfo('版本更新',reason);return
        source=filedialog.askdirectory(title='选择可信新版本的完整 Portable 发布目录')
        if not source:return
        parent=filedialog.askdirectory(title='选择新版本的安装父目录（保留当前版本）')
        if not parent:return
        target=Path(parent)/('AI-Music-Processor-version-'+uuid.uuid4().hex[:12])
        self._start_version_operation(source,target)
    def rollback_application_version(self):
        reason=self._audio_pipeline_busy_reason()
        if reason:messagebox.showinfo('版本回退',reason);return
        self._start_version_operation()
    def cancel_version_operation(self):
        cancel=self.__dict__.get('version_update_cancel')
        if cancel is not None:
            cancel.set()
            if self.__dict__.get('version_update_running',False):
                self.status.config(text='已请求取消；将在当前校验步骤结束后返回，当前版本选择保持不变')
    def _start_version_operation(self,source=None,target=None):
        # File dialogs run a nested Tk loop; recheck after returning from them.
        reason=self._audio_pipeline_busy_reason()
        if reason:self.status.config(text=reason);return
        if not self._abort_scheduled_shutdown('版本更新或回退'):
            self.status.config(text='无法取消之前安排的关机，版本操作已停止');return
        in_use=bool(self.is_running or self.component_install_running)
        self.version_update_running=True
        cancel=threading.Event();self.version_update_cancel=cancel
        self._refresh_component_mutation_controls()
        self.status.config(text='正在完整校验独立版本目录；保留当前版本及用户数据，可取消')
        def task():
            try:
                from core.version_update import install_version_update,rollback_previous_version
                if source is None:result=rollback_previous_version(CONFIG_DIR,cancel=cancel,in_use=in_use)
                else:result=install_version_update(BASE_DIR,source,target,CONFIG_DIR,cancel=cancel,in_use=in_use)
                self.events.put(('version_operation',(result,None)))
            except Exception as exc:self.events.put(('version_operation',({},exc)))
        self._start_background_worker(task,daemon=True,name='version-update')
    def run_benchmark(self):
        reason=self._audio_pipeline_busy_reason()
        if reason:self.status.config(text=reason);return
        if (self.is_running or self.component_install_running or
                self.__dict__.get('benchmark_running',False) or
                self.__dict__.get('cache_collect_running',False) or
                self.__dict__.get('runtime_restart_required',False)):
            self.status.config(text='请等待当前任务、组件操作或缓存操作完成；组件变化后请先重启');return
        try:params=self.parameters()
        except Exception as exc:messagebox.showerror('基准参数无效',str(exc));return
        self.benchmark_control=Control();self.benchmark_running=True
        control=self.benchmark_control
        directory=Path(TEMP_DIR)/('runtime-benchmark-'+uuid.uuid4().hex)
        self.status.config(text='正在执行当前模式的20秒音频真实整链基准，可取消；模型推理可能较慢')
        def task():
            try:
                from core.runtime_benchmark import run_runtime_benchmark
                report=run_runtime_benchmark(directory,params,control,
                    progress_callback=lambda stage,p,msg:self.events.put(('benchmark_progress',(stage,p,msg))))
                self.events.put(('runtime_benchmark',(report,directory/'benchmark.json',None)))
            except Exception as exc:self.events.put(('runtime_benchmark',({},directory/'benchmark.json',exc)))
        self._start_background_worker(task,daemon=True,name='runtime-benchmark')
    def cancel_benchmark(self):
        control=self.__dict__.get('benchmark_control')
        if control is not None:control.cancel()
    def show_platform_wizard(self):
        from core.component_platform import build_registry, scan_hardware
        if self.wizard_window is not None:
            try:
                if self.wizard_window.winfo_exists():
                    self.wizard_window.lift();return
            except tk.TclError:
                self.wizard_window=None
        try:
            registry=build_registry(BASE_DIR)
            hardware=scan_hardware(BASE_DIR)
        except Exception as exc:
            messagebox.showerror('组件与向导',str(exc));return
        window=tk.Toplevel(self)
        window.title('组件与首次向导')
        window.geometry('760x560')
        def close():
            if window.winfo_exists():
                dispose_tk_variables(window)
                window.destroy()
            self.wizard_window=None
            self.wizard_frame=None
        def skipped():
            self._save_onboarding(first_run_completed=True, tutorial_dismissed=True)
            close()
        def finished(session):
            preferences=session.preferences
            previous_output,previous_quota=self.output.get(),self.cache_quota.get()
            self.output.set(preferences['custom_output_dir'])
            self.cache_quota.set('' if preferences['quota_bytes'] is None else str(preferences['quota_bytes']/1024**3))
            try:
                from core.ux_flow import remember_onboarding
                updated=remember_onboarding(self.settings,first_run_completed=True,tutorial_dismissed=False)
                updated['custom_output_dir']=preferences['custom_output_dir']
                updated['mode']=session.mode if session.mode in ('polish','voice_conversion','transcribe') else self.mode.get()
                if session.mode=='voice_conversion':
                    from core.separator_routing import hp2_profile,dual_profile
                    updated['separator_profile']=hp2_profile() if session.separator=='hp2' else dual_profile()
                self._persist_settings(updated)
            except Exception as error:
                self.output.set(previous_output);self.cache_quota.set(previous_quota)
                messagebox.showerror('首次设置未保存',str(error),parent=window);return
            if session.mode in ('polish','voice_conversion','transcribe'):
                self.mode.set(session.mode)
                self.refresh_mode_flow()
            if session.mode=='voice_conversion':
                self.separator_route.set('hp2' if session.separator=='hp2' else 'dual')
            close()
            self.show_tutorial()
        self.wizard_window=window
        self.wizard_frame=build_first_run_wizard(window, registry, hardware, on_skip=skipped, on_finish=finished,
                                                 on_import=self.import_offline_components,
                                                 on_import_package=self.import_component_package,
                                                 worker_launcher=self._start_background_worker,
                                                 initial_output=self.output.get(),initial_cache_quota=self.cache_quota.get(),
                                                 execution_guard=self._audio_pipeline_busy_reason)
        self.wizard_frame.pack(fill='both', expand=True)
        self._refresh_component_mutation_controls()
        window.protocol('WM_DELETE_WINDOW', close)
    def show_tutorial(self):
        if self.tutorial_window is not None:
            try:
                if self.tutorial_window.winfo_exists():
                    self.tutorial_session.set_mode(self.mode.get())
                    self.tutorial_frame.refresh()
                    self.tutorial_window.lift();return
            except tk.TclError:
                self.tutorial_window=None
        window=tk.Toplevel(self)
        window.title('新手教程')
        window.geometry('640x360')
        def dismiss():
            self._save_onboarding(tutorial_dismissed=True)
        frame=build_tutorial_panel(window, self.mode.get(), self._tutorial_target, on_dismiss=dismiss)
        frame.pack(fill='both', expand=True)
        self.tutorial_window=window
        self.tutorial_frame=frame
        self.tutorial_session=frame.session
        def close():
            frame.session.clear_highlight()
            if window.winfo_exists():
                dispose_tk_variables(window)
                window.destroy()
            self.tutorial_window=None
            self.tutorial_frame=None
            self.tutorial_session=None
        window.protocol('WM_DELETE_WINDOW', close)
    def diagnose(self):
        from core.diagnostics import diagnostics
        self.diagnostics.delete('1.0','end')
        self.diagnostics.insert('end','检测中…')
        def task():
            try:result=json.dumps(diagnostics(),ensure_ascii=False,indent=2)
            except Exception as exc:result=str(exc)
            self.events.put(('log',result))
            self.events.put(('diagnostic',result))
        self._start_background_worker(task,daemon=True,name='diagnostics')
    def cache_info(self):
        root=Path(self.output.get())
        self.cache_stats_token+=1
        token=self.cache_stats_token
        self.status.config(text='正在后台统计输出与缓存…')
        def task():
            total=0;count=0;error=None
            try:
                if root.exists():
                    for item in root.rglob('*'):
                        try:
                            if item.is_file():
                                total+=item.stat().st_size;count+=1
                        except OSError:
                            continue
            except Exception as exc:
                error=exc
            self.events.put(('cache_info',(token,total,count,str(root),error)))
        self._start_background_worker(task,daemon=True,name='cache-stat')

    def _start_cache_collect(self, policy, dry_run, expected=None):
        if (self.__dict__.get('cache_collect_running',False) or self.is_running or
                self.__dict__.get('benchmark_running',False) or
                self.__dict__.get('scan_thread') is not None):
            self.status.config(text='请等待当前处理、文件扫描或缓存操作完成后再回收缓存')
            return
        if not self._abort_scheduled_shutdown('缓存统计或清理'):
            self.status.config(text='无法取消之前安排的关机；已停止缓存操作')
            return
        self.cache_cleanup_token+=1
        token=self.cache_cleanup_token
        self.cache_collect_running=True
        root=Path(self.output.get())
        protected=[item.get("path") for item in self.pending.values()]
        def task():
            try:
                from core.cache_manager import CacheManager
                manager=CacheManager(self.pipeline.storage,root)
                if dry_run:
                    result=manager.collect(quota_bytes=policy['quota_bytes'],
                                           ttl_days=policy['ttl_days'],dry_run=True,protected_paths=protected)
                    self.events.put(('cache_preview',(token,policy,result,None)))
                    return
                # Recompute immediately before deletion. If a concurrent job or
                # the user changed the tree, require another explicit preview.
                check=manager.collect(quota_bytes=policy['quota_bytes'],
                                       ttl_days=policy['ttl_days'],dry_run=True,protected_paths=protected)
                if sorted(check.get('selected_paths',[]))!=sorted(expected or []):
                    self.events.put(('cache_cleanup_changed',(token,'清单已变化，请重新预览')))
                    return
                result=manager.collect(quota_bytes=policy['quota_bytes'],
                                       ttl_days=policy['ttl_days'],dry_run=False,protected_paths=protected)
                result['changed']=True
                self.events.put(('cache_cleanup',(token,result,None)) )
            except Exception as exc:
                if dry_run:self.events.put(('cache_preview',(token,policy,{},exc)))
                else:self.events.put(('cache_cleanup',(token,{},exc)))
        self._start_background_worker(task,daemon=True,name='cache-gc')

    def preview_cache_cleanup(self):
        try:
            policy=self._read_cache_policy()
            self._persist_settings()
        except Exception as exc:
            messagebox.showerror('缓存策略无效',str(exc));return
        self.status.config(text='正在后台生成缓存回收预览…')
        self._start_cache_collect(policy,True)
    def destroy(self):
        if getattr(self,'_tk_teardown_complete',False):
            return
        if threading.get_ident()!=getattr(self,'_tk_owner_thread',threading.get_ident()):
            raise RuntimeError('Tk application must be destroyed on its owning thread')
        if self._active_background_workers():
            self._closing=True
            self.scan_cancel.set()
            self.control.cancel()
            try:self.after(100,self.destroy)
            except tk.TclError:pass
            return
        self._tk_teardown_complete=True
        # Tk's callbacks and Variables must be detached before the Tcl
        # interpreter is destroyed; late cyclic GC can then run on any thread.
        try:
            for after_id in self.tk.splitlist(self.tk.call('after','info')):
                try:self.after_cancel(after_id)
                except tk.TclError:pass
        except tk.TclError:
            pass
        unhook_drop_native(self)
        self._dispose_tk_variables()
        gc.collect()
        super().destroy()
        gc.collect()

    def close(self, persist=True):
        gate=self.__dict__.get('runtime_gate')
        if gate is not None:gate.cancelled.set()
        self.cancel_version_operation()
        self.cancel_benchmark()
        preview_control=self.__dict__.get('source_preview_control')
        if preview_control is not None:preview_control.cancel()
        from core.ab_preview import stop_ab_preview
        stop_ab_preview()
        self.ab_token=uuid.uuid4().hex
        self.scan_cancel.set()
        self.control.cancel()
        if self.is_running or self._active_background_workers():
            self._closing=True
            self.status.config(text='正在停止处理并等待后台工作结束…')
            self.after(100,lambda:self.close(persist=persist));return
        if persist:
            try:self._persist_settings(self.parameters())
            except (ValueError,OSError) as exc:self.log('退出时设置保存失败：'+str(exc))
            self._try_save_queue()
        self.pipeline.storage.close()
        preview_root=Path(TEMP_DIR)/'ab-previews'
        for owned in self.__dict__.get('ab_owned_previews',[]):
            path=Path(owned)
            if path.parent==preview_root and path.name.startswith('ab-'):
                for temporary in (path,Path(str(path)+'.json')):
                    try:temporary.unlink(missing_ok=True)
                    except OSError as error:self.log('试听临时文件暂未清理：'+str(error))
        self.destroy()


def _runtime_guard_action(method):
    from functools import wraps
    @wraps(method)
    def guarded(self,*args,**kwargs):
        gate=self.__dict__.get('runtime_gate')
        if gate is not None and not gate.ready:
            self.status.config(text='运行环境尚未通过校验，请等待，或在工具页安装/修复后重新校验')
            return
        if gate is not None and method.__name__ in ('start','work','run_benchmark'):
            allowed=(gate.result or {}).get('allowed_modes')
            mode=self.mode.get() if 'mode' in self.__dict__ else 'polish'
            if allowed is not None and mode not in allowed:
                self.status.config(text='当前仅安装精修组件，请在工具页安装所选模式的依赖')
                return
        if gate is not None and (gate.result or {}).get('allowed_modes') is not None and method.__name__ in ('run_separator_experiment','analyze_selected_voice_range'):
            self.status.config(text='此功能需要完整音频/分离组件，请先在工具页安装')
            return
        return method(self,*args,**kwargs)
    return guarded


# UI visibility never authorizes unverified tools/model execution. Guard both
# buttons and keyboard/programmatic entry points, including source preview.
for _action in ('start','work','analyze_selected_voice_range','preview_selected_waveform',
                'show_ab_preview','editor','diagnose','run_benchmark',
                'run_separator_experiment','preview_pending_source','play'):
    setattr(AIMusicProcessorApp,_action,_runtime_guard_action(getattr(AIMusicProcessorApp,_action)))


def _run_smoke_test(app):
    app.withdraw()
    app.update_idletasks()
    with open(os.path.join(TEMP_DIR,'launcher_smoke.json'),'w',encoding='utf-8') as f:
        json.dump(dict(status='PASS',models=list(app.models),tk=app.tk.call('info','patchlevel')),f)
    # A launcher smoke run must not rewrite the user's settings or queued work.
    app.close(persist=False)
    app.mainloop()


if __name__=='__main__':
    try:
        if os.name=='nt':
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
            kernel.CreateMutexW.restype=ctypes.c_void_p
            instance_mutex=kernel.CreateMutexW(None,False,'Local\\AI_Music_Processor_1_Local')
            if ctypes.get_last_error()==183:
                messagebox.showinfo('已运行','工作台已经打开，请使用现有窗口')
                sys.exit(0)
        app=AIMusicProcessorApp(defer_runtime_verification=_DEFER_RUNTIME_CHECK)
        if '--smoke-test' in sys.argv:
            _run_smoke_test(app)
        else:
            app.mainloop()
    except Exception:
        import traceback
        os.makedirs(os.path.dirname(STARTUP_ERROR_PATH),exist_ok=True)
        path=STARTUP_ERROR_PATH
        with open(path,'w',encoding='utf-8') as f:f.write(traceback.format_exc())
        if '--smoke-test' not in sys.argv:
            try:messagebox.showerror('启动失败','详情见 '+path)
            except Exception:pass
        raise SystemExit(1)


