import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / 'runtime' / 'addons'))
import mido
from core.midi_cleanup import cleanup_midi_file


def read_events(path):
    midi = mido.MidiFile(path)
    tracks = []
    for track in midi.tracks:
        tick, events = 0, []
        for message in track:
            tick += message.time
            events.append((tick, message))
        tracks.append(events)
    return midi, tracks


class MIDICleanupTests(unittest.TestCase):
    def test_duplicate_cleanup_preserves_chords_channels_tracks_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.mid', Path(directory)/'out.mid'
            midi = mido.MidiFile(ticks_per_beat=480)
            track = mido.MidiTrack([
                mido.MetaMessage('set_tempo', tempo=500000), mido.MetaMessage('time_signature', numerator=3, denominator=4),
                mido.MetaMessage('key_signature', key='C'), mido.Message('program_change', channel=0, program=4),
                mido.Message('note_on', note=60, velocity=80), mido.Message('note_on', note=60, velocity=80),
                mido.Message('note_on', note=64, velocity=70), mido.Message('note_on', note=67, velocity=90),
                mido.Message('note_on', channel=1, note=60, velocity=60),
                mido.Message('note_off', note=60, time=480), mido.Message('note_off', note=60),
                mido.Message('note_off', note=64), mido.Message('note_off', note=67),
                mido.Message('note_off', channel=1, note=60), mido.MetaMessage('end_of_track')])
            midi.tracks.extend([track, mido.MidiTrack([mido.MetaMessage('track_name', name='another track'), mido.MetaMessage('end_of_track')])])
            midi.save(source)
            before = source.read_bytes()
            result = cleanup_midi_file(source, dest)
            cleaned, tracks = read_events(dest)
            onsets = [message for _, message in tracks[0] if message.type == 'note_on']
            self.assertEqual(sorted((note.channel, note.note) for note in onsets), [(0, 60), (0, 64), (0, 67), (1, 60)])
            self.assertEqual(result['report']['counts']['duplicates_removed'], 1)
            self.assertEqual(len(cleaned.tracks), 2)
            self.assertEqual(cleaned.ticks_per_beat, 480)
            self.assertEqual([message.type for _, message in tracks[0]][:4], ['set_tempo', 'time_signature', 'key_signature', 'program_change'])
            self.assertEqual(source.read_bytes(), before)

    def test_optional_grid_velocity_and_same_pitch_overlap_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.mid', Path(directory)/'out.mid'
            midi = mido.MidiFile(ticks_per_beat=480)
            midi.tracks.append(mido.MidiTrack([
                mido.Message('note_on', note=60, velocity=10, time=13),
                mido.Message('note_on', note=64, velocity=100),
                mido.Message('note_on', note=60, velocity=30, time=100),
                mido.Message('note_off', note=60, time=20),
                mido.Message('note_off', note=64), mido.Message('note_off', note=60, time=100)]))
            midi.save(source)
            result = cleanup_midi_file(source, dest, quantize=True, merge_overlaps=True, velocity_range=(40, 90))
            _, tracks = read_events(dest)
            notes = [(tick, message) for tick, message in tracks[0] if message.type in ('note_on', 'note_off')]
            self.assertEqual(sorted(message.note for _, message in notes if message.type == 'note_on'), [60, 64])
            self.assertTrue(all(tick % 120 == 0 for tick, _ in notes))
            self.assertTrue(all(40 <= message.velocity <= 90 for _, message in notes if message.type == 'note_on'))
            self.assertEqual(result['report']['counts']['overlaps_merged'], 1)

    def test_unclosed_notes_are_released_orphans_removed_and_bad_vlq_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.mid', Path(directory)/'out.mid'
            midi = mido.MidiFile()
            midi.tracks.append(mido.MidiTrack([mido.Message('note_off', note=40),
                mido.Message('note_on', note=60, velocity=90), mido.MetaMessage('end_of_track', time=300)]))
            midi.save(source)
            result = cleanup_midi_file(source, dest)
            self.assertEqual(result['report']['counts']['orphan_noteoffs_removed'], 1)
            self.assertEqual(result['report']['counts']['unclosed_notes_released'], 1)
            _, events = read_events(dest)
            self.assertTrue(any(tick == 300 and message.type == 'note_off' for tick, message in events[0]))
            before = dest.read_bytes()
            # Oversized variable-length integer is refused before mido parsing.
            source.write_bytes(b'MThd\0\0\0\6\0\0\0\1\1\xe0MTrk\0\0\0\6\x81\x81\x81\x81\x00\x00')
            with self.assertRaises(ValueError):
                cleanup_midi_file(source, dest)
            self.assertEqual(dest.read_bytes(), before)

    def test_cancel_and_inplace_never_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory)/'in.mid', Path(directory)/'out.mid'
            midi = mido.MidiFile(); midi.tracks.append(mido.MidiTrack([mido.MetaMessage('end_of_track')]))
            midi.save(source)
            with self.assertRaises(ValueError):
                cleanup_midi_file(source, source)
            def cancel():
                raise RuntimeError('cancelled')
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                cleanup_midi_file(source, dest, cancel=cancel)
            self.assertFalse(dest.exists())


if __name__ == '__main__':
    unittest.main()
