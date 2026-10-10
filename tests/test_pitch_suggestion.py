import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_available_rvc_models
from core.pitch_suggestion import suggest_pitch


class PitchSuggestionTests(unittest.TestCase):
    def test_known_models_use_their_own_hints(self):
        models = get_available_rvc_models()
        sunyanzi = suggest_pitch(models["sunyanzi"], "male")
        zhoujielun = suggest_pitch(models["zhoujielun"], "female")
        same_gender = suggest_pitch(models["zhoujielun"], "male")
        zhaolusi = suggest_pitch(models["zhaolusi"], "male")
        self.assertEqual(sunyanzi["semitones"], 12)
        self.assertEqual(sunyanzi["text"], "建议 12 半音")
        self.assertFalse(sunyanzi["applied"])
        self.assertEqual(zhoujielun["semitones"], -12)
        self.assertEqual(zhoujielun["text"], "建议 -12 半音")
        self.assertNotEqual(zhoujielun["semitones"], 12)
        self.assertEqual(same_gender["semitones"], 0)
        self.assertEqual(same_gender["text"], "建议 0 半音")
        self.assertEqual(zhaolusi["semitones"], models["zhaolusi"]["default_pitch_male"])
        self.assertEqual(sunyanzi["semitones"], models["sunyanzi"]["default_pitch_male"])
        self.assertEqual(zhoujielun["semitones"], models["zhoujielun"]["default_pitch_female"])

    def test_unknown_model_has_no_suggestion(self):
        unknown = {
            "gender": "unknown",
            "default_pitch_male": 12,
            "default_pitch_female": -12,
        }
        result = suggest_pitch(unknown, "male")
        self.assertEqual(result["semitones"], 0)
        self.assertEqual(result["text"], "无建议")
        self.assertFalse(result["applied"])
        self.assertEqual(suggest_pitch(None, "female")["text"], "无建议")
        self.assertEqual(suggest_pitch({"gender": "female", "default_pitch_male": 12}, None), {
            "semitones": 0, "text": "无建议", "applied": False, "reason": "没有输入性别",
        })
        self.assertEqual(suggest_pitch({"gender": "female", "default_pitch_male": True}, "male")["text"], "无建议")
        self.assertEqual(suggest_pitch({"gender": "female", "default_pitch_male": 25}, "male")["text"], "无建议")

    def test_request_keeps_the_submitted_pitch(self):
        models = get_available_rvc_models()
        original = dict(models["sunyanzi"])
        request = {"rvc_model_key": "sunyanzi", "pitch_shift": 7}
        suggestion = suggest_pitch(models["sunyanzi"], "male")
        self.assertEqual(suggestion["semitones"], 12)
        self.assertEqual(request["pitch_shift"], 7)
        self.assertFalse(suggestion["applied"])
        self.assertEqual(models["sunyanzi"]["default_pitch_male"], original["default_pitch_male"])
        for relative in ("core/pipeline.py", "core/job_spec.py", "process_music.py", "AI_Music_Processor.pyw"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("pitch_suggestion", source)
            self.assertNotIn("suggest_pitch", source)
        cli = (ROOT / "process_music.py").read_text(encoding="utf-8")
        self.assertIn("pitch_shift=args.pitch", cli)
        gui = (ROOT / "AI_Music_Processor.pyw").read_text(encoding="utf-8")
        self.assertIn("pitch_shift=int(self.pitch.get())", gui)
        self.assertNotIn("default_pitch_male", gui)
        self.assertNotIn("default_pitch_female", gui)
