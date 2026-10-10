import os
import sys
import shutil
import threading
import json
import tempfile
import math
from core.paths import application_data_paths, is_release_mode

# 基础目录定位
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
RELEASE_MODE = is_release_mode(BASE_DIR)
_DATA_PATHS = application_data_paths(BASE_DIR, release_mode=RELEASE_MODE)
DATA_ROOT = str(_DATA_PATHS["data_root"])
OUTPUT_DIR = str(_DATA_PATHS["output"])
TEMP_DIR = str(_DATA_PATHS["temp"])
LOG_DIR = str(_DATA_PATHS["logs"])
STARTUP_ERROR_PATH = (os.path.join(LOG_DIR, 'startup_error.log') if RELEASE_MODE
                      else os.path.join(BASE_DIR, 'startup_error.log'))

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# 工具路径解析
RVC_ROOT = os.path.join(TOOLS_DIR, "RVC", "RVC-beta-v2-0618")

def resolve_rvc_python(base_dir=None, release_mode=None):
    root = os.path.abspath(base_dir or BASE_DIR)
    path = os.path.join(root, "tools", "RVC", "RVC-beta-v2-0618", "runtime", "python.exe")
    if os.path.isfile(path):
        return path
    strict = is_release_mode(root) if release_mode is None else bool(release_mode)
    if strict:
        raise FileNotFoundError("Portable release is missing its bundled Python runtime: " + path)
    return sys.executable

RVC_PYTHON = resolve_rvc_python(BASE_DIR, RELEASE_MODE)

UVR_ROOT = os.path.join(TOOLS_DIR, "UVR5")
AUDACITY_EXE = os.path.join(TOOLS_DIR, "Audacity", "Audacity-4.0.1.262721353[909d5f7]-x86_64", "bin", "Audacity4.exe")
NEURALNOTE_EXE = os.path.join(TOOLS_DIR, "NeuralNote", "NeuralNote.exe")
LMMS_EXE = os.path.join(TOOLS_DIR, "LMMS", "lmms.exe")
MUSESCORE_EXE = os.path.join(TOOLS_DIR, "MuseScore", "MuseScorePortable.exe")

