import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import soundfile as sf
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.ab_preview import project_audio_roles,prepare_ab_preview,play_ab_preview,stop_ab_preview


class ABPreviewTests(unittest.TestCase):
    def test_roles_same_start_excerpt_and_immutable_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'原始.wav';master=root/'母带.wav'
            rate=48000;t=np.arange(rate*2)/rate
            audio=(.2*np.sin(2*np.pi*440*t)).astype('float32')
            sf.write(source,audio,rate,subtype='FLOAT');sf.write(master,audio*.5,rate,subtype='FLOAT')
            original=source.read_bytes()
            roles=project_audio_roles({'status':'DONE','source_path':str(source),'output_paths':{'master_wav':str(master)},
                'stage_records':[{'status':'DONE','stage_name':'Clean','outputs':{'cleaned':str(master)}}]})
            self.assertEqual(set(roles),{'原始输入','最终母带','Clean.cleaned'})
            a=prepare_ab_preview(source,root/'previews',.5,1.)
            b=prepare_ab_preview(master,root/'previews',.5,1.)
            self.assertEqual(a['start_seconds'],b['start_seconds'])
            data,sr=sf.read(a['preview_path'],always_2d=True)
            self.assertEqual((sr,len(data),data.shape[1]),(48000,48000,2))
            self.assertLess(np.max(np.abs(data[:,0]-audio[24000:72000])),1/32768)
            self.assertEqual(source.read_bytes(),original)
            with patch('winsound.PlaySound') as play:
                play_ab_preview(a);play_ab_preview(b);stop_ab_preview()
                self.assertEqual(play.call_args_list[-1].args,(None,0))
                self.assertEqual(play.call_args_list[0].args[0],a['preview_path'])
            Path(a['preview_path']).write_bytes(b'changed')
            with self.assertRaises(ValueError):play_ab_preview(a)

    def test_bad_time_or_incomplete_project_never_becomes_listening_pass(self):
        with self.assertRaises(ValueError):project_audio_roles({'status':'RUNNING'})
        for start,duration in ((-1,30),(float('nan'),30),(0,31),(0,float('inf'))):
            with self.assertRaises(ValueError):prepare_ab_preview('missing.wav','unused',start,duration)

    def test_loudness_matched_ab_pair(self):
        from core.ab_preview import prepare_ab_pair
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'a.wav'; master = root / 'b.wav'
            rate = 48000; t = np.arange(rate) / rate
            sf.write(source, (0.4 * np.sin(2 * np.pi * 440 * t)).astype('float32'), rate)
            sf.write(master, (0.1 * np.sin(2 * np.pi * 440 * t)).astype('float32'), rate)
            rec_a, rec_b = prepare_ab_pair(source, master, root / 'previews', 0.0, 0.5, match_loudness=True)
            self.assertTrue(rec_b['loudness_matched'])
            self.assertAlmostEqual(rec_a['rms'], rec_b['rms'], delta=0.05)

    def test_listening_evaluation_creation_and_anonymization(self):
        from core.polish_listening import create_listening_evaluation, save_listening_evaluation, export_anonymized_listening_summary, DEFECT_TAGS
        with tempfile.TemporaryDirectory() as folder:
            eval_record = create_listening_evaluation(
                case_id="case-42",
                listener="Alice Bob",
                date="2026-10-10",
                choice_a_role="原始输入",
                choice_b_role="最终母带",
                preferred_choice="B",
                defect_tags=["distortion", "sibilance"],
                notes="B 更自然，高频轻微齿音",
            )
            self.assertEqual(eval_record["listening_quality"], "assessed")
            self.assertEqual(eval_record["quality_acceptance"], "READY_FOR_EXTERNAL_VALIDATION")

            # Check invalid defect tag rejection
            with self.assertRaises(ValueError):
                create_listening_evaluation("case-42", "Alice", "2026-10-10", "A", "B", "B", defect_tags=["invalid_tag"])

            saved = save_listening_evaluation(eval_record, folder)
            self.assertTrue(Path(saved).is_file())

            # Export anonymized summary
            summary_path = Path(folder) / "summary.json"
            summary = export_anonymized_listening_summary([eval_record], summary_path)
            self.assertEqual(summary["total_evaluations"], 1)
            self.assertEqual(summary["choices_distribution"]["B"], 1)
            self.assertEqual(summary["defects_distribution"]["distortion"], 1)
            # Ensure personal name is redacted
            anon_record = summary["anonymized_evaluations"][0]
            self.assertNotIn("Alice Bob", str(anon_record))
            self.assertTrue(anon_record["evaluator_anon_id"].startswith("evaluator-"))


if __name__=='__main__':unittest.main()
