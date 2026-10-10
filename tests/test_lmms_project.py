import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.append(str(ROOT/'runtime/addons'))
import mido
from core.lmms_project import export_lmms_project
from core import transcriber


class LMMSProjectTests(unittest.TestCase):
    def test_overlap_tempo_changes_and_source_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'来源.mid'; sf=root/'font.sf2';sf.write_bytes(b'fixture')
            midi=mido.MidiFile(ticks_per_beat=480);track=mido.MidiTrack();midi.tracks.append(track)
            track.extend([mido.MetaMessage('set_tempo',tempo=500000),
                mido.Message('program_change',program=4),mido.Message('note_on',note=60,velocity=100),
                mido.Message('note_on',note=64,velocity=70,time=240),
                mido.MetaMessage('set_tempo',tempo=1000000,time=240),
                mido.Message('pitchwheel',pitch=4096),
                mido.Message('note_off',note=60,time=240),mido.Message('note_off',note=64,time=240)])
            midi.save(source);original=source.read_bytes()
            result=Path(export_lmms_project(source,root/'out',sf))
            project=ET.parse(result).getroot();notes=project.findall('.//note')
            self.assertEqual(project.find('head').get('bpm'),'120')
            self.assertEqual([(int(n.get('key')),int(n.get('pos')),int(n.get('len'))) for n in notes],
                             [(48,0,96),(52,24,120)])
            self.assertEqual(project.find('.//sf2player').get('patch'),'4')
            pitch = project.find('.//instrumenttrack/pitch')
            curve = project.find('.//automationpattern')
            self.assertEqual(curve.find('object').get('id'),pitch.get('id'))
            self.assertEqual([(int(p.get('pos')),float(p.get('value'))) for p in curve.findall('time')],
                             [(0,0.),(48,100.)])
            self.assertEqual(source.read_bytes(),original)
            with mock.patch.dict(os.environ, {'AI_MUSIC_PROCESSOR_DATA_ROOT':str(root/'data')}), \
                 mock.patch.object(transcriber,'LMMS_EXE',str(sf)), \
                 mock.patch.object(transcriber,'BASE_DIR',str(ROOT)), mock.patch.object(transcriber.subprocess,'Popen') as launch:
                self.assertTrue(transcriber.open_in_lmms(str(source)))
                args=launch.call_args[0][0]
                self.assertNotIn('--import',args)
                self.assertTrue(args[-1].endswith('.mmp'))

    def test_controller_effects_and_broken_notes_fail_without_writing_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sf=root/'font.sf2';sf.write_bytes(b'fixture');source=root/'in.mid'
            for message in (mido.Message('control_change',control=64,value=127),
                            mido.Message('note_on',note=60,velocity=100)):
                midi=mido.MidiFile();midi.tracks.append(mido.MidiTrack([message]));midi.save(source)
                with self.assertRaises(ValueError):export_lmms_project(source,root/'out',sf)
                self.assertFalse((root/'out').exists())


if __name__=='__main__':unittest.main()
