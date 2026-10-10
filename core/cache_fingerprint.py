"""Stage-specific fingerprints of implementations and actual inference artifacts."""
import os
import importlib.metadata
import threading
from pathlib import Path
from core.config import BASE_DIR, RVC_ROOT, RVC_PYTHON, ADDONS_DIR, UVR_ROOT, FFMPEG_EXE, FFPROBE_EXE
from core.storage import sha256_file

STAGE_MODULES = {
    'SourceClean':['source_policy'],
    'Ingest': ['decryptor','analyzer'], 'Analyze':['analyzer','polish_analysis'], 'Clean':['cleaner'],
    'Repair': ['polish_chain','polish_dsp','polish_analysis','polish_policy'],
    'Tone': ['polish_chain','polish_dsp','polish_analysis','polish_policy'],
    'Dynamics': ['polish_chain','polish_dsp','polish_analysis','polish_policy'],
    'Stereo': ['polish_chain','polish_dsp','polish_analysis','polish_policy'],
    'Separate':['separator','uvr_worker','uvr_band_safety','uvr_trusted_weights','creative_worker','worker_runtime'],
    'RVC':['rvc_engine','rvc_worker','safe_checkpoint','safe_faiss_index','worker_runtime'], 'Align':['aligner'],
    'RVCSearch':['pipeline','rvc_engine','rvc_worker','safe_checkpoint','safe_faiss_index','worker_runtime','search_candidate_worker',
                 'parameter_search','budgeted_search','artifact_score','representative_clips'],
                'VocalRestore':['separator','uvr_worker','uvr_band_safety','uvr_trusted_weights','worker_runtime','vocal_restoration','restoration_worker',
                                'vocal_restore_profile'],
    'Mix':['mixer'], 'Ducking':['mix_ducking'], 'Master':['mastering'], 'QC':['qc','analyzer','export_repair'],
    'PitchCorrection':['pitch_correction'], 'MIDICleanup':['midi_cleanup','midi_cleanup_options'],
    'Transcribe':['transcriber','creative_worker','worker_runtime'],
}
_HASHES = {}
_HASH_LOCK = threading.Lock()


