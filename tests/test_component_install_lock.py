import json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from core.install_platform import _component_install_lock,install_offline_package

ROOT=Path(__file__).resolve().parents[1]

class ComponentInstallLockTests(unittest.TestCase):
    def test_same_process_thread_and_reentrant_install_are_rejected_before_io(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);errors=[]
            with _component_install_lock(root):
                with self.assertRaisesRegex(RuntimeError,'另一个组件安装'):
                    install_offline_package(root/'does-not-exist.ampkg',root)
                def concurrent():
                    try:
                        install_offline_package(root/'does-not-exist.ampkg',root)
                    except Exception as error:errors.append(str(error))
                worker=threading.Thread(target=concurrent);worker.start();worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors),1)
                self.assertIn('另一个组件安装',errors[0])
            with _component_install_lock(root):pass
            self.assertEqual(list(root.iterdir()),[])

    def test_separate_process_data_root_and_windows_case_alias_do_not_bypass_lock(self):
        with tempfile.TemporaryDirectory(prefix='组件锁 ') as folder:
            root=Path(folder)/'app';root.mkdir()
            code=('import sys,json\n'
                  'sys.path.insert(0,sys.argv[1])\n'
                  'from core.install_platform import install_offline_package\n'
                  'try:\n'
                  ' install_offline_package(sys.argv[2]+"/missing.ampkg",sys.argv[2])\n'
                  'except Exception as e:\n'
                  ' print(json.dumps({"type":type(e).__name__,"error":str(e)}))\n')
            env=dict(os.environ,AI_MUSIC_PROCESSOR_DATA_ROOT=str(Path(folder)/'other-user-data'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
            alias=str(root).upper() if os.name=='nt' else str(root)
            with _component_install_lock(root):
                result=subprocess.run([sys.executable,'-B','-c',code,str(ROOT),alias],cwd=str(ROOT),env=env,capture_output=True,text=True,encoding='utf-8',timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                record=json.loads(result.stdout)
                self.assertEqual(record['type'],'RuntimeError')
                self.assertIn('另一个组件安装',record['error'])
            self.assertFalse((Path(folder)/'other-user-data').exists())
            self.assertEqual(list(root.iterdir()),[])

if __name__=='__main__':unittest.main()
