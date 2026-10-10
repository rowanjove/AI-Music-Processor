import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.reporting import dependency_evidence


class DependencyReportTests(unittest.TestCase):
    def test_versions_and_artifact_identities_remain_stage_specific(self):
        project={'stage_records':[
            {'stage_name':'Separate','parameters':{'package_versions':{'torch':'2.0.0'},'dependency_hashes':{'model.onnx':'a'*64}}},
            {'stage_name':'RVC','parameters':{'package_versions':{'torch':'2.0.0','librosa':'not-in-runtime-metadata'},
                                           'dependency_hashes':{'model.onnx':'b'*64}}}]}
        result=dependency_evidence(project)
        self.assertIn('| torch | 2.0.0 | RVC, Separate |',result)
        self.assertIn('not-in-runtime-metadata',result)
        self.assertIn('| model.onnx | '+'a'*64+' | Separate |',result)
        self.assertIn('| model.onnx | '+'b'*64+' | RVC |',result)
        self.assertIn('未确认',dependency_evidence({}))


if __name__=='__main__':unittest.main()