def _hash(path):
    info = path.stat()
    identity = (str(path.resolve()),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
    with _HASH_LOCK:
        value = _HASHES.get(identity)
    if value is not None:return value
    value = sha256_file(path)
    after = path.stat()
    if (info.st_size,info.st_mtime_ns,info.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise RuntimeError('计算指纹期间依赖文件变化：'+str(path))
    with _HASH_LOCK:_HASHES[identity] = value
    return value


def vocal_eq_fingerprint_paths(root, parameters):
    """Hash the switch only when a job actually skips the extra vocal EQ."""
    if isinstance(parameters, dict) and parameters.get('skip_vocal_eq') is True:
        return [root/'core'/'vocal_eq.py']
    return []


def mix_compressor_fingerprint_paths(root, parameters):
    """Hash the switch only when a job actually removes the vocal compressor."""
    if isinstance(parameters, dict) and parameters.get('skip_mix_compressor') is True:
        return [root/'core'/'mix_compressor.py']
    return []


def rvc_headroom_fingerprint_paths(root, parameters):
    """Hash the policy whenever it can choose this song's RVC input gain."""
    if isinstance(parameters, dict) and parameters.get('rvc_input_headroom_strategy') in ('auto_peak', 'fixed'):
        return [root/'core'/'rvc_headroom.py']
    value = parameters.get('rvc_input_gain_db', 0) if isinstance(parameters, dict) else 0
    if type(value) in (int, float) and value == value and value != 0:
        return [root/'core'/'rvc_headroom.py']
    return []


def f0_method_fingerprint_paths(root, parameters):
    """Hash the resolver only when this stage recorded an RMVPE request."""
    if isinstance(parameters, dict) and (parameters.get('f0_requested') == 'rmvpe' or parameters.get('f0_method') == 'rmvpe'):
        return [root/'core'/'f0_method.py', root/'core'/'rmvpe_adapter.py',
                root/'core'/'vendor'/'rmvpe.py', root/'core'/'vendor'/'rmvpe_eager.py',
                Path((parameters.get('effective_f0') or {}).get('weight_path'))
                if (parameters.get('effective_f0') or {}).get('weight_path')
                else root/'tools'/'RVC'/'RVC-beta-v2-0618'/'rmvpe.pt']
    return []


def f0_quality_fingerprint_paths(root, parameters):
    """Hash the gate only for a voice-conversion QC report."""
    if isinstance(parameters, dict) and parameters.get('mode') == 'voice_conversion':
        return [root/'core'/'f0_quality.py']
    return []


def artifact_score_fingerprint_paths(root, parameters):
    """Hash the heuristic only for a voice-conversion QC report."""
    if isinstance(parameters, dict) and parameters.get('mode') == 'voice_conversion':
        return [root/'core'/'artifact_score.py']
    return []


def voice_quality_fingerprint_paths(root, parameters):
    """Hash the plan when a voice QC report or an opted-in retry can change."""
    if not isinstance(parameters, dict):
        return []
    if parameters.get('quality_assist') is True or parameters.get('mode') == 'voice_conversion':
        return [root/'core'/name for name in ('voice_quality_flow.py', 'quality_retry.py',
                'parameter_search.py', 'pitch_suggestion.py', 'representative_clips.py')]
    return []


def stage_quality_trace_fingerprint_paths(root, parameters):
    """Hash the trace only for a voice-conversion QC report."""
    if isinstance(parameters, dict) and parameters.get('mode') == 'voice_conversion':
        return [root/'core'/'stage_quality_trace.py']
    return []


def separator_choice_fingerprint_paths(root, parameters):
    """Hash the selector only when the stage is not the HP2 default."""
    if not isinstance(parameters, dict):
        return []
    paths = []
    if parameters.get('separator_backend') not in (None, 'hp2'):
        paths.append(root/'core'/'separator_choice.py')
    profile = parameters.get('separator_profile')
    if isinstance(profile, dict) and profile.get('mode') == 'dual':
        paths.append(root/'core'/'separator_routing.py')
    return paths


def polish_reference_fingerprint_paths(parameters):
    """The reference file belongs to Tone only. Repair must not see it."""
    if not isinstance(parameters, dict):
        return []
    reference = parameters.get('reference')
    if isinstance(reference, dict) and reference.get('path'):
        return [Path(reference['path'])]
    return []


def stage_component_ids(stage, parameters):
    """Components whose catalog declaration belongs to this stage only."""
    if stage in ('RVC', 'RVCSearch'):
        identities = ('engine.rvc', 'model.hubert')
        if isinstance(parameters, dict) and parameters.get('f0_method') == 'rmvpe':
            identities += ('engine.f0.rmvpe',)
        return identities
    if stage == 'Transcribe':
        return ('engine.basic_pitch',)
    if stage == 'Separate' and isinstance(parameters, dict):
        mode = parameters.get('mode')
        if mode == 'voice_conversion':
            profile = parameters.get('separator_profile')
            if isinstance(profile, dict) and profile.get('mode') == 'dual':
                vocal = (profile.get('vocal') or {}).get('component_id')
                instrumental = (profile.get('instrumental') or {}).get('component_id')
                if not vocal or not instrumental:
                    raise ValueError('双模型指纹缺少组件身份')
                return (vocal, instrumental)
            return ('engine.separator.hp2',)
        if mode == 'transcribe':
            return ('engine.separator.htdemucs',)
    return ()


def component_declaration_entries(stage, parameters):
    """Map component:<id> to the catalog entry hash. Other stages get nothing."""
    ids = stage_component_ids(stage, parameters)
    if not ids:
        return {}
    from core.component_identity import declaration_sha256
    from core.component_platform import catalog_path, load_catalog
    catalog = load_catalog(catalog_path(BASE_DIR))
    by_id = {item['id']: item for item in catalog['components']}
    entries = {}
    for component_id in ids:
        component = by_id.get(component_id)
        if component is None:
            raise ValueError('阶段指纹缺少组件：' + component_id)
        entries['component:' + component_id] = declaration_sha256(component)
    return entries


def fingerprints(stage, parameters):
    root = Path(BASE_DIR)
    paths = [root/'core'/ (name+'.py') for name in STAGE_MODULES.get(stage, [])]
    paths += [root/'core'/name for name in ('pipeline_support.py','stage_operations.py')]
    if stage == 'Ingest':paths.append(root/'core'/'resource_limits.py')
    paths += [Path(FFMPEG_EXE),Path(FFPROBE_EXE)]
    if stage == 'Clean':
        paths += vocal_eq_fingerprint_paths(root, parameters)
    if stage == 'Mix':
        paths += mix_compressor_fingerprint_paths(root, parameters)
        paths.append(root/'core'/'bounded_voice_dsp.py')
    if isinstance(parameters,dict) and 'execution_policy' in parameters:
        paths.append(root/'core'/'execution_policy.py')
    if stage == 'RVCPost':
        paths.append(root/'core'/'rvc_postprocess.py')
        if isinstance(parameters,dict) and parameters.get('rvc_postprocess_profile')=='gentle':
            paths.append(root/'core'/'bounded_voice_dsp.py')
        paths += voice_quality_fingerprint_paths(root, parameters)
    if stage == 'QC':
        paths += f0_quality_fingerprint_paths(root, parameters)
        paths += artifact_score_fingerprint_paths(root, parameters)
        paths += voice_quality_fingerprint_paths(root, parameters)
        paths += stage_quality_trace_fingerprint_paths(root, parameters)
    if stage == 'Separate':
        paths += separator_choice_fingerprint_paths(root, parameters)
    if stage == 'Tone':
        paths += polish_reference_fingerprint_paths(parameters)
    if stage == 'QC' and isinstance(parameters, dict) and parameters.get('mode') == 'polish':
        paths.append(root/'core'/'polish_qc.py')
    declarations = component_declaration_entries(stage, parameters)
    if declarations:
        paths.append(root/'core'/'component_identity.py')
    if stage == 'RVC':
        model = parameters.get('effective_rvc', {})
        paths += [Path(model[k]) for k in ('pth','index') if model.get(k)]
        vendor = Path(RVC_ROOT)
        paths += [vendor/'hubert_base.pt',vendor/'vc_infer_pipeline.py',vendor/'my_utils.py']
        for name in ('infer_pack','lib'):
            directory = vendor/name
            if directory.exists(): paths += sorted(directory.rglob('*.py'))
        paths += rvc_headroom_fingerprint_paths(root, parameters)
        paths += f0_method_fingerprint_paths(root, parameters)
    elif stage == 'VocalRestore':
        identity = parameters.get('effective_vocal_restoration') or parameters
        path = identity.get('weight_path') or identity.get('path')
        if path:
            paths.append(Path(path))
        if identity.get('backend') == 'mdxnet' or identity.get('effect_id') == 'onnx_dereverb':
            paths.append(Path(RVC_ROOT)/'MDXNet.py')
    elif stage == 'Separate':
        if parameters.get('mode') == 'voice_conversion':
            vendor = Path(RVC_ROOT)
            profile = parameters.get('separator_profile') if isinstance(parameters, dict) else None
            if isinstance(profile, dict) and profile.get('mode') == 'dual':
                mdx_root = Path(UVR_ROOT)/'models'/'MDX_Net_Models'
                paths += [root/'core'/'mdx_separator.py', root/'core'/'mdx_worker.py',
                          mdx_root/'model_data'/'model_data.json']
                allowed = {'Kim_Vocal_2.onnx', 'UVR-MDX-NET-Inst_HQ_3.onnx'}
                for side in ('vocal', 'instrumental'):
                    name = (profile.get(side) or {}).get('model_file')
                    if name in allowed:
                        artifact = (parameters.get('effective_separator') or {}).get(side) or {}
                        paths.append(Path(artifact['path']) if artifact.get('path') else mdx_root/name)
            else:
                paths += [vendor/'uvr5_weights'/'HP2_all_vocals.pth',vendor/'infer_uvr5.py']
                paths += sorted((vendor/'uvr5_pack').rglob('*.py'))
                paths += [vendor/'uvr5_pack'/'name_params.json',
                          vendor/'uvr5_pack'/'lib_v5'/'modelparams'/'4band_v2.json']
        else:
            repo = Path(UVR_ROOT)/'models'/'Demucs_Models'/'v3_v4_repo'
            # htdemucs bag references this one checksum-named checkpoint.
            paths += [repo/'htdemucs.yaml']
            paths += sorted(repo.glob('955717e8*.th'))
            paths += sorted((Path(ADDONS_DIR)/'demucs').rglob('*.py'))
    elif stage == 'Transcribe':
        paths += [Path(ADDONS_DIR)/'basic_pitch'/'saved_models'/'icassp_2022'/'nmp.onnx']
        for package in ('basic_pitch','pretty_midi'):
            paths += sorted((Path(ADDONS_DIR)/package).rglob('*.py'))
    result = {}
    for path in paths:
        if path.is_file(): result[str(path.resolve())] = _hash(path)
    result.update(declarations)
    return dict(sorted(result.items()))


def enrich(stage, parameters):
    packages = {'RVC':['torch','fairseq','faiss-cpu','pyworld','librosa','scipy','soundfile'],
                'RVCSearch':['torch','fairseq','faiss-cpu','pyworld','librosa','scipy','soundfile'],
                'VocalRestore':['torch','librosa','scipy','soundfile','onnxruntime','numpy','pyworld'],
                'Separate':['torch','demucs','numpy','librosa','onnxruntime','scipy','soundfile'],
                'Transcribe':['basic-pitch','onnxruntime','pretty-midi','librosa'],
                'Align':['numpy','scipy','soundfile'], 'QC':['numpy','soundfile']}
    packages['PitchCorrection'] = ['numpy', 'soundfile', 'pyworld', 'scipy', 'librosa']
    versions = {}
    runtime_paths = [str(Path(RVC_PYTHON).parent/'Lib'/'site-packages')]
    if stage in ('Separate','Transcribe'):runtime_paths.insert(0,str(ADDONS_DIR))
    distributions = list(importlib.metadata.distributions(path=runtime_paths))
    dependencies = fingerprints(stage,parameters)
    if stage in ('RVC','RVCSearch','VocalRestore','Separate','Transcribe') and Path(RVC_PYTHON).is_file():
        dependencies[str(Path(RVC_PYTHON).resolve())] = _hash(Path(RVC_PYTHON))
    for package in packages.get(stage,[]):
        normalized = package.lower().replace('-','_')
        found = next((d for d in distributions if (d.metadata.get('Name') or '').lower().replace('-','_') == normalized),None)
        if found:
            versions[package] = found.version
            for entry in found.files or []:
                if str(entry).endswith(('dist-info/RECORD','dist-info/METADATA')):
                    path = Path(found.locate_file(entry))
                    if path.is_file():dependencies[str(path.resolve())] = _hash(path)
        else:
            try:versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:versions[package] = 'not-in-runtime-metadata'
    return dict(parameters, dependency_hashes=dependencies, package_versions=versions, stage_contract_version=2)
