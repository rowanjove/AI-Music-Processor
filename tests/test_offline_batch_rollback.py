import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core.install_platform import install_offline_package
from core.storage import sha256_file


class OfflineBatchRollbackTests(unittest.TestCase):
    def test_second_commit_failure_restores_first_and_preserves_donor(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);live=root/'live';donor=root/'donor';rows=[]
            for name in ('first.bin','second.bin'):
                target=live/'runtime'/name;source=donor/'runtime'/name
                target.parent.mkdir(parents=True,exist_ok=True);source.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(b'old-'+name.encode());source.write_bytes(b'new-'+name.encode())
                rows.append(dict(path='runtime/'+name,sha256=sha256_file(source)))
            (donor/'manifest.json').write_text(json.dumps({'files':rows}),encoding='utf-8')
            (live/'runtime-manifest').mkdir()
            (live/'runtime-manifest/inventory.json').write_text(json.dumps({'files':rows}),encoding='utf-8')
            replace=os.replace
            def fail(a,b):
                if Path(b)==live/'runtime/second.bin':raise OSError('second file locked')
                return replace(a,b)
            with patch('core.install_platform.os.replace',fail),self.assertRaises(OSError):
                install_offline_package(donor,live)
            for name in ('first.bin','second.bin'):
                self.assertEqual((live/'runtime'/name).read_bytes(),b'old-'+name.encode())
                self.assertEqual((donor/'runtime'/name).read_bytes(),b'new-'+name.encode())
            result=install_offline_package(donor,live)
            self.assertTrue(result['restart_required']);self.assertEqual(len(result['installed']),2)
