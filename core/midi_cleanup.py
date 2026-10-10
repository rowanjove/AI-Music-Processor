"""File-facing opt-in MIDI cleanup preserving tracks, channels and polyphony."""
import collections
import hashlib
import io
import json
import os
import struct
import sys
import uuid
from pathlib import Path

ALGORITHM_VERSION = 'polyphonic-midi-cleanup-v1'
MAX_BYTES = 16 * 1024 ** 2
MAX_EVENTS = 1000000


def _check(cancel):
    if cancel is not None:
        if callable(cancel):
            cancel()
        elif hasattr(cancel, 'check'):
            cancel.check()
        else:
            cancel.check_cancel()


def _preflight(data, cancel):
    """Bound SMF lengths and four-byte VLQs before the general MIDI parser."""
    if len(data) < 14 or data[:4] != b'MThd' or data[4:8] != b'\0\0\0\6':
        raise ValueError('MIDI cleanup requires a plain SMF header')
    kind, tracks, resolution = struct.unpack('>HHH', data[8:14])
    if kind not in (0, 1, 2) or not tracks or (kind == 0 and tracks != 1) or not 0 < resolution < 0x8000:
        raise ValueError('Invalid MIDI track/resolution header')
    position, events = 14, 0
    for _ in range(tracks):
        _check(cancel)
        if data[position:position+4] != b'MTrk' or position+8 > len(data):
            raise ValueError('Missing MIDI track chunk')
        length = struct.unpack('>I', data[position+4:position+8])[0]
        position += 8
        end, running = position+length, None
        if end > len(data):
            raise ValueError('MIDI track length exceeds file')
        def vlq():
            nonlocal position
            value = 0
            for index in range(4):
                if position >= end:
                    raise ValueError('Truncated MIDI variable length')
                byte = data[position]; position += 1
                value = (value << 7) | (byte & 127)
                if byte < 128:
                    return value
            raise ValueError('MIDI variable length exceeds four bytes')
        while position < end:
            events += 1
            if events > MAX_EVENTS:
                raise ValueError('MIDI event budget exceeded')
            if events % 1000 == 0:
                _check(cancel)
            vlq()
            if position >= end:
                raise ValueError('Truncated MIDI event')
            status = data[position]
            if status >= 128:
                position += 1
            elif running is not None:
                status = running
            else:
                raise ValueError('MIDI running status has no channel event')
            if status == 255:
                running = None
                if position >= end:
                    raise ValueError('Truncated MIDI meta event')
                position += 1
                amount = vlq()
                position += amount
            elif status in (240, 247):
                running = None
                amount = vlq()
                position += amount
            elif 128 <= status < 240:
                running = status
                count = 1 if status >> 4 in (12, 13) else 2
                if position+count > end or any(byte >= 128 for byte in data[position:position+count]):
                    raise ValueError('Invalid MIDI channel data')
                position += count
            else:
                raise ValueError('Unsupported MIDI system event')
            if position > end:
                raise ValueError('MIDI event exceeds its track')
    if position != len(data):
        raise ValueError('Trailing MIDI chunks/data are not part of this score')


def _merge_notes(notes):
    grouped, merged, count = collections.defaultdict(list), [], 0
    for note in notes:
        grouped[(note['on'].channel, note['on'].note, note['program'])].append(note)
    for group in grouped.values():
        current = None
        for note in sorted(group, key=lambda value: (value['start'], value['end'])):
            if current is not None and note['start'] < current['end']:
                current['end'] = max(current['end'], note['end'])
                count += 1
            else:
                current = note
                merged.append(current)
    return merged, count


