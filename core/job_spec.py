"""Frozen, versioned processing inputs. Never guess missing resume settings."""
import copy
import hashlib
import math
import os
import shutil
import uuid
from core.storage import canonical_json, CorruptJobError, sha256_file
from core.config import BASE_DIR, CLEAN_PRESETS, MASTER_PRESETS, get_available_rvc_models

REQUIRED = {'clean_preset','master_preset','rvc_model_key','pitch_shift','f0_method',
            'index_rate','generate_midi','output_formats','output_sample_rate','bpm'}


def validate_mix_gain_db(value, field='mix gain'):
    """Validate a user supplied stem gain without accepting bool/NaN values."""
    if type(value) not in (int, float):
        raise ValueError('%s 必须是 -6 到 6 dB 范围内的有限数值' % field)
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if not finite or not -6.0 <= value <= 6.0:
        raise ValueError('%s 必须是 -6 到 6 dB 范围内的有限数值' % field)
    return value


def validate_transcribe_stem(value):
    """Validate the real separated stem used by the MIDI transcriber."""
    if type(value) is not str or value not in ('vocal', 'bass', 'other'):
        raise ValueError('transcribe_stem 必须是 vocal/bass/other')
    return value


def validate_audio_options(parameters):
    if 'input_channel_mode' in parameters and (type(parameters['input_channel_mode']) is not str or
            parameters['input_channel_mode'] not in ('auto','mono','stereo')):
        raise ValueError('输入声道模式必须为 auto/mono/stereo')
    if 'output_channels' in parameters and (type(parameters['output_channels']) is not int or parameters['output_channels'] not in (1,2)):
        raise ValueError('输出声道数只能为整数 1/2')
    if 'detailed_analysis' in parameters and type(parameters['detailed_analysis']) is not bool:
        raise ValueError('详细分析开关必须为布尔值')


