import sys
import traceback
import unittest
import copy
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.component_platform import build_registry
from core.ux_flow import mode_stages


def _drive(parent, registry, hardware):
    import tkinter as tk
    from AI_Music_Processor import (
        build_first_run_wizard,
        build_mode_flow_panel,
        build_tutorial_panel,
        dispose_tk_variables,
        highlight_mode_stage,
    )
    stages = {}
    for mode in ("polish", "voice_conversion", "transcribe"):
        panel = build_mode_flow_panel(parent, mode)
        stages[mode] = [panel.stage_labels[name].cget("text") for name in panel.stage_labels]
    voice = build_mode_flow_panel(parent, "voice_conversion", generate_midi=True)
    highlight_mode_stage(voice, "Separate")
    highlighted = voice.stage_labels["Separate"].cget("text")
    highlight_mode_stage(voice, "RVC")
    moved = voice.stage_labels["RVC"].cget("text")
    cleared = voice.stage_labels["Separate"].cget("text")
    optional = build_mode_flow_panel(parent, "voice_conversion", generate_midi=True,
                                     vocal_restore=True, pitch_correction=True,
                                     midi_cleanup=True, mix_ducking=False)
    highlight_mode_stage(optional, "PitchCorrection", "跳过：用户选择 Off")
    skipped_pitch = optional.stage_labels["PitchCorrection"].cget("text")
    highlight_mode_stage(optional, "RVC", "开始")
    skipped_pitch_after_next = optional.stage_labels["PitchCorrection"].cget("text")
    jobs = ["kept-job"]
    wizard = build_first_run_wizard(parent, registry, hardware)
    welcome = wizard.step_label.cget("text")
    wizard.next_button.invoke()
    hardware_step = wizard.step_label.cget("text")
    hardware_body = wizard.body.get("1.0", "end")
    wizard.back_button.invoke()
    back = wizard.step_label.cget("text")
    wizard.self_test_button.invoke()
    unselected = wizard.session.self_test["reason"]
    wizard.mode_voice.invoke()
    wizard.separator_dual.invoke()
    wizard.self_test_button.invoke()
    deadline = time.monotonic() + 5
    while getattr(wizard, "self_test_running", False) and time.monotonic() < deadline:
        parent.update()
        time.sleep(0.01)
    for _ in range(3):
        wizard.next_button.invoke()
    install_body = wizard.body.get("1.0", "end")
    wizard.skip_button.invoke()
    wizard.finish_button.invoke()
    singer = tk.Label(parent, text="模型")
    voice_tutorial = build_tutorial_panel(
        parent, "voice_conversion", lambda title: singer if title == "目标歌手" else None,
    )
    while voice_tutorial.session.title() != "目标歌手":
        voice_tutorial.next_button.invoke()
    voice_steps = list(voice_tutorial.session.steps)
    highlighted_singer = singer.cget("text")
    voice_tutorial.session.set_mode("polish")
    restored_singer = singer.cget("text")
    polish_steps = list(voice_tutorial.session.steps)
    tutorial = build_tutorial_panel(parent)
    tutorial.next_button.invoke()
    tutorial.skip_button.invoke()
    skipped = tutorial.session.skipped
    tutorial.reopen_button.invoke()
    result = {
        "stages": stages,
        "midi_stages": [voice.stage_labels[name].cget("text") for name in voice.stage_labels],
        "highlighted": highlighted,
        "moved": moved,
        "cleared": cleared,
        "skipped_pitch": skipped_pitch,
        "skipped_pitch_after_next": skipped_pitch_after_next,
        "welcome": welcome,
        "hardware_step": hardware_step,
        "gpu_unprobed": "GPU：未探测" in hardware_body,
        "back": back,
        "skipped_wizard": wizard.session.skipped,
        "wizard_resets_jobs": wizard.session.resets_jobs,
        "jobs": list(jobs),
        "self_test": wizard.session.self_test,
        "unselected_reason": unselected,
        "selected_mode": wizard.session.mode,
        "selected_separator": wizard.session.separator,
        "install_offline": "没有在线组件源" in install_body and "活动托管清单校验后安装或修复" in install_body,
        "finished_mode": wizard.session.finished and wizard.session.mode,
        "voice_steps": voice_steps,
        "highlighted_singer": highlighted_singer,
        "restored_singer": restored_singer,
        "polish_steps": polish_steps,
        "tutorial_skipped_then_reopened": skipped and tutorial.session.reopened and not tutorial.session.skipped,
        "tutorial_index": tutorial.session.index,
        "tutorial_resets_jobs": tutorial.session.resets_jobs,
        "tutorial_title": tutorial.step_label.cget("text"),
    }
    dispose_tk_variables(parent)
    return result


