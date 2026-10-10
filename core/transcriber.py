import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from core.config import NEURALNOTE_EXE, MUSESCORE_EXE, LMMS_EXE, AUDACITY_EXE
from core.config import RVC_PYTHON, BASE_DIR
from core.processes import run
from core.paths import application_data_paths


def _editor_location(kind,default):
    from core.component_location import resolve_editor_location
    return resolve_editor_location(application_data_paths(BASE_DIR)['config'],kind,default)


_EDITOR_EXTENSIONS = {
    'audacity': {'.wav', '.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.aiff', '.aif'},
    'musescore': {'.mid', '.midi', '.musicxml', '.xml', '.mscz', '.mscx'},
    'lmms': {'.mid', '.midi', '.mmp', '.mmpz', '.xml'},
    'neuralnote': {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus', '.aiff', '.aif'},
}


def validate_editor_input(kind, path):
    """Validate the file we can submit to an external editor.

    This proves only that the path is present and has a supported extension;
    the external application must still confirm that it finished importing.
    """
    if kind not in _EDITOR_EXTENSIONS:
        return False, f'不支持的编辑器类型：{kind}'
    if not path:
        return False, '没有可导入的文件'
    absolute=os.path.abspath(os.fspath(path))
    if not os.path.isfile(absolute):
        return False, f'文件不存在：{absolute}'
    if os.path.splitext(absolute)[1].lower() not in _EDITOR_EXTENSIONS[kind]:
        return False, f'{kind} 不支持此文件格式：{os.path.basename(absolute)}'
    return True, absolute

def transcribe_audio(input_path, output_midi, bpm=120):
    if not 30 <= float(bpm) <= 300:
        raise ValueError('BPM 超出 30~300 范围')
    os.makedirs(os.path.dirname(os.path.abspath(output_midi)), exist_ok=True)
    run([RVC_PYTHON, os.path.join(BASE_DIR, 'core', 'creative_worker.py'), 'transcribe',
         os.path.abspath(input_path), os.path.abspath(output_midi), '--bpm', str(bpm)], timeout=14400)
    with open(output_midi, 'rb') as f:
        if f.read(4) != b'MThd':
            raise RuntimeError('扒谱未返回有效 MIDI')
    return output_midi

def launch_neuralnote(audio_path=None):
    """启动 NeuralNote 进行 AI 扒谱"""
    executable=_editor_location('neuralnote',NEURALNOTE_EXE)
    if os.path.exists(executable):
        cmd = [executable]
        if audio_path:
            valid,_=validate_editor_input('neuralnote',audio_path)
            if not valid:return False
            cmd.append(os.path.abspath(audio_path))
        try:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return True
        except OSError:
            return False
    return False

def open_in_musescore(midi_or_audio_path):
    """在 MuseScore 中打开乐谱工程进行排版与打印"""
    valid,_=validate_editor_input('musescore',midi_or_audio_path)
    executable=_editor_location('musescore',MUSESCORE_EXE)
    if os.path.exists(executable) and valid:
        cmd = [executable, os.path.abspath(midi_or_audio_path)]
        try:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return True
        except OSError:
            return False
    return False

def open_in_lmms(project_or_midi_path):
    """在 LMMS 中打开编曲工程"""
    valid,_=validate_editor_input('lmms',project_or_midi_path)
    executable=_editor_location('lmms',LMMS_EXE)
    if os.path.exists(executable) and valid:
        absolute=os.path.abspath(project_or_midi_path)
        try:
            if str(project_or_midi_path).lower().endswith(('.mid', '.midi')):
                from core.lmms_project import export_lmms_project
                data = application_data_paths(BASE_DIR)
                absolute = export_lmms_project(absolute, Path(data['output']) / 'lmms',
                    os.path.join(BASE_DIR, 'runtime', 'addons', 'pretty_midi', 'TimGM6mb.sf2'))
            cmd = [executable, '--config', lmms_config(), absolute]
            subprocess.Popen(cmd, cwd=os.path.dirname(executable), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return True
        except OSError:
            return False
    return False

def lmms_config(base_dir=None):
    """Workspace-only configuration; retain any existing user preferences."""
    app_root = os.path.abspath(base_dir or BASE_DIR)
    data_paths = application_data_paths(app_root)
    path = os.path.join(str(data_paths['config']), 'lmms.xml')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        working = os.path.join(str(data_paths['output']), 'lmms')
        os.makedirs(working, exist_ok=True)
        soundfont = os.path.join(app_root, 'runtime', 'addons', 'pretty_midi', 'TimGM6mb.sf2')
        root = ET.Element('lmms', version='1.2.2')
        ET.SubElement(root, 'paths', workingdir=working+os.sep, defaultsf2=soundfont,
                      sf2dir=os.path.dirname(soundfont)+os.sep)
        # LMMS 1.2.2 otherwise opens its modal first-run SetupDialog before
        # processing --import. Only seed our new workspace profile; existing
        # editor preferences remain owned by the user.
        ET.SubElement(root, 'app', configured='1')
        ET.SubElement(root, 'mixer', audiodev='SDL (Simple DirectMedia Layer)',
                      framesperaudiobuffer='1024')
        ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path

def open_in_audacity(audio_path):
    """在 Audacity 4.0 工作台中打开进行人工修音检查"""
    valid,_=validate_editor_input('audacity',audio_path)
    executable=_editor_location('audacity',AUDACITY_EXE)
    if os.path.exists(executable) and valid:
        cmd = [executable, os.path.abspath(audio_path)]
        try:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return True
        except OSError:
            return False
    return False
