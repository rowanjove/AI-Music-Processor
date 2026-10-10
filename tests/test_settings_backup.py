import hashlib,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.settings_backup import export_settings_backup,read_settings_backup,plan_settings_restore,restore_settings_backup


class SettingsBackupTests(unittest.TestCase):
    def test_invalid_execution_policy_and_windows_profile_collisions_rejected(self):
        from core.config import normalize_settings
        self.assertNotIn('execution_policy',normalize_settings({'execution_policy':'bad'}))
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'bad.ampbackup'
            for settings in ({'execution_policy':'bad'},{'mode':'bad'},{'mix_ducking':'false'},
                             {'output_channels':3},{'input_channel_mode':'bad'}):
                with self.assertRaises(ValueError):export_settings_backup(target,settings)
            with self.assertRaises(ValueError):export_settings_backup(target,{}, {'Singer':{},'singer':{}})
            self.assertFalse(target.exists())

    def test_receipt_write_failure_rolls_back_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config';config.mkdir();(config/'app.json').write_bytes(b'old')
            source=root/'backup.ampbackup';export_settings_backup(source,{'mode':'polish'})
            write=Path.write_bytes
            def fail(path,data):
                if path.name=='restore.json':raise OSError('receipt unavailable')
                return write(path,data)
            with patch.object(Path,'write_bytes',fail),self.assertRaises(OSError):
                restore_settings_backup(source,config)
            self.assertEqual((config/'app.json').read_bytes(),b'old')
            self.assertFalse((config/'component-selection.json').exists())
    def test_metadata_roundtrip_dryrun_and_unknown_preferences_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config';config.mkdir();(config/'app.json').write_bytes(b'original')
            source=root/'settings.ampbackup'
            result=export_settings_backup(source,{'mode':'polish','future_key':{'value':2}},
                {'歌手.profile':{'pitch':2}},['mode.polish'],{'singer':{'name':'歌手','pth_sha256':'a'*64}})
            self.assertFalse(result['contains_model_weights'])
            plan=plan_settings_restore(source,config)
            self.assertFalse(plan['writes_performed']);self.assertEqual((config/'app.json').read_bytes(),b'original')
            restored=restore_settings_backup(source,config,plan)
            self.assertEqual(restored['status'],'RESTORED')
            self.assertEqual(json.loads((config/'app.json').read_text())['future_key'],{'value':2})
            self.assertEqual((Path(restored['backup_directory'])/'app.json').read_bytes(),b'original')
            self.assertEqual(json.loads((config/'component-selection.json').read_text()),['mode.polish'])
            self.assertEqual(json.loads((config/'model-profiles/歌手.profile.json').read_text()),{'pitch':2})
            self.assertEqual(list(root.rglob('*.pth')),[])

    def test_failed_commit_restores_originals_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config';config.mkdir();(config/'app.json').write_bytes(b'old')
            source=root/'backup.ampbackup';export_settings_backup(source,{'mode':'polish'})
            original=os.replace
            def fail(a,b):
                if Path(b).name=='component-selection.json':raise OSError('injected write failure')
                return original(a,b)
            with patch('core.settings_backup.os.replace',side_effect=fail),self.assertRaises(OSError):
                restore_settings_backup(source,config)
            self.assertEqual((config/'app.json').read_bytes(),b'old')
            self.assertFalse((config/'component-selection.json').exists())
            self.assertEqual([p for p in config.rglob('*.tmp')],[])
            self.assertEqual(len(list((config/'backup-history').rglob('app.json'))),1)

    def test_changed_preview_future_format_and_archive_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config';source=root/'backup.ampbackup'
            export_settings_backup(source,{'mode':'polish'});plan=plan_settings_restore(source,config)
            export_settings_backup(source,{'mode':'transcribe'})
            with self.assertRaises(ValueError):restore_settings_backup(source,config,plan)
            self.assertFalse(config.exists())
            with zipfile.ZipFile(source) as archive:payload=archive.read('payload.json');manifest=json.loads(archive.read('manifest.json'))
            manifest['schema_version']=99
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('manifest.json',json.dumps(manifest));archive.writestr('payload.json',payload)
            with self.assertRaises(ValueError):read_settings_backup(source)
            with zipfile.ZipFile(source,'w') as archive:archive.writestr('../outside.json','{}')
            with self.assertRaises(ValueError):read_settings_backup(source)
            self.assertFalse((root/'outside.json').exists())

    def test_v0_named_settings_migrates_without_discarding_unknown_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'old.ampbackup'
            payload=json.dumps({'settings':{'original_field':True},'profiles':{},'component_selection':[],
                                'model_metadata':{}}).encode()
            manifest={'format':'AI-Music-Processor-settings','schema_version':0,
                      'payload_sha256':hashlib.sha256(payload).hexdigest(),'contains_model_weights':False}
            with zipfile.ZipFile(source,'w') as archive:
                archive.writestr('manifest.json',json.dumps(manifest));archive.writestr('payload.json',payload)
            migrated=read_settings_backup(source)
            self.assertEqual(migrated['target_version'],1)
            self.assertEqual(migrated['payload']['app_settings'],{'original_field':True})


if __name__=='__main__':unittest.main()