class OnboardingWidgetTests(unittest.TestCase):
    def test_real_widgets_agree_across_two_constructions(self):
        import tkinter as tk
        registry = copy.deepcopy(build_registry(ROOT))
        for component_id in ("engine.separator.kim_vocal_2", "engine.separator.inst_hq_3"):
            registry["components"][component_id] = dict(
                registry["components"].get(component_id, {}), implemented=False, status="NOT_IMPLEMENTED"
            )
        hardware = {"gpu": {"status": "NOT_PROBED"}}
        root = tk.Tk()
        try:
            root.withdraw()
            first = _drive(tk.Toplevel(root), registry, hardware)
            second = _drive(tk.Toplevel(root), registry, hardware)
        finally:
            root.destroy()
        self.assertEqual(first, second)
        pipeline = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        for mode, names in first["stages"].items():
            self.assertEqual(names, mode_stages(mode))
            for name in names:
                self.assertIn('"%s"' % name, pipeline)
        self.assertIn("Transcribe", first["midi_stages"])
        self.assertNotIn("Transcribe", first["stages"]["voice_conversion"])
        self.assertEqual(first["highlighted"], "● Separate")
        self.assertEqual(first["moved"], "● RVC")
        self.assertEqual(first["cleared"], "Separate")
        self.assertEqual(first["skipped_pitch"], "↷ PitchCorrection（跳过）")
        self.assertEqual(first["skipped_pitch_after_next"], "↷ PitchCorrection（跳过）")
        self.assertEqual(first["welcome"], "欢迎")
        self.assertEqual(first["hardware_step"], "硬件检测")
        self.assertTrue(first["gpu_unprobed"])
        self.assertEqual(first["back"], "欢迎")
        self.assertTrue(first["skipped_wizard"])
        self.assertFalse(first["wizard_resets_jobs"])
        self.assertEqual(first["jobs"], ["kept-job"])
        self.assertEqual(first["selected_mode"], "voice_conversion")
        self.assertEqual(first["selected_separator"], "dual")
        report = first["self_test"]
        self.assertEqual(report["chain"], "dual")
        self.assertFalse(report["gpu_pass"])
        self.assertFalse(report["inference_ran"])
        self.assertFalse(report["start_gpu"])
        self.assertFalse(report["writes_history"])
        self.assertEqual(report["quality_acceptance"], "NOT_ASSESSED")
        self.assertIn("engine.separator.kim_vocal_2", report["missing"])
        self.assertIn("engine.separator.inst_hq_3", report["missing"])
        self.assertIn("请先选择", first["unselected_reason"])
        self.assertTrue(first["install_offline"])
        self.assertEqual(first["finished_mode"], "voice_conversion")
        self.assertIn("目标歌手", first["voice_steps"])
        self.assertNotIn("目标歌手", first["polish_steps"])
        self.assertEqual(first["highlighted_singer"], "● 模型")
        self.assertEqual(first["restored_singer"], "模型")
        self.assertTrue(first["tutorial_skipped_then_reopened"])
        self.assertEqual(first["tutorial_index"], 0)
        self.assertFalse(first["tutorial_resets_jobs"])
        self.assertEqual(first["tutorial_title"], "三种模式")
        window = (ROOT / "AI_Music_Processor.pyw").read_text(encoding="utf-8")
        self.assertIn("build_first_run_wizard", window)
        self.assertIn("build_tutorial_panel", window)
        self.assertIn("highlight_running_stage", window)
        self.assertIn("show_platform_wizard", window)
        self.assertIn("preflight_queue_items", window)
        self.assertIn("_maybe_open_onboarding", window)
        self.assertIn("remember_onboarding", (ROOT / "core" / "ux_flow.py").read_text(encoding="utf-8"))

    def test_first_launch_opens_the_wizard_and_remembers_skip_or_finish(self):
        import json
        import tempfile
        import tkinter as tk
        from unittest.mock import patch
        import AI_Music_Processor as ui
        from core import config
        from core.ux_flow import onboarding_flags

        def stop(app):
            app.close()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if not app.winfo_exists():
                        return
                    app.update()
                except tk.TclError:
                    return
                time.sleep(0.01)
            try:
                still_exists = bool(app.winfo_exists())
            except tk.TclError:
                still_exists = False
            if still_exists:
                raise RuntimeError("application close did not wait for its workers")

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config, "CONFIG_DIR", folder), patch.object(ui, "CONFIG_DIR", folder):
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    self.assertIsNotNone(app.wizard_window)
                    self.assertTrue(app.wizard_window.winfo_exists())
                    self.assertEqual(app.wizard_frame.step_label.cget("text"), "欢迎")
                    self.assertIsNone(app.tutorial_window)
                    session = app.wizard_frame.session
                    app.wizard_frame.skip_button.invoke()
                    self.assertTrue(session.skipped)
                    self.assertFalse(session.resets_jobs)
                    self.assertIsNone(app.wizard_window)
                    saved = json.loads((Path(folder) / "app.json").read_text(encoding="utf-8"))
                    self.assertTrue(saved["first_run_completed"])
                    self.assertTrue(saved["tutorial_dismissed"])
                    flags = onboarding_flags(saved)
                    self.assertFalse(flags["open_wizard"])
                    self.assertFalse(flags["open_tutorial"])
                    self.assertFalse(flags["resets_jobs"])
                finally:
                    stop(app)
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    self.assertIsNone(app.wizard_window)
                    self.assertIsNone(app.tutorial_window)
                    app.show_tutorial()
                    app.update()
                    self.assertEqual(app.tutorial_frame.step_label.cget("text"), "三种模式")
                    self.assertFalse(app.tutorial_session.resets_jobs)
                finally:
                    stop(app)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config, "CONFIG_DIR", folder), patch.object(ui, "CONFIG_DIR", folder):
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    app.wizard_frame.mode_voice.invoke()
                    app.wizard_frame.separator_hp2.invoke()
                    app.wizard_frame.finish_button.invoke()
                    app.update()
                    self.assertEqual(app.mode.get(), "voice_conversion")
                    saved=json.loads((Path(folder)/'app.json').read_text(encoding='utf-8'))
                    self.assertEqual(saved['mode'],'voice_conversion')
                    self.assertEqual(saved['separator_profile']['mode'],'hp2')
                    self.assertEqual(config.load_settings()['mode'],'voice_conversion')
                    self.assertIsNotNone(app.tutorial_window)
                    self.assertIn("目标歌手", app.tutorial_session.steps)
                    while app.tutorial_session.title() != "目标歌手":
                        app.tutorial_frame.next_button.invoke()
                    self.assertTrue(app.model_label.cget("text").startswith("●"))
                    app.mode.set("polish")
                    app.refresh_mode_flow()
                    self.assertNotIn("目标歌手", app.tutorial_session.steps)
                    self.assertEqual(app.model_label.cget("text"), "模型")
                    app.tutorial_frame.done_button.invoke()
                    saved = json.loads((Path(folder) / "app.json").read_text(encoding="utf-8"))
                    self.assertTrue(saved["first_run_completed"])
                    self.assertTrue(saved["tutorial_dismissed"])
                    self.assertFalse(app.tutorial_session.resets_jobs)
                finally:
                    stop(app)
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    self.assertIsNone(app.wizard_window)
                    self.assertIsNone(app.tutorial_window)
                finally:
                    stop(app)

    def test_mode_flow_follows_applied_params_and_dual_choice_roundtrips(self):
        import json
        import tempfile
        import tkinter as tk
        from unittest.mock import patch
        import AI_Music_Processor as ui
        from core import config
        from core.separator_routing import dual_profile

        def stop(app):
            app.close()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if not app.winfo_exists():
                        return
                    app.update()
                except tk.TclError:
                    return
                time.sleep(0.01)
            try:
                still_exists = bool(app.winfo_exists())
            except tk.TclError:
                still_exists = False
            if still_exists:
                raise RuntimeError("application close did not wait for its workers")

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(config, "CONFIG_DIR", folder), patch.object(ui, "CONFIG_DIR", folder):
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    app.notebook.select(app.notebook.tabs()[0])
                    self.assertTrue(app.bind_all("<Control-KeyPress>"))
                    with patch.object(app, "choose_file") as choose_file, patch.object(app, "start") as start:
                        self.assertEqual(app._handle_quick_action(SimpleNamespace(keysym="o")), "break")
                        self.assertEqual(app._handle_quick_action(SimpleNamespace(keysym="Return")), "break")
                        choose_file.assert_called_once()
                        start.assert_called_once()
                    app.notebook.select(app.notebook.tabs()[0])
                    self.assertEqual(app._handle_quick_action(SimpleNamespace(keysym="comma")), "break")
                    self.assertEqual(app.notebook.select(), str(app.settings_tab))
                    app.wizard_frame.skip_button.invoke()
                    app.apply_params({"mode": "voice_conversion", "generate_midi": False})
                    self.assertEqual(app.mode.get(), "voice_conversion")
                    self.assertIn("RVC", app.mode_flow_panel.stage_labels)
                    self.assertIn("VocalRestore", app.mode_flow_panel.stage_labels)
                    self.assertIn("PitchCorrection", app.mode_flow_panel.stage_labels)
                    self.assertIn("Ducking", app.mode_flow_panel.stage_labels)
                    self.assertNotIn("MIDICleanup", app.mode_flow_panel.stage_labels)
                    self.assertEqual(app.separator_route.get(), "hp2")
                    app.apply_params({"mode": "voice_conversion", "generate_midi": True})
                    self.assertIn("MIDICleanup", app.mode_flow_panel.stage_labels)
                    app.apply_params({"mode": "transcribe", "generate_midi": False})
                    self.assertIn("MIDICleanup", app.mode_flow_panel.stage_labels)
                    app.apply_params({"mode": "voice_conversion", "separator_profile": dual_profile()})
                    self.assertEqual(app.separator_route.get(), "dual")
                    app._persist_settings(app.parameters())
                    saved = json.loads((Path(folder) / "app.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["separator_profile"]["mode"], "dual")
                    self.assertEqual(saved["separator_profile"]["vocal"]["parameters"], {})
                finally:
                    stop(app)
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    self.assertEqual(app.separator_route.get(), "dual")
                    app.separator_route.set("hp2")
                    app._persist_settings(app.parameters())
                    saved = json.loads((Path(folder) / "app.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["separator_profile"]["mode"], "hp2")
                    self.assertTrue(saved["first_run_completed"])
                finally:
                    stop(app)
                app = ui.AIMusicProcessorApp()
                try:
                    app.withdraw()
                    app.update()
                    self.assertEqual(app.separator_route.get(), "hp2")
                finally:
                    stop(app)


if __name__ == "__main__":
    try:
        unittest.main()
    except Exception:
        traceback.print_exc()
        raise
