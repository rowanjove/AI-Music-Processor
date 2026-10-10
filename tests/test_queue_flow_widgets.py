import gc
import importlib.machinery
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.ux_flow import first_run_preferences


class QueueFlowWidgetTests(unittest.TestCase):
    def test_first_run_preferences_validate_real_output_and_cache_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'成品 空格'
            self.assertEqual(first_run_preferences(str(output),'2.5'),
                {'custom_output_dir':str(output),'quota_bytes':int(2.5*1024**3)})
            self.assertIsNone(first_run_preferences(str(output),'')['quota_bytes'])
            file=Path(folder)/'file';file.write_bytes(b'original')
            for directory,quota in (('',20),(str(file),20),(str(output),'NaN'),(str(output),'inf'),
                                    (str(output),'1e308'),(str(output),'-1'),(str(output),'0')):
                with self.assertRaises(ValueError):first_run_preferences(directory,quota)
            self.assertFalse(output.exists())
            self.assertEqual(file.read_bytes(),b'original')

    def test_actual_tk_panels_follow_queue_modes_and_clear_previous_skips(self):
        import tkinter as tk
        ui=sys.modules.get('AI_Music_Processor') or importlib.machinery.SourceFileLoader(
            'AI_Music_Processor',str(ROOT/'AI_Music_Processor.pyw')).load_module()
        root=tk.Tk();root.withdraw()
        frame=ui.ttk.Frame(root)
        fake=SimpleNamespace(mode_flow=frame,is_running=True)
        try:
            for mode,params,frozen in (
                    ('polish',{},True),
                    ('voice_conversion',{'pitch_correction_mode':'off','mix_ducking':False},True),
                    ('transcribe',{'midi_cleanup':{'enabled':True}},True),
                    ('voice_conversion',{'pitch_correction_mode':'auto','generate_midi':True,
                                         'parameter_search_enabled':True,'midi_cleanup':{'enabled':True}},True)):
                fake.running_flow=dict(mode=mode,parameters=params,frozen=frozen)
                ui.AIMusicProcessorApp.refresh_mode_flow(fake)
                panel=fake.mode_flow_panel
                self.assertEqual(panel.mode,mode)
                self.assertIsNone(panel.current_stage)
                self.assertEqual(getattr(panel,'stage_states',{}),{})
                if mode=='voice_conversion':
                    ui.highlight_mode_stage(panel,'PitchCorrection','跳过：Off')
                    self.assertEqual(panel.stage_states['PitchCorrection'],'SKIPPED')
                    ui.highlight_mode_stage(panel,'PitchCorrection','开始；阶段总量未知')
                    self.assertEqual(panel.stage_states['PitchCorrection'],'RUNNING')
            self.assertIn('RVCSearch',fake.mode_flow_panel.stage_labels)
            self.assertIn('Transcribe',fake.mode_flow_panel.stage_labels)
        finally:
            frame.destroy();root.destroy();gc.collect()


if __name__=='__main__':unittest.main()