def freeze(parameters, mode, artifact_root=None):
    spec = copy.deepcopy(parameters)
    spec['schema_version'] = 1
    spec.setdefault('input_channel_mode','auto')
    spec.setdefault('output_channels',2)
    spec.setdefault('detailed_analysis',True)
    validate_audio_options(spec)
    if mode == 'voice_conversion':
        # These are frozen only for newly-created VC jobs.  validate() accepts
        # their absence so historical snapshots keep their original hash.
        spec.setdefault('mix_vocal_gain_db', 0.5)
        spec.setdefault('mix_instrumental_gain_db', -1.0)
    if mode in ('voice_conversion', 'transcribe'):
        # VC jobs may generate MIDI; transcribe jobs always consume this stem.
        spec.setdefault('transcribe_stem', 'vocal')
    from core.execution_policy import freeze_execution_policy
    spec['execution_policy'] = freeze_execution_policy(spec.pop('device', 'auto'), spec.pop('performance_profile', 'balanced'))
    spec['effective_clean'] = copy.deepcopy(CLEAN_PRESETS[spec['clean_preset']])
    spec['effective_master'] = copy.deepcopy(MASTER_PRESETS[spec['master_preset']])
    from core.source_policy import freeze_source_policy
    spec.setdefault('source_hint', 'auto')
    spec.setdefault('source_preclean_enabled', False)
    spec['effective_source_policy'] = freeze_source_policy(spec['source_hint'], spec['source_preclean_enabled'])
    if mode == 'transcribe' or (mode == 'voice_conversion' and spec.get('generate_midi') is True):
        from core.midi_cleanup_options import normalize_midi_cleanup
        spec['midi_cleanup'] = normalize_midi_cleanup(spec.get('midi_cleanup'))
    if mode == 'voice_conversion' and 'rvc_input_headroom_strategy' not in spec:
        # A new job measures its own cleaned vocal at execution time. Older
        # snapshots without this field keep their historical fixed gain.
        # An explicitly supplied gain is an existing fixed-parameter request,
        # including a deliberate 0 dB pass-through.
        spec['rvc_input_headroom_strategy'] = (
            'fixed' if 'rvc_input_gain_db' in spec else 'auto_peak')
    if mode == 'voice_conversion' and 'skip_vocal_eq' not in spec:
        spec['skip_vocal_eq'] = False
    if mode == 'voice_conversion' and 'rvc_postprocess' not in spec:
        spec['rvc_postprocess'] = False
    if mode == 'voice_conversion':
        spec.setdefault('rvc_postprocess_profile', 'off')
        spec.setdefault('adaptive_mix', False)
        spec.setdefault('mix_bus_profile', 'off')
    if mode == 'voice_conversion' and 'skip_mix_compressor' not in spec:
        spec['skip_mix_compressor'] = False
    if mode == 'voice_conversion' and 'mix_ducking' not in spec:
        spec['mix_ducking'] = False
    if mode == 'voice_conversion':
        # VCQ-006 changes new jobs. Frozen pre-VCQ-006 snapshots without this
        # field keep the historical explicit echo when they need a rerun.
        spec.setdefault('mix_reverb', False)
    if mode == 'voice_conversion' and 'quality_retry_limit' not in spec:
        spec['quality_retry_limit'] = 0
    if mode == 'voice_conversion' and 'quality_assist' not in spec:
        spec['quality_assist'] = False
    if mode == 'voice_conversion' and 'parameter_search_enabled' not in spec:
        spec['parameter_search_enabled'] = False
    if mode == 'voice_conversion' and 'parameter_search_budget' not in spec:
        spec['parameter_search_budget'] = 'fast'
    if mode == 'voice_conversion' and 'vocal_restoration_effect' not in spec:
        spec['vocal_restoration_effect'] = None
    if mode == 'voice_conversion':
        from core.vocal_restore_profile import normalize_profile
        spec['vocal_restore_profile'] = normalize_profile(spec.get('vocal_restore_profile'))
    if mode == 'voice_conversion':
        # Pitch correction is an opt-in pre-RVC stage. Missing fields in older
        # frozen jobs remain absent and therefore resolve to the historical off path.
        spec.setdefault('pitch_correction_mode', 'off')
        spec.setdefault('pitch_correction_key', None)
        if spec['pitch_correction_key'] is not None:
            from core.pitch_correction import _scale
            spec['pitch_correction_key'] = _scale(spec['pitch_correction_key'])['key']
    if mode == 'voice_conversion' and spec.get('vocal_restoration_effect'):
        from core.vocal_restoration import freeze_effect
        spec['effective_vocal_restoration'] = freeze_effect(spec['vocal_restoration_effect'])
    if mode == 'voice_conversion' and 'separator_profile' not in spec:
        from core.separator_routing import dual_profile, hp2_profile
        # New jobs use the explicitly requested route. Existing frozen jobs
        # without this field remain HP2 in validate()/pipeline recovery.
        spec['separator_profile'] = hp2_profile() if 'separator_backend' in spec else dual_profile()
    if mode == 'voice_conversion' and 'separator_backend' not in spec:
        spec['separator_backend'] = 'hp2'
    if mode == 'voice_conversion' and 'separator_profile' in spec:
        from core.separator_routing import validate_separator_profile, dual_model_identities
        spec['separator_profile'] = validate_separator_profile(spec['separator_profile'])
        if spec['separator_profile']['mode'] == 'dual':
            spec['effective_separator'] = dual_model_identities()
    if mode == 'voice_conversion':
        from core.f0_method import resolve_f0_method, weight_path
        spec['effective_f0'] = resolve_f0_method(spec['f0_method'])
        if spec['effective_f0']['executed'] == 'rmvpe':
            f0_path = os.path.abspath(weight_path())
            spec['effective_f0']['weight_path'] = f0_path
            spec['effective_f0']['weight_sha256'] = sha256_file(f0_path)
        model = copy.deepcopy(get_available_rvc_models()[spec['rvc_model_key']])
        model['output_precision'] = 'float32'
        model['artifact_hashes'] = {key: sha256_file(model[key]) if model.get(key) else None for key in ('pth','index')}
        if artifact_root:
            os.makedirs(artifact_root,exist_ok=True)
            for key, expected in model['artifact_hashes'].items():
                if not expected: continue
                original = model[key]
                target = os.path.abspath(os.path.join(artifact_root,expected+'.'+key))
                if not os.path.isfile(target) or sha256_file(target) != expected:
                    temporary = target+'.'+uuid.uuid4().hex+'.tmp'
                    try:
                        shutil.copyfile(original,temporary)
                        if sha256_file(temporary) != expected:
                            raise CorruptJobError('复制期间模型变化，请重新创建任务：'+key)
                        os.replace(temporary,target)
                    finally:
                        if os.path.exists(temporary): os.unlink(temporary)
                model['original_'+key] = original
                model[key] = target
        spec['effective_rvc'] = model
    if mode == 'polish':
        from core.polish_policy import freeze_polish_fields
        freeze_polish_fields(spec)
    from core.component_identity import capture_component_identity
    spec['component_identity'] = capture_component_identity(mode, BASE_DIR,
        separator_profile=spec.get('separator_profile') if mode == 'voice_conversion' else None,
        f0_method=(spec.get('effective_f0') or {}).get('executed'))
    spec['spec_hash'] = digest(spec)
    return validate(spec,mode)


