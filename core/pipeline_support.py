"""File contracts and small adapters shared by stage implementations."""
from __future__ import annotations
import os, re, json, uuid, shutil
from typing import Any, Mapping, Optional
from core.config import FFMPEG_EXE
from core.separator import separate_audio
from core.storage import sha256_file, sha256_path
def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/*?:\"<>|\x00-\x1f]", "", name).strip().rstrip('. ')[:100]
    reserved = {'CON','PRN','AUX','NUL'} | {f'COM{i}' for i in range(1,10)} | {f'LPT{i}' for i in range(1,10)}
    return '_' + name if name.split('.')[0].upper() in reserved else name


def safe_log(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("gbk", errors="replace").decode("gbk"))


def _json_file(path: os.PathLike[str] | str, value: Any) -> str:
    target = os.path.abspath(os.fspath(path))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.remove(temporary)
    return target


def _existing_path(value: Any) -> Optional[str]:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str) and os.path.isfile(value):
        return os.path.abspath(value)
    return None


_OUTPUT_KEY_HINTS = {
    "wav", "mp3", "flac", "midi", "aligned", "alignment_json", "cleaned", "rvc", "mixed",
    "vocal", "drums", "bass", "other", "instrumental", "standard_wav", "qc_result", "report", 'master_json',
}


def _looks_like_output_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _OUTPUT_KEY_HINTS or lowered.endswith("_path") or lowered.endswith("_paths")


def _output_paths(value: Any, prefix: str = "", collect: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key)
            result.update(_output_paths(item, f"{prefix}{key_name}.", _looks_like_output_key(key_name)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            result.update(_output_paths(item, f"{prefix}{index}.", collect))
    elif collect and isinstance(value, (str, os.PathLike)):
        text = os.fspath(value)
        if os.path.isabs(text) or os.path.sep in text or (os.path.altsep and os.path.altsep in text) or os.path.splitext(text)[1].lower() in {".wav", ".mp3", ".flac", ".mid", ".midi", ".json", ".md"}:
            result[prefix.rstrip(".")] = os.path.abspath(text)
    return result


def _hash_inputs(values: Any) -> Any:
    if isinstance(values, Mapping):
        return {str(key): _hash_inputs(item) for key, item in sorted(values.items(), key=lambda pair: str(pair[0]))}
    if isinstance(values, (list, tuple)):
        return [_hash_inputs(item) for item in values]
    path = _existing_path(values)
    if path:
        return {"sha256": sha256_path(path), "kind": "file"}
    return values


def _validate_outputs(outputs: Any, hashes: Optional[Mapping[str, str]] = None) -> bool:
    paths = _output_paths(outputs)
    expected = dict(hashes or {})
    if set(paths) != set(expected):
        return not paths and not expected
    for key, path in paths.items():
        if not os.path.isfile(path):
            return False
        if sha256_file(path) != expected[key]:
            return False
    return True


def _hash_outputs(outputs: Any) -> dict[str, str]:
    return {key: sha256_file(path) for key, path in _output_paths(outputs).items() if os.path.isfile(path)}


def _infer_project_root(paths: list[str]) -> Optional[str]:
    markers = (os.sep + ".work" + os.sep, os.sep + "master" + os.sep, os.sep + "stems" + os.sep, os.sep + "reports" + os.sep, os.sep + "midi" + os.sep)
    for path in paths:
        lowered = path.lower()
        for marker in markers:
            index = lowered.find(marker.lower())
            if index >= 0:
                return path[:index]
    return None


def _relocate_cached_outputs(value: Any, source_root: Optional[str], target_root: Optional[str], prefix: str = "", collect: bool = False) -> Any:
    if not target_root:
        return value
    if not source_root:
        raise ValueError('Cached output has no project ownership root')
    source_root, target_root = map(os.path.abspath, (source_root, target_root))
    copies = {}
    def relocate(item, selected):
        if isinstance(item, Mapping):
            return {key:relocate(child,_looks_like_output_key(str(key))) for key,child in item.items()}
        if isinstance(item, (list, tuple)):
            children=[relocate(child,selected) for child in item]
            return tuple(children) if isinstance(item,tuple) else children
        if selected and isinstance(item,(str,os.PathLike)):
            source_path=os.path.abspath(os.fspath(item))
            if os.path.commonpath([source_path,source_root]) != source_root:
                raise ValueError('Cached output escapes its owning project')
            target_path=os.path.join(target_root,os.path.relpath(source_path,source_root))
            if os.path.commonpath([os.path.realpath(target_path),os.path.realpath(target_root)]) != os.path.realpath(target_root):
                raise ValueError('Cache target escapes its project through a link')
            if os.path.normcase(source_path) != os.path.normcase(target_path):
                copies[target_path]=source_path
            return target_path
        return item
    relocated=relocate(value,collect)
    staged, committed, backups = {}, [], {}
    try:
        # All files are copied and verified before exposing any final target.
        # Never return the source project path on a failed materialization.
        for target, source in copies.items():
            os.makedirs(os.path.dirname(target),exist_ok=True)
            temporary=target+'.cache-'+uuid.uuid4().hex+'.tmp'
            staged[target]=temporary
            shutil.copy2(source,temporary)
            if sha256_file(source) != sha256_file(temporary):
                raise OSError('Cached output changed while copying')
        for target, temporary in staged.items():
            if os.path.lexists(target):
                backup=target+'.cache-'+uuid.uuid4().hex+'.previous'
                os.replace(target,backup)
                backups[target]=backup
            os.replace(temporary,target)
            committed.append(target)
    except Exception:
        try:
            for target in reversed(committed):
                os.unlink(target)
            for target, backup in backups.items():
                os.replace(backup,target)
        except OSError as error:
            # Keep recoverable .previous backups and stop the job if rollback
            # cannot restore its targets. A retry must not hide data loss.
            raise RuntimeError('Cache materialization rollback failed; backups retained') from error
        raise
    finally:
        for temporary in staged.values():
            if os.path.exists(temporary):
                os.unlink(temporary)
    for backup in backups.values():
        os.unlink(backup)
    return relocated


def _copy_if_file(source: Any, target: str) -> Optional[str]:
    source_path = _existing_path(source)
    if source_path is None:
        return None
    target = os.path.abspath(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.abspath(source_path) != target:
        shutil.copy2(source_path, target)
    return target


def _normalise_stems(result: Any) -> dict[str, Optional[str]]:
    if isinstance(result, Mapping):
        values = {str(key).lower(): value for key, value in result.items()}
    elif isinstance(result, (list, tuple)):
        values = dict(zip(("vocal", "drums", "bass", "other"), result))
    else:
        raise RuntimeError("四轨分离器返回了无法识别的结果")
    aliases = {"vocals": "vocal", "voice": "vocal", "instrumental": "other", "music": "other"}
    for alias, canonical in aliases.items():
        if canonical not in values and alias in values:
            values[canonical] = values[alias]
    return {name: _existing_path(values.get(name)) for name in ("vocal", "drums", "bass", "other")}


def separate_four_stems(input_wav: str, output_dir: str, execution_policy=None) -> dict[str, Optional[str]]:
    """Run the optional four-stem addon, with a safe two-stem fallback."""
    os.makedirs(output_dir, exist_ok=True)
    try:
        from core.separator import separate_four_stems as addon_separate_four_stems
    except (ImportError, AttributeError):
        addon_separate_four_stems = None
    if addon_separate_four_stems is not None:
        return _normalise_stems(addon_separate_four_stems(input_wav, output_dir, **({'execution_policy': execution_policy} if execution_policy is not None else {})))
    vocal = os.path.join(output_dir, "vocal.wav")
    other = os.path.join(output_dir, "other.wav")
    separate_audio(input_wav, vocal, other, model_name="HP2_all_vocals.pth", agg=10, **({'execution_policy': execution_policy} if execution_policy is not None else {}))
    return {"vocal": vocal, "drums": None, "bass": None, "other": other}


def _transcribe(input_path: str, output_midi: str, bpm: int = 120) -> str:
    try:
        from core.transcriber import transcribe_audio
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("扒谱插件未安装：需要 core.transcriber.transcribe_audio") from exc
    result = transcribe_audio(input_path, output_midi, bpm=bpm)
    return _existing_path(result) or os.path.abspath(output_midi)


def _control_check(control: Any) -> None:
    if control is not None and getattr(control, "check", None) is not None:
        control.check()


def _file_fingerprint(path: Any) -> Optional[str]:
    try:
        return sha256_file(path) if isinstance(path, (str, os.PathLike)) and os.path.isfile(path) else None
    except OSError:
        return None


