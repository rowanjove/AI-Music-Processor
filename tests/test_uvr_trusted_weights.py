import hashlib,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import uvr_trusted_weights as trusted


class DummyConstructor:
    def __init__(self,model_path,**parameters):
        self.de_reverb='DeReverb' in str(model_path)
        Path(model_path).write_bytes(b'replaced-after-authentication')
        self.loaded=torch.load(model_path,map_location='cpu')


class UVRTrustedWeightsTests(unittest.TestCase):
    def test_immutable_stream_and_filename_architecture_survive_path_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'VR-DeEchoDeReverb.pth';data=b'known-checkpoint';path.write_bytes(data)
            calls=[]
            def load(stream,**options):
                self.assertFalse(isinstance(stream,(str,Path)))
                value=stream.read();calls.append(value);return value
            with patch.dict(trusted.APPROVED,{path.name:(len(data),hashlib.sha256(data).hexdigest())}), \
                 patch.dict(DummyConstructor.__init__.__globals__,{'torch':SimpleNamespace(load=load)}):
                instance=trusted.create_trusted_uvr_model(DummyConstructor,str(path))
            self.assertTrue(instance.de_reverb)
            self.assertEqual(calls,[data])
            self.assertEqual(instance.loaded,data)
            self.assertEqual(path.read_bytes(),b'replaced-after-authentication')
            self.assertEqual(instance.authenticated_checkpoint['loader'],'authenticated_immutable_checkpoint_stream')

    def test_unknown_or_modified_model_is_refused_before_constructor_or_loader(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('unknown.pth','HP2_all_vocals.pth'):
                path=Path(folder)/name;path.write_bytes(b'not-an-approved-model')
                with patch.dict(DummyConstructor.__init__.__globals__,{'torch':SimpleNamespace(
                        load=lambda *a,**k:self.fail('unauthenticated loader ran'))}):
                    with self.assertRaises(ValueError):trusted.create_trusted_uvr_model(DummyConstructor,str(path))
                self.assertEqual(path.read_bytes(),b'not-an-approved-model')


if __name__=='__main__':unittest.main()
