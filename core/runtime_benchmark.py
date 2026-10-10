"""Explicit real-chain benchmark; isolated history, no automatic preset changes."""
import json
import time
from pathlib import Path
from core.storage import JobStore,sha256_file
from core.processes import ProcessCancelled


def run_runtime_benchmark(directory,parameters,control=None,progress_callback=None):
    from core.pipeline import AudioPipeline
    from core.ux_flow import _write_self_test_tone,benchmark_view
    root=Path(directory);root.mkdir(parents=True,exist_ok=False)
    source=root/'20-second-input.wav'
    _write_self_test_tone(str(source),seconds=20)
    params=dict(parameters);params.pop('custom_output_dir',None);params.pop('dispatch_id',None)
    params['output_formats']=['wav']
    started=time.monotonic();result=None;error=None;status='FAILED'
    with JobStore(root/'benchmark-jobs.db') as store:
        try:
            result=AudioPipeline(storage=store,progress_callback=progress_callback).process_file(
                str(source),custom_output_dir=str(root/'output'),control=control,**params)
            status='EXECUTED'
        except ProcessCancelled as exc:error=str(exc);status='CANCELLED'
        except Exception as exc:error=str(exc)
        jobs=store.list_jobs()
        stages={job['id']:store.list_stages(job['id']) for job in jobs}
    elapsed=time.monotonic()-started
    record=dict(benchmark_view(elapsed),status=status,error=error,input_seconds=20,
                input_sha256=sha256_file(source),parameters=params,result=result,jobs=jobs,stage_records=stages,
                history_isolated=True,quality_acceptance='READY_FOR_EXTERNAL_VALIDATION',
                recommendation='可根据本次设备、阶段耗时选择执行档位；合成输入不代表真实歌曲音质或耗时。')
    (root/'benchmark.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return record
