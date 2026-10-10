import sys,struct,ctypes as c,faulthandler,importlib.machinery,time,os,json,tempfile,shutil
from pathlib import Path
faulthandler.enable()
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
import core.config as cfg
cfg.CONFIG_DIR=tempfile.mkdtemp(prefix='drop-test-',dir=root/'temp')
source=root/'temp/drag_baseline_20261004.pyw' if os.environ.get('TEST_DROP_BASELINE') else root/'AI_Music_Processor.pyw'
ui=importlib.machinery.SourceFileLoader('drop_ui',str(source)).load_module()
app=ui.AIMusicProcessorApp();app.withdraw();app.update()
user=c.WinDLL('user32');kernel=c.WinDLL('kernel32')
user.GetParent.argtypes=[c.c_void_p];user.GetParent.restype=c.c_void_p
hwnd=user.GetParent(app.winfo_id()) or app.winfo_id()
kernel.GlobalAlloc.argtypes=[c.c_uint,c.c_size_t];kernel.GlobalAlloc.restype=c.c_void_p
kernel.GlobalLock.argtypes=[c.c_void_p];kernel.GlobalLock.restype=c.c_void_p
kernel.GlobalUnlock.argtypes=[c.c_void_p]
user.SendMessageW.argtypes=[c.c_void_p,c.c_uint,c.c_size_t,c.c_ssize_t];user.SendMessageW.restype=c.c_ssize_t
folder=Path(cfg.CONFIG_DIR)/'拖拽 文件夹';folder.mkdir()
shutil.copyfile(root/'temp/验收 中文短样本.wav',folder/'中文 音频.wav')
(folder/'不支持.txt').write_text('ignored',encoding='utf-8')
for index in range(20):
    paths=[str(root/'temp/验收 中文短样本.wav'),str(root/'temp/单声道 中文 10秒.m4a')]
    if index>=3:paths.append(str(folder))
    payload=struct.pack('<IiiII',20,0,0,0,1)+('\0'.join(paths)+'\0\0').encode('utf-16le')
    handle=kernel.GlobalAlloc(0x42,len(payload));ptr=kernel.GlobalLock(handle)
    c.memmove(ptr,payload,len(payload));kernel.GlobalUnlock(handle)
    print('SEND_DROP',flush=True)
    user.PostMessageW.argtypes=[c.c_void_p,c.c_uint,c.c_size_t,c.c_ssize_t];user.PostMessageW.restype=c.c_int;user.PostMessageW(hwnd,0x233,handle,0)
    deadline=time.monotonic()+.15
    while time.monotonic()<deadline:
        app.update();time.sleep(.01)
    print('RECEIVED',len(app.pending),flush=True)
assert len(app.pending)==3
assert all(app.tree.item(key)['values'][1]=='等待' for key in app.pending)
result=dict(status='PASS',native_async_drops=20,queued_audio_files=len(app.pending),pointer_bits=c.sizeof(c.c_void_p)*8,unicode_paths=True,folder=True,duplicate_filter=True)
app.pending.clear();app.close()
print('DROP_PASS',flush=True)
(root/'output/acceptance/drop_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')

