import json,tempfile,unittest
from pathlib import Path
from core.runtime_benchmark import run_runtime_benchmark
from core.processes import Control


class RuntimeBenchmarkTests(unittest.TestCase):
    def test_real_twenty_second_polish_chain_and_isolated_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)/'benchmark'
            report=run_runtime_benchmark(directory,{'mode':'polish','device':'cpu','detailed_analysis':False})
            self.assertEqual(report['status'],'EXECUTED',report['error'])
            self.assertEqual(report['input_seconds'],20)
            self.assertTrue(report['history_isolated']);self.assertFalse(report['changes_preset'])
            self.assertGreater(report['elapsed_seconds'],0)
            self.assertEqual(report['jobs'][0]['status'],'DONE')
            stages=next(iter(report['stage_records'].values()))
            self.assertTrue({'Repair','Tone','Dynamics','Stereo','Master','QC'} <= {s['stage_name'] for s in stages})
            self.assertTrue((directory/'benchmark.json').is_file())
            self.assertEqual(report['quality_acceptance'],'READY_FOR_EXTERNAL_VALIDATION')

    def test_cancelled_benchmark_is_not_reported_as_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            control=Control();control.cancel()
            report=run_runtime_benchmark(Path(folder)/'cancelled',{'mode':'polish'},control)
            self.assertEqual(report['status'],'CANCELLED')
            self.assertIsNone(report['result'])