CONFIG_DIR = str(_DATA_PATHS["config"])
ADDONS_DIR = os.path.join(BASE_DIR, 'runtime', 'addons')
APP_VERSION = '1.0.0-local'
SUPPORTED_EXTENSIONS = {'.wav', '.flac', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.aiff', '.aif'}
os.makedirs(CONFIG_DIR, exist_ok=True)

# Configuration diagnostics are kept in memory so the desktop UI can report a
# broken optional profile without preventing the rest of the application from
# starting.  The list is replaced on every model/settings refresh.
CONFIG_WARNINGS = []

MODEL_PROFILE_DEFAULTS = {
    'pitch': 0,
    'f0_method': 'harvest',
    'protect': .33,
    'filter_radius': 3,
    'rms_mix_rate': .25,
    'index_rate': .65,
    'commercial': None,
}
DEFAULT_CACHE_POLICY = {'enabled': True, 'quota_bytes': 20 * 1024**3, 'ttl_days': 30}
_PROFILE_RANGES = {
    'pitch': (-24, 24),
    'protect': (0.0, 0.5),
    'filter_radius': (0, 7),
    'rms_mix_rate': (0.0, 1.0),
    'index_rate': (0.0, 1.0),
}


def get_config_warnings():
    """Return a snapshot of non-fatal configuration warnings."""
    return list(CONFIG_WARNINGS)


def _warn_config(message):
    if message not in CONFIG_WARNINGS:
        CONFIG_WARNINGS.append(message)


def _normalize_cache_policy(value):
    """Keep malformed app cache settings from preventing application startup."""
    if value is None:
        return dict(DEFAULT_CACHE_POLICY)
    if not isinstance(value, dict):
        _warn_config('cache_policy 已忽略：必须是 JSON 对象')
        return dict(DEFAULT_CACHE_POLICY)
    normalized=dict(DEFAULT_CACHE_POLICY)
    enabled=value.get('enabled',normalized['enabled'])
    if not isinstance(enabled,bool):
        _warn_config('cache_policy.enabled 已恢复默认值')
    else:
        normalized['enabled']=enabled
    for key,minimum in (('quota_bytes',1),('ttl_days',0)):
        raw=value.get(key,normalized[key])
        if raw is None:
            normalized[key]=None
            continue
        if isinstance(raw,bool) or not isinstance(raw,(int,float)) or not math.isfinite(raw) or raw<minimum:
            _warn_config(f'cache_policy.{key} 已恢复默认值')
            continue
        normalized[key]=int(raw)
    return normalized


def _validate_model_profile(value, source='<profile>'):
    """Validate one JSON model profile and return a safe, normalized copy.

    Unknown keys are retained for forward compatibility, while known fields
    are type/range checked.  A malformed file is rejected as a unit so an
    accidental partial write cannot silently change inference settings.
    """
    if not isinstance(value, dict):
        raise ValueError('必须是 JSON 对象')
    profile = dict(MODEL_PROFILE_DEFAULTS)
    for key, item in value.items():
        if key in _PROFILE_RANGES:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f'{key} 必须是数字')
            low, high = _PROFILE_RANGES[key]
            if not low <= item <= high:
                raise ValueError(f'{key} 超出 {low}~{high}')
            profile[key] = int(item) if key in ('pitch', 'filter_radius') else float(item)
        elif key == 'f0_method':
            if item not in ('harvest', 'pm', 'crepe', 'rmvpe'):
                raise ValueError('f0_method 必须是 harvest、pm、crepe 或 rmvpe')
            profile[key] = item
        elif key == 'commercial':
            if item is not None and not isinstance(item, (str, dict)):
                raise ValueError('commercial 必须是字符串、对象或 null')
            profile[key] = item
        else:
            # Preserve metadata fields so existing profiles remain forward
            # compatible, but do not let them override the typed settings.
            profile[key] = item
    return profile


