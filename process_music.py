"""Command line entry for reproducible local jobs and JSON result output."""
import os
import sys
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

_ENTRY_ROOT = os.path.dirname(os.path.abspath(__file__))
_PORTABLE_RELEASE = (os.path.lexists(os.path.join(_ENTRY_ROOT, 'PORTABLE_RELEASE')) or
                     os.path.lexists(os.path.join(_ENTRY_ROOT, 'release-manifest.json')))
if _PORTABLE_RELEASE:
    sys.path.insert(0, _ENTRY_ROOT)
    from core.paths import release_numba_cache_dir
    os.environ['NUMBA_CACHE_DIR'] = str(release_numba_cache_dir())

import argparse
import json
sys.path.insert(0, _ENTRY_ROOT)


def main(argv=None):
    root=_ENTRY_ROOT
    arguments=list(sys.argv[1:] if argv is None else argv)
    # This first parser is deliberately limited to the verify-only switch.
    # Release verification must run before config/pipeline imports can create
    # paths, open databases or start any work.
    verify_parser=argparse.ArgumentParser(add_help=False)
    verify_parser.add_argument('--verify-runtime',action='store_true')
    verify_args,_=verify_parser.parse_known_args(arguments)
    from core.paths import is_release_mode
    release_mode=is_release_mode(root)
    runtime_result=None
    if verify_args.verify_runtime or release_mode:
        from core.runtime_verify import verify_runtime
        runtime_result=verify_runtime(root=root,timeout=900,fast=not verify_args.verify_runtime)
        if verify_args.verify_runtime or runtime_result.get('status')=='FAIL':
            print(json.dumps(runtime_result,ensure_ascii=False,indent=2))
            if runtime_result.get('status')=='FAIL':raise SystemExit(1)
            if verify_args.verify_runtime:raise SystemExit(0)

    # Import application modules only after the release health gate succeeds.
    from core.pipeline import AudioPipeline
    from core.processes import Control, ProcessCancelled
    from core.config import CLEAN_PRESETS, MASTER_PRESETS
    parser=argparse.ArgumentParser()
    parser.add_argument('files',nargs='*')
    parser.add_argument('--mode',choices=['polish','voice_conversion','transcribe'],default='polish')
    parser.add_argument('--model',default='zhoujielun')
    parser.add_argument('--pitch',type=int,default=0)
    parser.add_argument('--f0',choices=['harvest','pm','crepe','rmvpe'],default='harvest')
    parser.add_argument('--clean',choices=list(CLEAN_PRESETS),default='light')
    parser.add_argument('--master',choices=list(MASTER_PRESETS),default='natural')
    parser.add_argument('--midi',action='store_true')
    parser.add_argument('--formats',default='wav,mp3')
    parser.add_argument('--sample-rate',type=int,choices=[44100,48000],default=48000)
    parser.add_argument('--output')
    parser.add_argument('--resume')
    parser.add_argument('--diagnose',action='store_true')
    parser.add_argument('--parameter-search',action='store_true',help='运行独立参数候选；默认不改变基线输出')
    parser.add_argument('--search-budget',choices=['fast','balanced','quality'],default='fast')
    parser.add_argument('--vocal-restoration',choices=['de_echo_normal','de_echo_aggressive'])
    parser.add_argument('--cache-preview',action='store_true',help='预览仅清理已完成任务.work的候选文件')
    parser.add_argument('--cache-clean',action='store_true',help='按容量/保留时间清理可再生缓存')
    parser.add_argument('--cache-quota-gb',type=float,default=20)
    parser.add_argument('--cache-ttl-days',type=int,default=30)
    args=parser.parse_args(arguments)
    if args.diagnose:
        from core.diagnostics import diagnostics
        print(json.dumps(diagnostics(runtime_result=runtime_result),ensure_ascii=False,indent=2));return
    pipeline=AudioPipeline()
    if args.cache_preview or args.cache_clean:
        from core.cache_manager import CacheManager
        from core.config import OUTPUT_DIR
        try:
            result=CacheManager(pipeline.storage,args.output or OUTPUT_DIR).collect(
                quota_bytes=int(args.cache_quota_gb*1024**3),ttl_days=args.cache_ttl_days,
                dry_run=not args.cache_clean)
            print(json.dumps(result,ensure_ascii=False,indent=2))
        finally:
            pipeline.storage.close()
        return
    files=args.files
    if args.resume:
        job=pipeline.storage.get_job(args.resume)
        if not job:parser.error('找不到任务')
        files=[job['source_path']]
    if not files:parser.error('请传入音频，或使用 --resume / --diagnose')
    failures=0
    try:
        for path in files:
            try:
                result=pipeline.process_file(path,mode=args.mode,clean_preset=args.clean,master_preset=args.master,
                                             rvc_model_key=args.model,pitch_shift=args.pitch,f0_method=args.f0,
                                             generate_midi=args.midi,output_formats=args.formats,output_sample_rate=args.sample_rate,
                                             custom_output_dir=args.output,resume_job_id=args.resume,control=Control(),
                                             parameter_search_enabled=args.parameter_search,
                                             parameter_search_budget=args.search_budget,
                                             vocal_restoration_effect=args.vocal_restoration)
                print(json.dumps(result,ensure_ascii=False,indent=2))
            except Exception as exc:
                failures+=1
                print(json.dumps(dict(status='FAILED',input=path,error=str(exc)),ensure_ascii=False))
    finally:
        pipeline.storage.close()
    raise SystemExit(1 if failures else 0)

if __name__=='__main__':main()

