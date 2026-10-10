import hashlib,json,tempfile,unittest
from pathlib import Path
from core.license_inventory import generate_license_dossier,verify_license_dossier

class LicenseInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        metadata=self.root/'runtime/addons/fixture.dist-info';metadata.mkdir(parents=True)
        (metadata/'METADATA').write_text('Name: fixture\nVersion: 1\nLicense: MIT\nAuthor: fixture author\nHome-page: https://example.invalid/fixture\n',encoding='utf-8')
        p=metadata/'LICENSE';p.write_bytes(b'fixture license text')
        manifest=self.root/'runtime-manifest';manifest.mkdir()
        self.write('packages.lock.json',{'distributions':[{'normalized_name':'fixture','name':'fixture','version':'1','metadata_root':'runtime/addons/fixture.dist-info'}]})
        self.write('models.json',{'assets':[{'id':'fixture-model','files':[{'path':'runtime/model.pth','sha256':'a'*64,'size':10}],'license_status':'UNKNOWN'}]})
        self.write('inventory.json',{'files':[{'path':str(f.relative_to(self.root)).replace('\\','/'),'size':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in (p,metadata/'METADATA')]})
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,record):
        (self.root/'runtime-manifest'/name).write_text(json.dumps(record),encoding='utf-8')
    def mutate(self,change):
        p=self.root/'LICENSES/manifest.json';record=json.loads(p.read_text(encoding='utf-8'));change(record);p.write_text(json.dumps(record),encoding='utf-8')
    def test_real_text_copied_unknown_preserved_and_public_blocked(self):
        result=generate_license_dossier(self.root)
        self.assertEqual(result['status'],'PASS_WITH_WARNINGS')
        self.assertEqual(result['subjects'],2)
        self.assertEqual(result['license_texts'],1)
        self.assertEqual(verify_license_dossier(self.root,public_release=True)['status'],'FAIL')
        data=json.loads((self.root/'LICENSES/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(data['subjects'][0]['declared_license'],'MIT')
        self.assertEqual(data['subjects'][0]['redistributable'],'UNKNOWN')
        self.assertEqual(data['subjects'][1]['license_text_status'],'NOT_LOCATED')
        self.assertEqual((self.root/data['texts'][0]['text_path']).read_bytes(),b'fixture license text')
        with self.assertRaises(FileExistsError):generate_license_dossier(self.root)
    def test_modified_license_text_rejected(self):
        generate_license_dossier(self.root)
        next((self.root/'LICENSES/texts').glob('*.txt')).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'identity mismatch'):verify_license_dossier(self.root)
    def test_removed_subject_and_runtime_model_identity_rejected(self):
        generate_license_dossier(self.root)
        self.write('models.json',{'assets':[{'id':'fixture-model','files':[{'path':'runtime/model.pth','sha256':'b'*64,'size':10}]}]})
        with self.assertRaisesRegex(ValueError,'model identity'):verify_license_dossier(self.root)
    def test_fake_approval_and_path_escape_rejected(self):
        generate_license_dossier(self.root)
        self.mutate(lambda r:r.update(public_release_allowed=True))
        with self.assertRaisesRegex(ValueError,'self-approve'):verify_license_dossier(self.root)
        self.mutate(lambda r:r.update(public_release_allowed=False))
        self.mutate(lambda r:r['texts'][0].update(text_path='../outside'))
        with self.assertRaisesRegex(ValueError,'Unsafe'):verify_license_dossier(self.root)
    def test_missing_text_coverage_and_source_tamper_rejected(self):
        generate_license_dossier(self.root)
        self.mutate(lambda r:r.update(texts=[]))
        with self.assertRaisesRegex(ValueError,'coverage'):verify_license_dossier(self.root)

    def test_runtime_license_source_and_copy_cannot_repin_themselves(self):
        generate_license_dossier(self.root)
        source=self.root/'runtime/addons/fixture.dist-info/LICENSE';source.write_bytes(b'forged replacement')
        data=json.loads((self.root/'LICENSES/manifest.json').read_text(encoding='utf-8'))
        text=self.root/data['texts'][0]['text_path'];text.write_bytes(source.read_bytes())
        self.mutate(lambda r:r['texts'][0].update(sha256=hashlib.sha256(source.read_bytes()).hexdigest(),size=source.stat().st_size))
        with self.assertRaisesRegex(ValueError,'pinned inventory'):verify_license_dossier(self.root)

    def test_source_code_named_license_is_not_fabricated_license_text(self):
        code=self.root/'core/license_example.py';code.parent.mkdir();code.write_bytes(b'print("application code")')
        inventory=json.loads((self.root/'runtime-manifest/inventory.json').read_text(encoding='utf-8'))
        inventory['files'].append({'path':'core/license_example.py','size':code.stat().st_size,'sha256':hashlib.sha256(code.read_bytes()).hexdigest()})
        self.write('inventory.json',inventory)
        self.assertEqual(generate_license_dossier(self.root)['license_texts'],1)

    def test_index_and_subject_description_cannot_claim_different_license(self):
        generate_license_dossier(self.root)
        path=self.root/'THIRD_PARTY_LICENSES.md';original=path.read_text(encoding='utf-8')
        path.write_text(original+'All rights approved',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'index differs'):verify_license_dossier(self.root)
        path.write_text(original,encoding='utf-8')
        self.mutate(lambda r:r['subjects'][0].update(declared_license='APPROVED'))
        with self.assertRaisesRegex(ValueError,'description/evidence'):verify_license_dossier(self.root)

    def test_missing_pinned_metadata_cannot_be_downgraded_to_unknown(self):
        from core.license_inventory import render_license_index
        generate_license_dossier(self.root)
        (self.root/'runtime/addons/fixture.dist-info/METADATA').unlink()
        self.mutate(lambda r:r['subjects'][0].update(declared_license='UNKNOWN',author='UNKNOWN',source='UNKNOWN'))
        record=json.loads((self.root/'LICENSES/manifest.json').read_text(encoding='utf-8'))
        (self.root/'THIRD_PARTY_LICENSES.md').write_text(render_license_index(record['subjects']),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'metadata.*inventory'):verify_license_dossier(self.root)

if __name__=='__main__':unittest.main()