def save_model_profile(path, profile):
    """Validate and atomically save one model profile.

    The temporary file lives beside the destination, making ``os.replace`` an
    atomic same-volume operation on Windows.  Validation happens before the
    destination is touched.
    """
    normalized = _validate_model_profile(profile, path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.profile-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return normalized

def resolve_tool_path(name, base_dir=None, release_mode=None):
    root = os.path.abspath(base_dir or BASE_DIR)
    strict = is_release_mode(root) if release_mode is None else bool(release_mode)
    if strict:
        candidate = os.path.join(root, 'runtime', 'bin', name + '.exe')
        if os.path.isfile(candidate):
            return candidate
        if os.environ.get('AI_MUSIC_PROCESSOR_UI_PATHS_ONLY')=='1':
            # The UI may describe missing components while its runtime gate is
            # closed. This is a declared path, never a PATH fallback or success.
            return candidate
        raise FileNotFoundError("Portable release is missing its bundled tool: " + candidate)
    tools_dir = os.path.join(root, 'tools')
    rvc_root = os.path.join(tools_dir, 'RVC', 'RVC-beta-v2-0618')
    candidates = [os.path.join(tools_dir, 'ffmpeg', name + '.exe'), shutil.which(name),
                  os.path.join('C:\\ffmpeg\\bin', name + '.exe'), os.path.join(rvc_root, name + '.exe')]
    return next((p for p in candidates if p and os.path.isfile(p)), name)

def tool_path(name):
    return resolve_tool_path(name, BASE_DIR, RELEASE_MODE)

FFMPEG_EXE = tool_path('ffmpeg')
FFPROBE_EXE = tool_path('ffprobe')

def normalize_settings(value, strict=False):
    if not isinstance(value,dict):
        raise ValueError("App Settings 必须是对象")
    value=dict(value)
    def warn(message):
        if strict:raise ValueError(message)
        _warn_config(message)
    if 'execution_policy' in value:
        from core.execution_policy import validate_execution_policy
        try:value['execution_policy']=validate_execution_policy(value['execution_policy'])
        except (TypeError,ValueError):
            warn('app.json 冻结执行策略无效，已恢复默认');value.pop('execution_policy')
    typed = {'device':str,'performance_profile':str,'mode':str,'clean_preset':str,'master_preset':str,'rvc_model_key':str,
             'pitch_shift':int,'f0_method':str,'generate_midi':bool,'output_formats':list,
             'output_sample_rate':int,'custom_output_dir':str,'index_rate':(int,float),'bpm':(int,float),
             'first_run_completed':bool,'tutorial_dismissed':bool,
             'parameter_search_enabled':bool,'parameter_search_budget':str,
             'vocal_restoration_effect':str,'rvc_input_headroom_strategy':str,
             'vocal_restore_profile':dict,
             'pitch_correction_mode':str,
             'source_hint':str,'source_preclean_enabled':bool,
             'prevent_sleep':bool,'notify_in_app':bool,'notify_sound':bool,'notify_toast':bool,
             'completion_action':str}
    typed.update(device=str,performance_profile=str,rvc_postprocess_profile=str,adaptive_mix=bool,
                 mix_bus_profile=str,input_channel_mode=str,output_channels=int,detailed_analysis=bool)
    typed.update(mix_ducking=bool,mix_reverb=bool,quality_assist=bool,quality_retry_limit=int,
                 mix_vocal_gain_db=(int,float),mix_instrumental_gain_db=(int,float),transcribe_stem=str,
                 rvc_postprocess=bool,skip_vocal_eq=bool,skip_mix_compressor=bool,
                 rvc_input_gain_db=(int,float),polish_profile=str,polish_reference=str,
                 polish_options=dict,midi_cleanup=dict,separator_profile=dict,separator_backend=str,
                 rvc_input_gain_db_strategy=str)
    for key, kind in typed.items():
        if key in value and (not isinstance(value[key],kind) or (isinstance(value[key],bool) and kind != bool)):
            warn('app.json 字段类型错误，已恢复默认：'+key);value.pop(key)
    if 'output_formats' in value and any(not isinstance(v,str) or v not in ('wav','mp3','flac') for v in value['output_formats']):
        warn('app.json 输出格式错误，已恢复默认');value.pop('output_formats')
    for key in ('index_rate','bpm'):
        if key in value and not math.isfinite(value[key]):
            warn('app.json 非有限数值，已恢复默认：'+key);value.pop(key)
    from core.execution_policy import DEVICES, PROFILES
    for key, allowed in (('device', DEVICES), ('performance_profile', PROFILES),
                         ('mode',('polish','voice_conversion','transcribe')),
                         ('input_channel_mode',('auto','mono','stereo')),('output_channels',(1,2)),
                         ('transcribe_stem',('vocal','bass','other')),
                         ('rvc_postprocess_profile',('off','gentle')),('mix_bus_profile',('off','gentle')),
                         ('clean_preset',CLEAN_PRESETS),('master_preset',MASTER_PRESETS),
                         ('output_sample_rate',(44100,48000)),('f0_method',('harvest','pm','crepe','rmvpe')),
                         ('separator_backend',('hp2',)),('polish_profile',('gentle','balanced','strong'))):
        if key in value and value[key] not in allowed:
            warn('app.json 执行策略无效，已恢复默认：'+key);value.pop(key)
    for key,bounds in (('pitch_shift',(-24,24)),('index_rate',(0,1)),('bpm',(30,300)),
                       ('mix_vocal_gain_db',(-6,6)),('mix_instrumental_gain_db',(-6,6)),
                       ('quality_retry_limit',(0,1)),('rvc_input_gain_db',(-24,24))):
        if key in value and (not math.isfinite(value[key]) or not bounds[0]<=value[key]<=bounds[1]):
            warn('app.json 数值范围无效，已恢复默认：'+key);value.pop(key)
    validators={}
    from core.separator_routing import validate_separator_profile
    from core.midi_cleanup_options import normalize_midi_cleanup
    validators['separator_profile']=validate_separator_profile
    validators['midi_cleanup']=normalize_midi_cleanup
    for key,validator in validators.items():
        if key in value:
            try:value[key]=validator(value[key])
            except (TypeError,ValueError):
                warn('app.json 配置对象无效，已恢复默认：'+key);value.pop(key)
    if 'source_hint' in value:
        from core.source_policy import HINTS
        if value['source_hint'] not in HINTS:
            warn('app.json 来源提示无效，已恢复默认');value.pop('source_hint')
    if 'parameter_search_budget' in value and value['parameter_search_budget'] not in ('fast','balanced','quality'):
        warn('app.json 搜索预算无效，已恢复默认');value.pop('parameter_search_budget')
    if 'rvc_input_headroom_strategy' in value:
        from core.rvc_headroom import validate_rvc_headroom_strategy
        try:validate_rvc_headroom_strategy(value['rvc_input_headroom_strategy'])
        except ValueError:
            warn('app.json RVC 输入余量策略无效，已恢复默认');value.pop('rvc_input_headroom_strategy')
    if 'vocal_restoration_effect' in value and value['vocal_restoration_effect'] not in ('off','de_echo_normal','de_echo_aggressive','de_echo_dereverb','onnx_dereverb','denoise','bleed_suppression'):
        warn('app.json VocalRestore 效果无效，已恢复默认');value.pop('vocal_restoration_effect')
    if 'vocal_restore_profile' in value:
        from core.vocal_restore_profile import normalize_profile
        try:value['vocal_restore_profile']=normalize_profile(value['vocal_restore_profile'])
        except (TypeError,ValueError):
            warn('app.json VocalRestore profile 无效，已恢复 Auto');value.pop('vocal_restore_profile')
    if 'pitch_correction_mode' in value and value['pitch_correction_mode'] not in ('off','light','medium','strong'):
        warn('app.json PitchCorrection 模式无效，已恢复默认');value.pop('pitch_correction_mode')
    if 'completion_action' in value and value['completion_action'] not in ('none','exit','sleep','shutdown'):
        warn('app.json 完成动作无效，已恢复默认');value.pop('completion_action')
    if 'pitch_correction_key' in value:
        key = value['pitch_correction_key']
        if key is not None:
            if not isinstance(key, str):
                warn('app.json PitchCorrection 调性无效，已恢复自动');value.pop('pitch_correction_key')
            else:
                from core.pitch_correction import _scale
                try:value['pitch_correction_key'] = _scale(key)['key']
                except ValueError:
                    warn('app.json PitchCorrection 调性无效，已恢复自动');value.pop('pitch_correction_key')
    if 'cache_policy' in value:
        if strict:
            policy=value['cache_policy']
            if not isinstance(policy,dict):raise ValueError('缓存策略必须是对象')
            if 'enabled' in policy and type(policy['enabled']) is not bool:raise ValueError('缓存开关必须是布尔值')
            for key,minimum in (('quota_bytes',1),('ttl_days',0)):
                number=policy.get(key)
                if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or number<minimum):
                    raise ValueError('缓存策略数值无效：'+key)
        value['cache_policy']=_normalize_cache_policy(value['cache_policy'])
    return value