def cleanup_midi_file(source, dest, quantize=False, grid_division=4, merge_overlaps=False,
                      velocity_range=None, report_path=None, cancel=None):
    addons = str(Path(__file__).resolve().parents[1] / 'runtime' / 'addons')
    if addons not in sys.path:
        sys.path.append(addons)
    import mido
    source, dest = os.fspath(source), os.fspath(dest)
    report_path = os.fspath(report_path or (str(Path(dest).with_suffix(''))+'.cleanup.json'))
    if len({os.path.normcase(os.path.abspath(path)) for path in (source, dest, report_path)}) != 3:
        raise ValueError('MIDI cleanup cannot overwrite its source or combine audio/report paths')
    if type(quantize) is not bool or type(merge_overlaps) is not bool or type(grid_division) is not int or not 1 <= grid_division <= 32:
        raise ValueError('Invalid MIDI cleanup grid/policy')
    if velocity_range is not None:
        if not isinstance(velocity_range, (list, tuple)) or len(velocity_range) != 2 or any(type(value) is not int for value in velocity_range) or not 1 <= velocity_range[0] <= velocity_range[1] <= 127:
            raise ValueError('Invalid MIDI velocity bounds')
    _check(cancel)
    with open(source, 'rb') as handle:
        data = handle.read(MAX_BYTES+1)
    if len(data) > MAX_BYTES:
        raise ValueError('MIDI file exceeds the cleanup byte budget')
    _preflight(data, cancel)
    midi = mido.MidiFile(file=io.BytesIO(data))
    result = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat, charset=midi.charset)
    counts = {'notes_before': 0, 'notes_after': 0, 'duplicates_removed': 0,
              'overlaps_merged': 0, 'orphan_noteoffs_removed': 0, 'unclosed_notes_released': 0,
              'zero_length_notes_removed': 0, 'quantized_notes': 0, 'velocities_changed': 0}
    for track in midi.tracks:
        _check(cancel)
        pending, programs, notes, other = collections.defaultdict(collections.deque), {}, [], []
        tick = 0
        for order, message in enumerate(track):
            tick += message.time
            if message.type == 'program_change':
                programs[message.channel] = message.program
            if message.type == 'note_on' and message.velocity:
                counts['notes_before'] += 1
                pending[(message.channel, message.note)].append((tick, order, message.copy(), programs.get(message.channel)))
            elif message.type == 'note_off' or (message.type == 'note_on' and not message.velocity):
                key = (message.channel, message.note)
                if not pending[key]:
                    counts['orphan_noteoffs_removed'] += 1
                    continue
                start, onset_order, onset, program = pending[key].popleft()
                if tick <= start:
                    counts['zero_length_notes_removed'] += 1
                else:
                    notes.append({'start': start, 'end': tick, 'on': onset, 'off': message.copy(),
                                  'order': onset_order, 'program': program})
            elif message.type != 'end_of_track':
                other.append((tick, 1, order, message.copy(time=0)))
        track_end = tick
        for queue in pending.values():
            for start, order, onset, program in queue:
                counts['unclosed_notes_released'] += 1
                notes.append({'start': start, 'end': max(start+1, track_end), 'on': onset,
                              'off': mido.Message('note_off', channel=onset.channel, note=onset.note, velocity=0),
                              'order': order, 'program': program})
        unique, cleaned = set(), []
        grid = midi.ticks_per_beat/grid_division
        for note in sorted(notes, key=lambda value: (value['start'], value['end'], value['order'])):
            identity = (note['on'].channel, note['on'].note, note['on'].velocity, note['start'], note['end'], note['program'])
            if identity in unique:
                counts['duplicates_removed'] += 1
                continue
            unique.add(identity)
            cleaned.append(note)
        if merge_overlaps:
            cleaned, count = _merge_notes(cleaned)
            counts['overlaps_merged'] += count
        for note in cleaned:
            if quantize:
                previous = (note['start'], note['end'])
                note['start'] = int(round(round(note['start']/grid)*grid))
                note['end'] = max(note['start']+max(1, int(round(grid))), int(round(round(note['end']/grid)*grid)))
                counts['quantized_notes'] += int(previous != (note['start'], note['end']))
            if velocity_range is not None:
                previous = note['on'].velocity
                note['on'].velocity = max(velocity_range[0], min(velocity_range[1], previous))
                counts['velocities_changed'] += int(previous != note['on'].velocity)
        if merge_overlaps:
            cleaned, count = _merge_notes(cleaned)
            counts['overlaps_merged'] += count
        counts['notes_after'] += len(cleaned)
        events = list(other)
        for note in cleaned:
            events.append((note['start'], 2, note['order'], note['on'].copy(time=0)))
            off = note['off']
            if off.type == 'note_on':
                off = mido.Message('note_off', channel=off.channel, note=off.note, velocity=0)
            events.append((note['end'], 0, note['order'], off.copy(time=0)))
        output, last_tick = mido.MidiTrack(), 0
        for event_tick, _, _, message in sorted(events, key=lambda value: value[:3]):
            output.append(message.copy(time=event_tick-last_tick))
            last_tick = event_tick
        output.append(mido.MetaMessage('end_of_track', time=max(track_end, last_tick)-last_tick))
        result.tracks.append(output)
    _check(cancel)
    for path in (dest, report_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = '.part.'+uuid.uuid4().hex
    try:
        result.save(dest+temporary)
        with open(dest+temporary, 'rb') as handle:
            output_data = handle.read(MAX_BYTES+1)
        if len(output_data) > MAX_BYTES:
            raise ValueError('Cleaned MIDI exceeds its byte budget')
        _preflight(output_data, cancel)
        report = {'schema_version': 1, 'algorithm': ALGORITHM_VERSION, 'counts': counts,
                  'tracks': len(result.tracks), 'ticks_per_beat': result.ticks_per_beat,
                  'quantize': quantize, 'grid_division': grid_division, 'merge_overlaps': merge_overlaps,
                  'velocity_range': velocity_range, 'source_sha256': hashlib.sha256(data).hexdigest(),
                  'output_sha256': hashlib.sha256(output_data).hexdigest(),
                  'tempo_meter_key_and_non_note_events': 'preserved', 'cross_pitch_polyphony': 'preserved'}
        with open(report_path+temporary, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, allow_nan=False, indent=2)
        _check(cancel)
        os.replace(report_path+temporary, report_path)
        os.replace(dest+temporary, dest)
    finally:
        for path in (dest+temporary, report_path+temporary):
            if os.path.isfile(path):
                os.remove(path)
    return {'midi': dest, 'decision': report_path, 'report': report}
