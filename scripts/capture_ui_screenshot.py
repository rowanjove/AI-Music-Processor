import os
import sys
import time
from pathlib import Path
import ctypes
from ctypes import wintypes
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib

def capture_hwnd_to_file(hwnd, output_path):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid window size: {w}x{h}")
        
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    old_bmp = gdi32.SelectObject(mem_dc, bitmap)
    
    rendered = user32.PrintWindow(hwnd, mem_dc, 2)
    if not rendered:
        rendered = user32.PrintWindow(hwnd, mem_dc, 0)
        
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD)
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3)
        ]

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h  # top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(bi), 0)

    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    img = img.convert("RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)
    print(f"Saved screenshot: {output_path} ({w}x{h})")

    gdi32.SelectObject(mem_dc, old_bmp)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

def capture():
    import core.ux_flow
    core.ux_flow.onboarding_flags = lambda settings: {'open_wizard': False, 'open_tutorial': False}
    
    app_module = importlib.import_module("AI_Music_Processor")
    app = app_module.AIMusicProcessorApp(defer_runtime_verification=False)
    
    # Set size and position
    app.geometry("1100x780+100+60")
    app.update()
    app.update_idletasks()
    
    # Setup Voice Conversion mode
    app.mode.set("voice_conversion")
    app.separator_route.set("dual")
    app.f0.set("rmvpe")
    app.mix_vocal_gain.set("0.5")
    app.mix_instrumental_gain.set("-1.0")
    app.mix_ducking.set(True)
    app.refresh_mode_flow()
    
    # Insert realistic demonstration items
    try:
        app.tree.delete(*app.tree.get_children())
        app.tree.insert('', 'end', values=('demo_vocal_track.wav', '换声重制 (Voice Conversion)', '就绪', 'Kim Vocal 2 / RMVPE / sunyanzi (+2)', 'master/stems/reports'))
        app.tree.insert('', 'end', values=('pop_song_fullmix.mp3', '换声重制 (Voice Conversion)', '处理中 (68%)', 'Kim Vocal 2 + Inst HQ 3 / harvest', 'stems/vocal_converted.wav'))
    except Exception as e:
        print("Mock insert warning:", e)
        
    app.update()
    app.update_idletasks()
    time.sleep(0.8)
    
    user32 = ctypes.windll.user32
    child_hwnd = app.winfo_id()
    hwnd = user32.GetParent(child_hwnd) or child_hwnd
    
    output_dir = ROOT / "docs" / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Main Workbench (Voice Conversion)
    app.home_canvas.yview_moveto(0.0)
    app.update()
    time.sleep(0.4)
    capture_hwnd_to_file(hwnd, output_dir / "workbench_main.png")
    
    # 2. Advanced Audio & Mixing Settings (Scrolled down to Advanced Frame)
    app.home_canvas.yview_moveto(0.38)
    app.update()
    time.sleep(0.5)
    capture_hwnd_to_file(hwnd, output_dir / "advanced_voice_conversion.png")
    
    # 3. Transcribe Mode (Stem separation + MIDI)
    app.mode.set("transcribe")
    app.transcribe_stem.set("vocal")
    app.midi_cleanup_enabled.set(True)
    app.midi_cleanup_quantize.set(True)
    app.refresh_mode_flow()
    
    try:
        app.tree.delete(*app.tree.get_children())
        app.tree.insert('', 'end', values=('guitar_solo_acoustic.wav', '扒谱四轨 + MIDI', '就绪', 'HTDemucs 4-Stem / Basic Pitch', 'midi/stems/qc_result.json'))
        app.tree.insert('', 'end', values=('bassline_groove_take2.flac', '扒谱四轨 + MIDI', '就绪', 'HTDemucs 4-Stem (Bass 轨优先)', 'midi/bass_transcribed.mid'))
    except Exception as e:
        print("Mock insert warning:", e)
        
    app.home_canvas.yview_moveto(0.0)
    app.update()
    time.sleep(0.6)
    capture_hwnd_to_file(hwnd, output_dir / "transcribe_mode.png")
    
    # 4. Model & Presets Tab
    try:
        tabs = app.notebook.tabs()
        if len(tabs) > 2:
            app.notebook.select(tabs[2])
            app.update()
            time.sleep(0.5)
            capture_hwnd_to_file(hwnd, output_dir / "model_presets.png")
    except Exception as e:
        print("Model tab warning:", e)
        
    app.destroy()

if __name__ == '__main__':
    capture()
