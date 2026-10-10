import copy
import os
import subprocess
import tempfile
import unittest
from core.execution_policy import freeze_execution_policy
from core.worker_runtime import run_worker_with_recovery
from core.job_spec import freeze, validate, digest
from core.storage import CorruptJobError


class ExecutionPolicyTests(unittest.TestCase):
    def test_explicit_cuda_oom_never_switches_cpu(self):
        commands=[]
        def failed(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command,1,stdout=b'',stderr=b'CUDA out of memory')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                run_worker_with_recovery(['python','worker.py','--device','cuda'],[os.path.join(folder,'out.wav')],timeout=10,gpu_overrides={'device':'cpu'},run_fn=failed)
        self.assertEqual(len(commands),2)
        for command in commands:
            self.assertEqual(command[command.index('--device')+1],'cuda')

    def test_new_policy_and_dsp_are_frozen_legacy_remains_absent(self):
        parameters={'clean_preset':'light','master_preset':'natural','rvc_model_key':'zhoujielun','pitch_shift':0,'f0_method':'pm','index_rate':.65,'generate_midi':False,'output_formats':['wav'],'output_sample_rate':48000,'bpm':120}
        # Polish mode avoids voice-model snapshots while exercising generic freeze.
        new=freeze(parameters,'polish')
        self.assertEqual(new['execution_policy'],freeze_execution_policy())
        old_policy=dict(new['execution_policy'])
        old_policy.pop('hp2_cudnn');old_policy.pop('hp2_zero_initialized_bands')
        from core.execution_policy import validate_execution_policy
        self.assertEqual(validate_execution_policy(old_policy),old_policy)
        for key,value in [('cpu_threads',True),('hp2_window',512.0),('performance_profile',[])]:
            malformed=dict(new['execution_policy']);malformed[key]=value
            with self.assertRaises(ValueError):validate_execution_policy(malformed)
        self.assertEqual(parameters.keys(), {'clean_preset','master_preset','rvc_model_key','pitch_shift','f0_method','index_rate','generate_midi','output_formats','output_sample_rate','bpm'})
        legacy=copy.deepcopy(new);legacy.pop('execution_policy');legacy['spec_hash']=digest(legacy)
        before=copy.deepcopy(legacy)
        validate(legacy,'polish')
        self.assertEqual(legacy,before)
        damaged=copy.deepcopy(new);damaged['execution_policy']['cpu_threads']=4;damaged['spec_hash']=digest(damaged)
        with self.assertRaises(CorruptJobError):validate(damaged,'polish')
        for key,value in [('adaptive_mix',1),('rvc_postprocess_profile','fake'),('mix_bus_profile',True)]:
            bad=copy.deepcopy(new);bad[key]=value;bad['spec_hash']=digest(bad)
            with self.assertRaises(CorruptJobError):validate(bad,'polish')
