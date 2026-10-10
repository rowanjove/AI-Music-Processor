"""Bounded MIDI-to-LMMS bridge preserving playback time and source bytes.

LMMS 1.2.2's native MIDI importer does not preserve the supplied tempo. This
bridge maps the global MIDI tempo timeline onto a constant LMMS tempo, retains
overlapping notes/programs, and exports a separate editable project. Controller
effects require an explicit external MIDI editor rather than silently losing
them. LMMS uses 48 ticks/beat and key 0 represents MIDI note 12.
"""
import collections
import hashlib
import io
import json
import math
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from core.midi_cleanup import MAX_BYTES, _preflight


def export_lmms_project(source, output_directory, soundfont):
    source, directory, soundfont = Path(source), Path(output_directory), Path(soundfont)
    if not soundfont.is_file():
        raise FileNotFoundError('LMMS SoundFont 不存在：' + str(soundfont))
    with source.open('rb') as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('MIDI 超过 16 MiB 安全上限')
    _preflight(data, None)
    addons = str(Path(__file__).resolve().parents[1] / 'runtime' / 'addons')
    if addons not in sys.path:
        sys.path.append(addons)
    import mido
    midi = mido.MidiFile(file=io.BytesIO(data))
    if midi.type == 2 or len(midi.tracks) > 256:
        raise ValueError('LMMS 转换要求同步 MIDI（SMF 0/1，最多 256 轨）')
    messages = list(mido.merge_tracks(midi.tracks))
    if any(m.type == 'set_tempo' and not 1 <= m.tempo <= 0xffffff for m in messages):
        raise ValueError('MIDI 速度事件无效')
    tempo = 500000
    first_tempo = next((m.tempo for m in messages if m.type == 'set_tempo'), tempo)
    bpm = 60000000 / first_tempo
    if not math.isfinite(bpm) or not 10 <= bpm <= 999:
        raise ValueError('MIDI 初始速度超出 LMMS 范围')
    programs = collections.defaultdict(int)
    active = collections.defaultdict(collections.deque)
    notes, elapsed, tempo_changes = [], 0., 0
    pitch_events = collections.defaultdict(list)
    signature = (4, 4)
    for message in messages:
        elapsed += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if elapsed > 86400:
            raise ValueError('MIDI 时长超过 24 小时安全上限')
        if message.type == 'set_tempo':
            tempo = message.tempo
            tempo_changes += 1
        elif message.type == 'time_signature':
            if elapsed == 0:
                signature = (message.numerator, message.denominator)
            elif (message.numerator, message.denominator) != signature:
                raise ValueError('变拍号 MIDI 请在 MIDI 编辑器中打开，不能静默丢弃拍号')
        elif message.type == 'program_change':
            programs[message.channel] = message.program
        elif message.type == 'note_on' and message.velocity:
            if not 12 <= message.note <= 119 or message.channel == 9:
                raise ValueError('此 MIDI 含 LMMS SoundFont 桥接不支持的音高或鼓通道')
            active[message.channel, message.note].append(
                (elapsed, message.velocity, programs[message.channel]))
        elif message.type == 'note_off' or (message.type == 'note_on' and not message.velocity):
            queue = active[message.channel, message.note]
            if queue:
                start, velocity, program = queue.popleft()
                if elapsed <= start:
                    raise ValueError('MIDI 包含零长度音符')
                notes.append((message.channel, program, message.note, start, elapsed, velocity))
                if len(notes) > 100000:
                    raise ValueError('MIDI 超过 100000 音符安全上限')
        elif message.type == 'pitchwheel':
            pitch_events[message.channel].append((elapsed, message.pitch * 200 / 8192))
        elif message.type in ('control_change', 'aftertouch', 'polytouch', 'sysex'):
            raise ValueError('此 MIDI 含控制器/弯音效果，请在 MIDI 编辑器中打开；桥接不能静默丢弃效果')
    if any(active.values()) or not notes:
        raise ValueError('MIDI 含未闭合音符或没有可转换音符')
    root = ET.Element('lmms-project', version='1.0', creator='AI Music Processor',
                      creatorversion='1.0', type='song')
    ET.SubElement(root, 'head', bpm=format(bpm, '.12g'), mastervol='100', masterpitch='0',
                  timesig_numerator=str(signature[0]), timesig_denominator=str(signature[1]))
    container = ET.SubElement(ET.SubElement(root, 'song'), 'trackcontainer', type='song')
    groups = collections.defaultdict(list)
    for note in notes:
        groups[note[:2]].append(note)
    timing_error = 0.
    for ordinal, ((channel, program), group) in enumerate(sorted(groups.items())):
        track = ET.SubElement(container, 'track', type='0', muted='0',
                              name='MIDI channel %d / program %d' % (channel+1, program))
        instrument = ET.SubElement(track, 'instrumenttrack', vol='100', pan='0', pitchrange='2',
                                   basenote='57', usemasterpitch='1', fxch='0')
        pitch_id = str(1001 + ordinal)
        ET.SubElement(instrument, 'pitch', id=pitch_id, value='0', scale_type='linear')
        ET.SubElement(ET.SubElement(instrument, 'instrument', name='sf2player'),
                      'sf2player', src=soundfont.resolve().as_posix(), bank='0', patch=str(program),
                      gain='1', reverbOn='0', chorusOn='0')
        pattern = ET.SubElement(track, 'pattern', type='1', pos='0', steps='16', muted='0', name='Imported MIDI')
        end_tick = 0
        for _, _, key, start, end, velocity in sorted(group, key=lambda n:(n[3], n[2], n[4])):
            pos = round(start*bpm*48/60)
            finish = max(pos+1, round(end*bpm*48/60))
            timing_error = max(timing_error, abs(pos*60/(bpm*48)-start), abs(finish*60/(bpm*48)-end))
            ET.SubElement(pattern, 'note', key=str(key-12), pos=str(pos), len=str(finish-pos),
                          vol=str(round(velocity*100/63)), pan='0')
            end_tick = max(end_tick, finish)
        pattern.set('len', str(max(192, math.ceil(end_tick/192)*192)))
        if pitch_events[channel]:
            automation = ET.SubElement(container, 'track', type='5', muted='0', name='Pitch / channel %d' % (channel+1))
            ET.SubElement(automation, 'automationtrack')
            points = {0: 0.}
            for timestamp, value in pitch_events[channel]:
                position = round(timestamp*bpm*48/60)
                points[position] = value
                timing_error = max(timing_error, abs(position*60/(bpm*48)-timestamp))
            curve = ET.SubElement(automation, 'automationpattern', mute='0', pos='0', prog='0',
                                  tens='1', len=str(max(end_tick, max(points)+1)))
            for position, value in sorted(points.items()):
                ET.SubElement(curve, 'time', pos=str(position), value=format(value, '.12g'))
            ET.SubElement(curve, 'object', id=pitch_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ('midi-' + uuid.uuid4().hex + '.mmp')
    report = dict(schema_version=1, source_path=str(source.resolve()), source_sha256=hashlib.sha256(data).hexdigest(),
                  note_count=len(notes), output_tempo_bpm=bpm, tempo_events=tempo_changes,
                  pitch_bend_events=sum(map(len,pitch_events.values())), pitch_range_semitones=2,
                  timing_mapping='MIDI elapsed seconds to constant-tempo LMMS ticks',
                  max_endpoint_rounding_seconds=timing_error, source_modified=False,
                  quality_acceptance='READY_FOR_EXTERNAL_VALIDATION')
    try:
        with target.open('xb') as handle:
            ET.ElementTree(root).write(handle, encoding='utf-8', xml_declaration=True)
        with Path(str(target)+'.json').open('x',encoding='utf-8') as handle:
            json.dump(report,handle,ensure_ascii=False,indent=2)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return str(target)
