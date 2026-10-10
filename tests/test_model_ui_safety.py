import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

import AI_Music_Processor as ui


class _GuardContext:
    _model_stat_signature=staticmethod(ui.AIMusicProcessorApp._model_stat_signature)


class ModelUiSafetyTests(unittest.TestCase):
    def test_visible_capability_labels_distinguish_present_from_runnable(self):
        readable_incomplete={'checkpoint':{'readable':True,'inference_compatible':False}}
        unreadable={'checkpoint':{'readable':False}}
        runnable={'checkpoint':{'readable':True,'inference_compatible':True},
                  'index':{'readable':True,'dimension_relation':'MATCH'}}
        self.assertIn('不完整',ui.AIMusicProcessorApp._model_availability_text(readable_incomplete))
        self.assertIn('解析拒绝',ui.AIMusicProcessorApp._model_availability_text(unreadable))
        self.assertIn('可推理',ui.AIMusicProcessorApp._model_availability_text(runnable))

    def test_rvc_start_requires_complete_model_and_safe_matching_index(self):
        with tempfile.TemporaryDirectory() as folder:
            pth=Path(folder)/'voice.pth'
            pth.write_bytes(b'model')
            index=Path(folder)/'voice.index'
            index.write_bytes(b'index')
            signature=ui.AIMusicProcessorApp._model_stat_signature(str(pth))
            model_info={'pth':str(pth),'index':str(index)}
            context=_GuardContext()
            context.models={'voice':model_info}
            context.model_inspection_cache={str(pth):(signature,{
                'checkpoint':{'inference_compatible':False},'index':{'readable':True}},None)}
            params={'rvc_model_key':'voice','index_rate':0.65}
            self.assertIn('未通过完整',ui.AIMusicProcessorApp._rvc_model_run_guard(context,params))
            context.model_inspection_cache[str(pth)]=(signature,{
                'checkpoint':{'inference_compatible':True},
                'index':{'readable':False,'dimension_relation':'NOT_ASSESSED'}},None)
            self.assertIn('Index 设为 0',ui.AIMusicProcessorApp._rvc_model_run_guard(context,params))
            params['index_rate']=0.0
            self.assertIsNone(ui.AIMusicProcessorApp._rvc_model_run_guard(context,params))


if __name__=='__main__':
    unittest.main()