def load_settings():
    path = os.path.join(CONFIG_DIR, 'app.json')
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError('app.json 必须是 JSON 对象')
        return normalize_settings(value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _warn_config(f'app.json 已忽略（{exc}）：{path}')
        return {}

def save_settings(settings):
    path = os.path.join(CONFIG_DIR, 'app.json')
    if not isinstance(settings, dict):
        raise ValueError('设置必须是 JSON 对象')
    settings=dict(settings)
    if 'cache_policy' in settings:
        settings['cache_policy']=_normalize_cache_policy(settings['cache_policy'])
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.app-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

# 全局 GPU 互斥锁（严禁 UVR 与 RVC 同时抢占 6GB 显存）
from core.gpu import GPULock
GPU_LOCK = GPULock()

# 预设模型库扫描
def get_available_rvc_models():
    """扫描现有 RVC 音色模型与配套 index 库"""
    models = {}
    weights_dir = os.path.join(RVC_ROOT, "weights")
    logs_dir = os.path.join(RVC_ROOT, "logs")

    # 预置三款专属模型映射
    known_models = {
        "zhoujielun": {
            "name": "周杰伦 (Jay Chou)",
            "pth": os.path.join(weights_dir, "zhoujielun.pth"),
            "index": os.path.join(logs_dir, "zhoujielun", "added_IVF1803_Flat_nprobe_1_zhoujielun_v2.index"),
            "gender": "male",
            "default_pitch_male": 0,
            "default_pitch_female": -12
        },
        "sunyanzi": {
            "name": "孙燕姿 (Stefanie Sun)",
            "pth": os.path.join(weights_dir, "sunyanzi.pth"),
            "index": os.path.join(logs_dir, "sunyanzi", "added_IVF1249_Flat_nprobe_1_syz_v2.index"),
            "gender": "female",
            "default_pitch_male": 12,
            "default_pitch_female": 0
        },
        "zhaolusi": {
            "name": "赵露思 (Rosy Zhao)",
            "pth": os.path.join(weights_dir, "zhaolusi.pth"),
            "index": os.path.join(logs_dir, "zhaolusi", "added_IVF1362_Flat_nprobe_1_zls_1_v2.index"),
            "gender": "female",
            "default_pitch_male": 12,
            "default_pitch_female": 0
        }
    }

    for key, info in known_models.items():
        if os.path.exists(info["pth"]):
            models[key] = info

    # 动态扫描用户放入的额外模型
    if os.path.exists(weights_dir):
        for f in os.listdir(weights_dir):
            if f.endswith(".pth"):
                base = os.path.splitext(f)[0]
                if base not in models:
                    # 查找对应 index
                    idx_path = None
                    target_log = os.path.join(logs_dir, base)
                    if os.path.isdir(target_log):
                        for sub in os.listdir(target_log):
                            if sub.endswith(".index"):
                                idx_path = os.path.join(target_log, sub)
                                break
                    models[base] = {
                        "name": f"自定义: {base}",
                        "pth": os.path.join(weights_dir, f),
                        "index": idx_path,
                        "gender": "unknown",
                        "default_pitch_male": 0,
                        "default_pitch_female": 0
                    }

    custom_root = os.path.join(BASE_DIR, 'models', 'rvc')
    if os.path.isdir(custom_root):
        for name in sorted(os.listdir(custom_root)):
            if name.casefold() == 'imported':
                continue
            root = os.path.join(custom_root, name)
            if not os.path.isdir(root):
                continue
            weights = sorted(f for f in os.listdir(root) if f.endswith('.pth'))
            indices = sorted(f for f in os.listdir(root) if f.endswith('.index') and not f.startswith('trained_'))
            if weights:
                models[name] = dict(name=name, pth=os.path.join(root, weights[0]),
                                    index=os.path.join(root, indices[0]) if indices else None, gender='unknown')
        imported_root = os.path.join(custom_root, 'imported')
        if os.path.isdir(imported_root):
            for filename in sorted(os.listdir(imported_root)):
                if not filename.lower().endswith('.pth'):
                    continue
                pth = os.path.join(imported_root, filename)
                if not os.path.isfile(pth):
                    continue
                stem = os.path.splitext(filename)[0]
                key = 'imported_' + stem
                suffix = 2
                while key in models:
                    key = 'imported_%s_%d' % (stem, suffix)
                    suffix += 1
                index = os.path.join(imported_root, stem + '.index')
                models[key] = dict(
                    name='导入模型: ' + stem,
                    pth=pth,
                    index=index if os.path.isfile(index) else None,
                    gender='unknown',
                    user_imported=True,
                )
    for key, info in models.items():
        profile_path = os.path.join(CONFIG_DIR, 'model-profiles', key + '.json')
        local_profile = os.path.join(os.path.dirname(info['pth']), 'profile.json')
        profile = dict(MODEL_PROFILE_DEFAULTS)
        for path in (local_profile, profile_path):
            if os.path.isfile(path):
                try:
                    with open(path, encoding='utf-8-sig') as f:
                        loaded = json.load(f)
                    validated = _validate_model_profile(loaded, path)
                    # Keep the historical precedence of local_profile then
                    # config profile without allowing validator defaults from
                    # the second file to erase values from the first one.
                    profile.update({name: validated[name] for name in loaded if name in validated})
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    _warn_config(f'模型 Profile 已忽略（{key}: {exc}）：{path}')
        info['profile'] = profile
        info['profile_warnings'] = [w for w in CONFIG_WARNINGS if key in w]
        if info['index'] and not os.path.isfile(info['index']):
            info['index'] = None
        info['profile_path'] = profile_path
    return models

# 预设清理与母带方案
CLEAN_PRESETS = {
    "light": {
        "name": "轻度清理 (自然通透)",
        "filter": (
            "highpass=f=35:poles=2,"
            "equalizer=f=300:width_type=q:w=1.2:g=-1.0,"
            "equalizer=f=6500:width_type=q:w=1.5:g=-1.5,"
            "equalizer=f=11000:width_type=q:w=1.5:g=-1.0,"
            "highshelf=f=15000:g=-1.8:width_type=q:w=0.7,"
            "stereotools=slev=0.95,"
            "alimiter=limit=-1dB:level=false"
        )
    },
    "medium": {
        "name": "中度清理 (推荐默认)",
        "filter": (
            "highpass=f=35:poles=2,"
            "equalizer=f=300:width_type=q:w=1.2:g=-1.5,"
            "equalizer=f=6500:width_type=q:w=1.5:g=-2.5,"
            "equalizer=f=11000:width_type=q:w=1.5:g=-2.0,"
            "highshelf=f=15000:g=-2.5:width_type=q:w=0.7,"
            "stereotools=slev=0.90,"
            "alimiter=limit=-1dB:level=false"
        )
    },
    "heavy": {
        "name": "重度清理 (强消砂感与塑料音)",
        "filter": (
            "highpass=f=38:poles=2,"
            "equalizer=f=320:width_type=q:w=1.2:g=-2.0,"
            "equalizer=f=6500:width_type=q:w=1.5:g=-3.0,"
            "equalizer=f=11000:width_type=q:w=1.5:g=-2.5,"
            "highshelf=f=15000:g=-3.5:width_type=q:w=0.7,"
            "stereotools=slev=0.85,"
            "alimiter=limit=-1dB:level=false"
        )
    }
}

MASTER_PRESETS = {
    "natural": {
        "name": "默认自然 (-11 LUFS)",
        "target_lufs": -11.0,
        "true_peak": -1.0
    },
    "streaming": {
        "name": "流媒体合规 (-12 LUFS)",
        "target_lufs": -12.0,
        "true_peak": -1.0
    },
    "loud": {
        "name": "流行响亮 (-10 LUFS)",
        "target_lufs": -10.0,
        "true_peak": -1.0
    }
}
MASTER_PRESETS.update({
    'folk': {'name': '民谣 (-13 LUFS)', 'target_lufs': -13., 'true_peak': -1.},
    'ancient': {'name': '古风 (-12 LUFS)', 'target_lufs': -12., 'true_peak': -1.},
    'strong': {'name': '强响度 (-9 LUFS)', 'target_lufs': -9., 'true_peak': -1.}
})