def digest(spec):
    return hashlib.sha256(canonical_json({k:v for k,v in spec.items() if k != 'spec_hash'}).encode()).hexdigest()


def resolve_mix_reverb(spec, previous_stage_parameters=None):
    """Resolve an already frozen VC job, preserving the pre-VCQ-006 default."""
    if 'mix_reverb' in spec:
        value = spec['mix_reverb']
    elif isinstance(previous_stage_parameters, dict) and 'reverb' in previous_stage_parameters:
        # Some intermediate versions changed the default before freezing it.
        # A prior actual Mix stage is stronger evidence than an inferred default.
        value = previous_stage_parameters['reverb']
    else:
        value = True
    if type(value) is not bool:
        raise CorruptJobError('Mix reverb 开关必须是布尔值')
    return value


def validate(spec, mode):
    if not isinstance(spec, dict) or not REQUIRED.issubset(spec):
        raise CorruptJobError('任务参数快照缺少必需字段，拒绝使用默认值恢复')
    from core.bounded_voice_dsp import validate_profile
    try:
        validate_audio_options(spec)
        for key in ('rvc_postprocess_profile', 'mix_bus_profile'):
            if key in spec:
                validate_profile(spec[key])
        if 'adaptive_mix' in spec and type(spec['adaptive_mix']) is not bool:
            raise ValueError('adaptive_mix 必须是布尔值')
    except ValueError as exc:
        raise CorruptJobError(str(exc)) from exc
    if spec.get('schema_version') != 1 or spec.get('spec_hash') != digest(spec):
        raise CorruptJobError('旧版或损坏的任务快照无法安全恢复；请保留旧项目，使用原参数创建新任务')
    if 'execution_policy' in spec:
        from core.execution_policy import validate_execution_policy
        try:
            validate_execution_policy(spec['execution_policy'])
        except ValueError as exc:
            raise CorruptJobError(str(exc)) from exc
    if 'source_hint' in spec or 'source_preclean_enabled' in spec or 'effective_source_policy' in spec:
        from core.source_policy import HINTS, freeze_source_policy
        hint = spec.get('source_hint', 'auto')
        enabled = spec.get('source_preclean_enabled', False)
        if hint not in HINTS or type(enabled) is not bool:
            raise CorruptJobError('SourcePolicy 快照字段无效')
        expected_policy = freeze_source_policy(hint, enabled)
        if spec.get('effective_source_policy') != expected_policy:
            raise CorruptJobError('SourcePolicy 快照缺少或修改了冻结策略')
    if 'midi_cleanup' in spec:
        from core.midi_cleanup_options import normalize_midi_cleanup
        try:
            midi_cleanup = normalize_midi_cleanup(spec['midi_cleanup'])
        except ValueError as exc:
            raise CorruptJobError(str(exc)) from exc
        if midi_cleanup != spec['midi_cleanup']:
            raise CorruptJobError('MIDI cleanup 快照不是标准化参数')
        if midi_cleanup['enabled'] and not (mode == 'transcribe' or
                                            (mode == 'voice_conversion' and spec.get('generate_midi') is True)):
            raise CorruptJobError('未生成 MIDI 的任务不能启用 MIDI cleanup')
    types = {'clean_preset':str,'master_preset':str,'rvc_model_key':str,'pitch_shift':int,
             'f0_method':str,'generate_midi':bool,'output_formats':list,'output_sample_rate':int}
    for key, kind in types.items():
        if type(spec[key]) is not kind:
            raise CorruptJobError('任务快照字段类型错误：'+key)
    for key in ('index_rate','bpm'):
        if type(spec[key]) not in (int,float) or not math.isfinite(spec[key]):
            raise CorruptJobError('任务快照数值无效：'+key)
    for key in ('mix_vocal_gain_db', 'mix_instrumental_gain_db'):
        if key in spec:
            try:
                validate_mix_gain_db(spec[key], key)
            except ValueError as exc:
                raise CorruptJobError(str(exc)) from exc
    if 'transcribe_stem' in spec:
        try:
            validate_transcribe_stem(spec['transcribe_stem'])
        except ValueError as exc:
            raise CorruptJobError(str(exc)) from exc
    if 'rvc_input_gain_db' in spec:
        from core.rvc_headroom import validate_rvc_input_gain_db
        try:
            validate_rvc_input_gain_db(spec['rvc_input_gain_db'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'rvc_input_headroom_strategy' in spec:
        from core.rvc_headroom import validate_rvc_headroom_strategy
        try:
            validate_rvc_headroom_strategy(spec['rvc_input_headroom_strategy'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'parameter_search_enabled' in spec and type(spec['parameter_search_enabled']) is not bool:
        raise CorruptJobError('parameter_search_enabled 只能是布尔值')
    if 'parameter_search_budget' in spec:
        from core.budgeted_search import budget_limit
        try:
            budget_limit(spec['parameter_search_budget'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'vocal_restoration_effect' in spec:
        effect = spec['vocal_restoration_effect']
        identity = spec.get('effective_vocal_restoration')
        if effect is None:
            if identity is not None:
                raise CorruptJobError('未选择 VocalRestore 时不能冻结效果权重')
        else:
            from core.vocal_restoration import freeze_effect
            if not isinstance(identity, dict) or identity.get('effect_id') != effect:
                raise CorruptJobError('VocalRestore 快照缺少冻结权重身份')
            try:
                weight_root = identity.get('weight_dir') or os.path.dirname(identity.get('path') or '')
                current = freeze_effect(effect, weight_root or None)
            except (OSError, ValueError) as exc:
                raise CorruptJobError('VocalRestore 权重无法验证：' + str(exc)) from exc
            from core.vocal_restoration import _identity_matches
            if not _identity_matches(identity, current):
                raise CorruptJobError('VocalRestore 权重已变化，拒绝恢复')
    if 'vocal_restore_profile' in spec:
        from core.vocal_restore_profile import normalize_profile
        try:
            profile = normalize_profile(spec['vocal_restore_profile'])
        except (TypeError, ValueError) as exc:
            raise CorruptJobError('VocalRestore profile 无效：' + str(exc)) from exc
        if profile != spec['vocal_restore_profile']:
            raise CorruptJobError('VocalRestore profile 不是标准化的冻结参数')
    if 'pitch_correction_mode' in spec or 'pitch_correction_key' in spec:
        correction_mode = spec.get('pitch_correction_mode', 'off')
        correction_key = spec.get('pitch_correction_key')
        if correction_mode not in ('off', 'light', 'medium', 'strong'):
            raise CorruptJobError('PitchCorrection 模式无效')
        if correction_key is not None:
            from core.pitch_correction import _scale
            try:
                _scale(correction_key)
            except (TypeError, ValueError) as exc:
                raise CorruptJobError('PitchCorrection 调性无效') from exc
    if 'skip_vocal_eq' in spec:
        from core.vocal_eq import validate_skip_vocal_eq
        try:
            validate_skip_vocal_eq(spec['skip_vocal_eq'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'rvc_postprocess' in spec:
        from core.rvc_postprocess import validate_rvc_postprocess
        try:
            validate_rvc_postprocess(spec['rvc_postprocess'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'skip_mix_compressor' in spec:
        from core.mix_compressor import validate_skip_mix_compressor
        try:
            validate_skip_mix_compressor(spec['skip_mix_compressor'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'mix_ducking' in spec and type(spec['mix_ducking']) is not bool:
        raise CorruptJobError('Mix Ducking 开关必须是布尔值')
    if 'mix_reverb' in spec:
        resolve_mix_reverb(spec)
    if 'quality_retry_limit' in spec:
        from core.quality_retry import validate_quality_retry_limit
        try:
            validate_quality_retry_limit(spec['quality_retry_limit'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'quality_assist' in spec:
        from core.voice_quality_flow import validate_quality_assist
        try:
            validate_quality_assist(spec['quality_assist'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'separator_backend' in spec:
        from core.separator_choice import validate_separator_backend
        try:
            validate_separator_backend(spec['separator_backend'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if 'separator_profile' in spec:
        from core.separator_routing import validate_separator_profile
        try:
            validate_separator_profile(spec['separator_profile'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
        if spec['separator_profile']['mode'] == 'dual':
            artifacts = spec.get('effective_separator')
            if not isinstance(artifacts, dict) or set(artifacts) != {'vocal', 'instrumental'}:
                raise CorruptJobError('旧双模型任务未冻结权重和 metadata，请保留项目并创建新任务')
            from core.mdx_separator import verify_model_identity
            for side, artifact in artifacts.items():
                if not isinstance(artifact, dict) or not artifact.get('path') or not artifact.get('metadata_sha256'):
                    raise CorruptJobError('双模型快照缺少文件或 metadata 身份：'+side)
                try:
                    verify_model_identity(artifact['path'], artifact, metadata_path=artifact.get('metadata_path'))
                except (OSError, ValueError, RuntimeError) as exc:
                    raise CorruptJobError('双模型权重或 metadata 已变化，拒绝恢复：'+str(exc)) from exc
    if 'polish' in spec:
        from core.polish_policy import clamp_policy
        try:
            clamped = clamp_policy(spec['polish'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
        if canonical_json(clamped) != canonical_json(spec['polish']):
            raise CorruptJobError('精修参数超出安全上限或无效')
    if 'component_identity' in spec:
        from core.component_identity import validate_component_identity
        try:
            validate_component_identity(spec['component_identity'])
        except ValueError as exc:
            raise CorruptJobError(str(exc))
    if not isinstance(spec.get('effective_clean'),dict) or not isinstance(spec['effective_clean'].get('filter'),str):
        raise CorruptJobError('缺少冻结的清理参数')
    master = spec.get('effective_master', {})
    if not all(type(master.get(k)) in (int,float) and math.isfinite(master[k]) for k in ('target_lufs','true_peak')):
        raise CorruptJobError('缺少冻结的母带参数')
    if mode == 'voice_conversion':
        f0 = spec.get('effective_f0')
        if f0 is not None:
            if not isinstance(f0, dict) or f0.get('requested') != spec['f0_method'] or f0.get('executed') not in ('harvest', 'pm', 'crepe', 'rmvpe'):
                raise CorruptJobError('冻结 F0 决策无效')
            if f0['executed'] == 'rmvpe':
                path = f0.get('weight_path')
                if not path or not os.path.isfile(path) or not f0.get('weight_sha256') or sha256_file(path) != f0['weight_sha256']:
                    raise CorruptJobError('冻结 RMVPE 权重已变化，不能恢复旧任务')
        model = spec.get('effective_rvc', {})
        if model.get('output_precision', 'legacy_int16') not in ('float32', 'legacy_int16'):
            raise CorruptJobError('冻结 RVC 输出精度无效')
        profile = model.get('profile', {})
        if not all(k in profile for k in ('protect','filter_radius','rms_mix_rate')):
            raise CorruptJobError('缺少冻结的模型Profile')
        for key,low,high in [('protect',0,.5),('filter_radius',0,7),('rms_mix_rate',0,1)]:
            value = profile[key]
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise CorruptJobError('冻结Profile数值无效：'+key)
        if type(profile['filter_radius']) is not int:
            raise CorruptJobError('filter_radius 必须是整数')
        if set(model.get('artifact_hashes',{})) != {'pth','index'} or not model['artifact_hashes']['pth']:
            raise CorruptJobError('冻结模型缺少权重hash')
        for key, expected in model.get('artifact_hashes', {}).items():
            if expected and (not model.get(key) or sha256_file(model[key]) != expected):
                raise CorruptJobError('模型文件已变化，不能用旧任务身份恢复：'+key)
    return spec
