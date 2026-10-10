import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TkLifecycleTests(unittest.TestCase):
    def test_launcher_smoke_waits_for_workers_without_persisting_user_state(self):
        script = r'''
import json, os, tempfile, threading
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, __ROOT_REPR__)
import AI_Music_Processor as ui
import core.config as config
import core.storage as storage

with tempfile.TemporaryDirectory(prefix="amp-smoke-no-save-") as folder:
    root = Path(folder)
    for name in ("config", "output", "temp", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    overrides = {
        "CONFIG_DIR": str(root / "config"), "OUTPUT_DIR": str(root / "output"),
        "TEMP_DIR": str(root / "temp"), "LOG_DIR": str(root / "logs"),
    }
    with patch.multiple(config, **overrides), patch.multiple(ui, **overrides), \
            patch.object(storage, "DEFAULT_DATABASE_PATH", str(root / "output" / "database" / "ui.db")), \
            patch.object(ui, "get_available_rvc_models", return_value={}):
        app = ui.AIMusicProcessorApp()
        release = threading.Event()
        app._start_background_worker(lambda owner=app: release.wait(3),
                                     daemon=True, name="smoke-lifecycle-fixture")
        app.after(150, release.set)
        ui._run_smoke_test(app)
        if (root / "config" / "app.json").exists():
            raise RuntimeError("smoke run overwrote app settings")
        if (root / "config" / "queue.json").exists():
            raise RuntimeError("smoke run overwrote the queued work")
        manifest = json.loads((root / "temp" / "launcher_smoke.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "PASS":
            raise RuntimeError("smoke manifest was not written")
print("TK_SMOKE_NO_SAVE_PASS")
'''.replace("__ROOT_REPR__", repr(str(ROOT)))
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("TK_SMOKE_NO_SAVE_PASS", result.stdout)
        self.assertNotIn("Variable.__del__", result.stderr)
        self.assertNotIn("Tcl_AsyncDelete", result.stderr)

    def test_repeated_gui_close_detaches_tcl_before_background_collection(self):
        script = r'''
import gc, os, tempfile, threading, time
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, __ROOT_REPR__)
import AI_Music_Processor as ui
import core.config as config
import core.storage as storage

for iteration in range(3):
    folder = tempfile.TemporaryDirectory(prefix="amp-tk-lifecycle-")
    root = Path(folder.name)
    for name in ("config", "output", "temp", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    overrides = {
        "CONFIG_DIR": str(root / "config"), "OUTPUT_DIR": str(root / "output"),
        "TEMP_DIR": str(root / "temp"), "LOG_DIR": str(root / "logs"),
    }
    with patch.multiple(config, **overrides), patch.multiple(ui, **overrides), \
            patch.object(storage, "DEFAULT_DATABASE_PATH", str(root / "output" / "database" / "ui.db")), \
            patch.object(ui, "get_available_rvc_models", return_value={}):
        app = ui.AIMusicProcessorApp()
        app.withdraw()
        app.update_idletasks()
        app._maybe_open_onboarding()
        release = threading.Event()
        # The worker intentionally retains the app until close has waited it out.
        app._start_background_worker(lambda owner=app: release.wait(3),
                                     daemon=True, name="lifecycle-fixture")
        app.close()
        release.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                if not app.winfo_exists():
                    break
                app.update()
            except ui.tk.TclError:
                break
            time.sleep(0.01)
        try:
            app_exists = bool(app.winfo_exists())
        except ui.tk.TclError:
            app_exists = False
        if app_exists:
            raise RuntimeError("app did not finish close after worker exit")
        del app
        gc.collect()
        collector = threading.Thread(target=gc.collect, name="late-gc-fixture")
        collector.start()
        collector.join(5)
        if collector.is_alive():
            raise RuntimeError("background GC did not finish")
    folder.cleanup()

print("TK_LIFECYCLE_PASS")
'''.replace("__ROOT_REPR__", repr(str(ROOT)))
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("TK_LIFECYCLE_PASS", result.stdout)
        self.assertNotIn("Variable.__del__", result.stderr)
        self.assertNotIn("Tcl_AsyncDelete", result.stderr)


if __name__ == "__main__":
    unittest.main()
