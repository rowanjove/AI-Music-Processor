import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.config as config
import core.storage as storage


class RecordingCompletionActions:
    def __init__(self, finish_status="SCHEDULED"):
        self.finish_status = finish_status
        self.arm_call = None
        self.finish_call = None
        self.disarmed = False
        self.abort_calls = []

    def abort_scheduled_shutdown(self):
        self.abort_calls.append(True)
        return {"status": "NOT_SCHEDULED"}

    def disarm(self):
        self.disarmed = True

    def arm(self, action, batch_ids, confirmed=False, dispatch_nonce=None):
        self.arm_call = (action, tuple(batch_ids), confirmed, dispatch_nonce)
        return {"armed": action != "none", "action": action, "dispatch_nonce": dispatch_nonce}

    def finish(self, batch_statuses, dispatch_nonce, **kwargs):
        self.finish_call = (dict(batch_statuses), dispatch_nonce, dict(kwargs))
        return {"status": self.finish_status, "action": "shutdown"}


def _load_ui():
    spec = importlib.util.spec_from_loader(
        "session_actions_ui_test",
        importlib.machinery.SourceFileLoader("session_actions_ui_test", str(ROOT / "AI_Music_Processor.pyw")),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionActionUiTests(unittest.TestCase):
    def _make_app(self, folder):
        import tkinter as tk
        ui = _load_ui()
        data = Path(folder)
        for name in ("config", "output", "temp", "logs"):
            (data / name).mkdir(parents=True, exist_ok=True)
        with patch.object(config, "CONFIG_DIR", str(data / "config")), \
             patch.object(ui, "CONFIG_DIR", str(data / "config")), \
             patch.object(config, "OUTPUT_DIR", str(data / "output")), \
             patch.object(ui, "OUTPUT_DIR", str(data / "output")), \
             patch.object(config, "TEMP_DIR", str(data / "temp")), \
             patch.object(ui, "TEMP_DIR", str(data / "temp")), \
             patch.object(storage, "DEFAULT_DATABASE_PATH", str(data / "output" / "database" / "ui.db")), \
             patch.object(ui, "hook_drop_native", lambda *args, **kwargs: None):
            app = ui.AIMusicProcessorApp()
        app.withdraw()
        app.update()
        self.addCleanup(self._close_app, app, tk)
        return app, ui

    @staticmethod
    def _close_app(app, tk):
        try:
            for item in app.tk.splitlist(app.tk.call("after", "info")):
                app.after_cancel(item)
        except tk.TclError:
            pass
        try:
            app.pipeline.storage.close()
        except Exception:
            pass
        try:
            app.destroy()
        except tk.TclError:
            pass

    def test_explicit_action_is_confirmed_and_bound_to_each_dispatch_id(self):
        import tkinter as tk
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = tempdir.name
        app, ui = self._make_app(folder)
        actions = RecordingCompletionActions()
        app.completion_actions = actions
        app.completion_action_var.set("shutdown")
        items = [("queue-a", {"path": "a.wav", "params": {}}),
                 ("queue-b", {"path": "b.wav", "params": {}})]
        with patch.object(ui.messagebox, "askyesno", return_value=True):
            self.assertTrue(app._arm_completion_for_batch(items))
        action, identifiers, confirmed, nonce = actions.arm_call
        self.assertEqual(action, "shutdown")
        self.assertTrue(confirmed)
        self.assertTrue(nonce)
        self.assertEqual(set(identifiers), {item[1]["dispatch_id"] for item in items})
        self.assertEqual(len(set(identifiers)), 2)
        app._completion_statuses = {identifier: "DONE" for identifier in identifiers}
        app._finish_completion_batch()
        statuses, finished_nonce, options = actions.finish_call
        self.assertEqual(set(statuses), set(identifiers))
        self.assertEqual(set(statuses.values()), {"DONE"})
        self.assertEqual(finished_nonce, nonce)
        self.assertFalse(options["pending"])
        self.assertFalse(options["active"])
        self.assertFalse(options["maintenance"])
        self.assertEqual(app._completion_batch_ids, ())
        self.assertIn("处理完成", app.status.cget("text"))

    def test_declined_action_does_not_arm_and_user_cancel_disarms(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = tempdir.name
        app, ui = self._make_app(folder)
        actions = RecordingCompletionActions()
        app.completion_actions = actions
        app.completion_action_var.set("exit")
        items = [("queue-a", {"path": "a.wav", "params": {}})]
        with patch.object(ui.messagebox, "askyesno", return_value=False):
            self.assertTrue(app._arm_completion_for_batch(items))
        self.assertEqual(actions.arm_call[0], "none")
        self.assertTrue(actions.arm_call[2])
        app.is_running = True
        app.cancel()
        self.assertTrue(actions.disarmed)
        self.assertEqual(len(actions.abort_calls), 2)

    def test_settings_keep_unrelated_fields_and_reject_unknown_completion_action(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config, "CONFIG_DIR", folder):
            path = Path(folder) / "app.json"
            path.write_text(json.dumps({
                "prevent_sleep": True,
                "completion_action": "shutdown",
                "notify_in_app": False,
                "notify_sound": True,
                "notify_toast": False,
                "user_extension": {"keep": 9},
            }), encoding="utf-8")
            values = config.load_settings()
            self.assertTrue(values["prevent_sleep"])
            self.assertEqual(values["completion_action"], "shutdown")
            self.assertFalse(values["notify_in_app"])
            self.assertTrue(values["notify_sound"])
            self.assertEqual(values["user_extension"], {"keep": 9})
            path.write_text(json.dumps({"completion_action": "force_shutdown", "notify_toast": "yes"}), encoding="utf-8")
            sanitized = config.load_settings()
            self.assertNotIn("completion_action", sanitized)
            self.assertNotIn("notify_toast", sanitized)


if __name__ == "__main__":
    unittest.main()
