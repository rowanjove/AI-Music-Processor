import hashlib,json,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.editor_install import install_editor,_run_installer
from core.component_location import resolve_editor_location

class EditorInstallTests(unittest.TestCase):
    def fixture(self,base,member='Audacity.exe'):
        root=base/'app';(root/'packaging').mkdir(parents=True)
        pe=bytearray(128);pe[:2]=b'MZ';pe[60:64]=(64).to_bytes(4,'little');pe[64:68]=b'PE\0\0'
        archive=base/'source.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr(member,pe)
        entry={'version':'fixture','filename':'editor.zip','url':'https://github.com/test/editor.zip','sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'size':archive.stat().st_size,'strategy':'zip','executables':['audacity.exe']}
        (root/'packaging/editor-sources.json').write_text(json.dumps({'editors':{'audacity':entry}}),encoding='utf-8')
        def download(urls,path,*args,**kwargs):Path(path).write_bytes(archive.read_bytes())
        return root,base/'user',download

    def test_real_zip_and_pe_registration_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root,data,download=self.fixture(Path(folder))
            with patch('core.editor_install.download_verified',side_effect=download):result=install_editor(root,data,'audacity')
            self.assertEqual(result['status'],'INSTALLED')
            self.assertEqual(resolve_editor_location(data/'config','audacity',None),result['location']['path'])
            self.assertFalse((root/'tools').exists())

    def test_zip_traversal_cannot_write_or_register(self):
        with tempfile.TemporaryDirectory() as folder:
            root,data,download=self.fixture(Path(folder),'../../escape.exe')
            with patch('core.editor_install.download_verified',side_effect=download),self.assertRaises(ValueError):install_editor(root,data,'audacity')
            self.assertFalse((data/'config/external-editors.json').exists())
            self.assertFalse((Path(folder)/'escape.exe').exists())

    def test_failed_registration_removes_new_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root,data,download=self.fixture(Path(folder))
            with patch('core.editor_install.download_verified',side_effect=download),patch('core.editor_install.save_editor_location',side_effect=OSError('settings locked')),self.assertRaises(OSError):install_editor(root,data,'audacity')
            self.assertEqual(list((data/'external-tools').iterdir()),[])

    def test_cache_only_does_not_claim_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            root,data,download=self.fixture(Path(folder))
            with patch('core.editor_install.download_verified',side_effect=download):result=install_editor(root,data,'audacity',cache_only=True)
            self.assertEqual(result['status'],'CACHED_NOT_INSTALLED');self.assertFalse((data/'config').exists())

    @unittest.skipUnless(sys.platform=='win32','Windows UAC')
    def test_only_installer_is_elevated_and_arguments_stay_literal(self):
        import base64
        from types import SimpleNamespace
        error=OSError('requires elevation');error.winerror=740
        command=[r"C:\installer path\setup.exe",'/S',r"/D=C:\user's data\editor"]
        with patch('core.editor_install.subprocess.run',side_effect=[error,SimpleNamespace(returncode=0)]) as run:
            self.assertEqual(_run_installer(command),0)
        shell_command=run.call_args_list[1][0][0]
        script=base64.b64decode(shell_command[-1]).decode('utf-16-le')
        self.assertIn('-Verb RunAs',script);self.assertIn("user''s data",script)
        self.assertNotIn('python',script.lower())
