import os
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.paths import PORTABLE_RELEASE_CONTENT


def _framework_csc():
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for relative in (r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                     r"Microsoft.NET\Framework\v4.0.30319\csc.exe"):
        candidate = windows / relative
        if candidate.is_file():
            return candidate
    return None


@unittest.skipUnless(os.name == "nt" and _framework_csc(), "Windows .NET Framework C# compiler required")
class LauncherGateTests(unittest.TestCase):
    def test_normal_launch_has_visible_progress_and_cancels_owned_verifier(self):
        with tempfile.TemporaryDirectory(prefix='amp visible startup ') as directory:
            root=Path(directory)
            runtime=root/'tools/RVC/RVC-beta-v2-0618/runtime';runtime.mkdir(parents=True)
            (root/'PORTABLE_RELEASE').write_text(PORTABLE_RELEASE_CONTENT,encoding='ascii')
            (root/'AI_Music_Processor.pyw').write_text('# fixture',encoding='utf-8')
            stub=root/'SlowStub.cs'
            stub.write_text('using System; class SlowStub {static int Main(){Console.OutputEncoding=System.Text.Encoding.UTF8;Console.WriteLine("AMP_RUNTIME_PROGRESS:校验模型 10/35001");Console.Out.Flush();System.Threading.Thread.Sleep(10000);return 0;}}',encoding='utf-8')
            compiled=subprocess.run([str(_framework_csc()),'/nologo','/target:exe','/out:'+str(runtime/'python.exe'),str(stub)],capture_output=True,timeout=60)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            shutil.copyfile(runtime/'python.exe',runtime/'pythonw.exe')
            harness=root/'VisibleHarness.cs'
            harness.write_text(r'''
using System;using System.Reflection;using System.Windows.Forms;using System.Diagnostics;
class VisibleHarness {
 [STAThread] static int Main(){
  bool visible=false,progress=false;Stopwatch elapsed=Stopwatch.StartNew();
  Timer timer=new Timer{Interval=50};
  timer.Tick+=delegate{
   foreach(Form f in Application.OpenForms){
    if(!f.Text.Contains("正在启动"))continue;
    if(f.Visible&&elapsed.ElapsedMilliseconds<1500)visible=true;
    foreach(Control c in f.Controls)if(c is Label&&c.Text.Contains("校验模型 10/35001"))progress=true;
    if(progress){timer.Stop();foreach(Control c in f.Controls)if(c is Button)((Button)c).PerformClick();}
   }
  };
  timer.Start();
  int result=(int)typeof(Launcher).GetMethod("Run",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{new string[0]});
  timer.Dispose();return visible&&progress&&result==9&&elapsed.ElapsedMilliseconds<8000?0:7;
 }
}''',encoding='utf-8')
            binary=root/'VisibleHarness.exe'
            compiled=subprocess.run([str(_framework_csc()),'/nologo','/target:exe','/main:VisibleHarness','/define:PORTABLE_BUILD','/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll','/out:'+str(binary),str(ROOT/'app/Launcher.cs'),str(harness)],capture_output=True,timeout=60)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            p=subprocess.run([str(binary)],cwd=str(root),capture_output=True,timeout=20)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)

    def test_missing_console_python_repairs_before_verified_gui_start(self):
        with tempfile.TemporaryDirectory(prefix='amp console repair ') as directory:
            root=Path(directory)/'program';donor=Path(directory)/'donor'
            relative='tools/RVC/RVC-beta-v2-0618/runtime/python.exe'
            (root/relative).parent.mkdir(parents=True);(donor/relative).parent.mkdir(parents=True)
            stub_source=Path(directory)/'ReadyStub.cs'
            stub_source.write_text('using System; class Stub { static int Main(string[] args) {'
                'int i=Array.IndexOf(args,"--startup-token"); if(i<0)return 23; '
                'Console.WriteLine("AMP_RUNTIME_READY:"+args[i+1]); return 0; }}',encoding='utf-8')
            stub=donor/relative
            built=subprocess.run([str(_framework_csc()),'/nologo','/target:exe','/out:'+str(stub),str(stub_source)],
                                 capture_output=True,timeout=60)
            self.assertEqual(built.returncode,0,built.stderr)
            shutil.copyfile(stub,root/Path(relative).with_name('pythonw.exe'))
            (root/'AI_Music_Processor.pyw').write_text('# fixture',encoding='utf-8')
            (root/'PORTABLE_RELEASE').write_text(PORTABLE_RELEASE_CONTENT,encoding='ascii')
            (root/'runtime-manifest').mkdir()
            (root/'runtime-manifest/inventory.json').write_text(json.dumps({'files':[{
                'path':relative,'size':stub.stat().st_size,'sha256':hashlib.sha256(stub.read_bytes()).hexdigest()}]}),encoding='utf-8')
            harness=Path(directory)/'ConsoleRepairHarness.cs'
            harness.write_text(r'''
using System; using System.IO; using System.Reflection; using System.Windows.Forms;
class ConsoleRepairHarness {
 [STAThread] static int Main(string[] args) {
  bool repaired=false; Exception failure=null; Timer timer=new Timer { Interval=50 };
  timer.Tick += delegate {
   foreach(Form form in Application.OpenForms) {
    if(!form.Text.Contains("离线安装 / 修复"))continue;
    timer.Stop();
    try {
     MethodInfo repair=typeof(Launcher).GetMethod("RepairFromDirectory",BindingFlags.Static|BindingFlags.NonPublic);
     repaired=(int)repair.Invoke(null,new object[]{AppDomain.CurrentDomain.BaseDirectory,args[0],new Action<string>(Console.WriteLine)})==1;
     form.DialogResult=DialogResult.OK; form.Close();
    } catch(Exception error) { failure=error; form.DialogResult=DialogResult.Cancel;form.Close(); }
    break;
   }
  };
  timer.Start();
  int code=(int)typeof(Launcher).GetMethod("Run",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{new string[0]});
  timer.Dispose();
  if(failure!=null){Console.Error.WriteLine(failure);return 9;}
  return repaired && code==0 ? 0 : 8;
 }
}
''',encoding='utf-8')
            binary=root/'ConsoleRepairHarness.exe'
            compiled=subprocess.run([str(_framework_csc()),'/nologo','/target:exe','/main:ConsoleRepairHarness',
                '/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll','/define:PORTABLE_BUILD',
                '/out:'+str(binary),str(ROOT/'app/Launcher.cs'),str(harness)],capture_output=True,timeout=60)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            repaired=subprocess.run([str(binary),str(donor)],capture_output=True,timeout=30)
            self.assertEqual(repaired.returncode,0,repaired.stdout+repaired.stderr)
            self.assertEqual((root/relative).read_bytes(),stub.read_bytes())

    def test_native_bootstrap_form_renders_without_python_and_cancel_is_safe(self):
        with tempfile.TemporaryDirectory(prefix='amp native bootstrap ') as directory:
            root = Path(directory)
            harness_source = root / 'NativeBootstrapHarness.cs'
            harness_source.write_text(r'''
using System;
using System.IO;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;
class NativeBootstrapHarness {
  [STAThread] static int Main(string[] args) {
    bool inspected=false;
    Exception failure=null;
    Timer timer=new Timer { Interval=100 };
    timer.Tick += delegate {
      foreach(Form form in Application.OpenForms) {
        if(!form.Text.Contains("离线安装 / 修复")) continue;
        try {
          bool status=false, repair=false, cancel=false;
          Button cancelButton=null;
          foreach(Control control in form.Controls) {
            if(control is Label && control.Text.Contains("SHA-256") && control.Text.Contains("用户数据保留")) status=true;
            if(control is Button && control.Text.Contains("选择完整离线组件目录")) repair=true;
            if(control is Button && control.Text=="取消") { cancel=true; cancelButton=(Button)control; }
          }
          if(!status||!repair||!cancel||form.Width<600||!form.Visible)
            throw new Exception("Native bootstrap controls did not actually render");
          using(Bitmap image=new Bitmap(form.Width,form.Height)) {
            form.DrawToBitmap(image,new Rectangle(0,0,image.Width,image.Height));
            image.Save(Path.Combine(args[0],"bootstrap.png"));
          }
          inspected=true;
          timer.Stop();
          cancelButton.PerformClick();
        } catch(Exception error) { failure=error; timer.Stop(); form.Close(); }
        break;
      }
    };
    timer.Start();
    MethodInfo method=typeof(Launcher).GetMethod("ShowRepair",BindingFlags.NonPublic|BindingFlags.Static);
    bool accepted=(bool)method.Invoke(null,new object[]{args[0]});
    timer.Dispose();
    if(failure!=null) { Console.Error.WriteLine(failure.Message); return 3; }
    return inspected&&!accepted&&!Directory.Exists(Path.Combine(args[0],"tools")) ? 0 : 4;
  }
}
''', encoding='utf-8')
            harness = root / 'NativeBootstrapHarness.exe'
            compiled = subprocess.run([str(_framework_csc()), '/nologo', '/target:exe',
                                       '/main:NativeBootstrapHarness', '/reference:System.Windows.Forms.dll',
                                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                                       '/out:' + str(harness), str(ROOT / 'app/Launcher.cs'), str(harness_source)],
                                      capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            rendered = subprocess.run([str(harness), str(root)], capture_output=True, timeout=20)
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            self.assertGreater((root / 'bootstrap.png').stat().st_size, 1000)
            self.assertFalse((root / 'tools').exists())

    def test_offline_repair_checks_hashes_preserves_data_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory(prefix='amp offline repair ') as directory:
            root = Path(directory) / 'program'
            donor = Path(directory) / 'offline'
            (root / 'core').mkdir(parents=True)
            (root / 'runtime-manifest').mkdir()
            (donor / 'core').mkdir(parents=True)
            target = root / 'core' / 'component.py'
            target.write_bytes(b'corrupt')
            content = b'correct component'
            (donor / 'core' / 'component.py').write_bytes(b'wrong donor')
            preserved = root / 'user-task.json'
            preserved.write_bytes(b'keep me')
            row = {'path': 'core/component.py', 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            inventory = root / 'runtime-manifest' / 'inventory.json'
            inventory.write_text(json.dumps({'files': [row]}), encoding='utf-8')
            launcher = root / 'repair.exe'
            compiled = subprocess.run([str(_framework_csc()), '/nologo', '/target:exe',
                                      '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
                                      '/out:' + str(launcher), str(ROOT / 'app/Launcher.cs')],
                                     capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            rejected = subprocess.run([str(launcher), '--repair-from', str(donor)], capture_output=True, timeout=20)
            self.assertEqual(rejected.returncode, 8)
            self.assertEqual(target.read_bytes(), b'corrupt')
            (donor / 'core/component.py').write_bytes(content)
            repaired = subprocess.run([str(launcher), '--repair-from', str(donor)], capture_output=True, timeout=20)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(preserved.read_bytes(), b'keep me')
            row['path'] = 'core/../../escape.py'
            inventory.write_text(json.dumps({'files': [row]}), encoding='utf-8')
            traversal = subprocess.run([str(launcher), '--repair-from', str(donor)], capture_output=True, timeout=20)
            self.assertEqual(traversal.returncode, 8)
            self.assertFalse((Path(directory) / 'escape.py').exists())

    def test_release_launcher_waits_for_verifier_and_blocks_or_starts(self):
        with tempfile.TemporaryDirectory(prefix="amp launcher gate ") as directory:
            root = Path(directory)
            runtime = root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime"
            runtime.mkdir(parents=True)
            marker = root / "PORTABLE_RELEASE"
            marker.write_text(PORTABLE_RELEASE_CONTENT, encoding="ascii")
            (root / "AI_Music_Processor.pyw").write_text("# launcher fixture\n", encoding="utf-8")
            stub_source = root / "python_stub.cs"
            stub_source.write_text(
                "using System; using System.IO; "
                "class PythonStub { static int Main() { string[] args=Environment.GetCommandLineArgs(); "
                "string name=Path.GetFileNameWithoutExtension(Environment.GetCommandLineArgs()[0]); "
                "if(name==\"python\") { File.WriteAllText(Path.Combine(Environment.CurrentDirectory,\"verify-args.txt\"),string.Join(\"|\",Environment.GetCommandLineArgs())); "
                "File.AppendAllText(Path.Combine(Environment.CurrentDirectory,\"verification-count.txt\"),\"1\\n\"); "
                "if(Environment.GetEnvironmentVariable(\"AMP_VERIFIER_FAIL\")==\"1\") return 23; "
                "int token=Array.IndexOf(args,\"--startup-token\"); "
                "if(token<0) return 0; "
                "if(Environment.GetEnvironmentVariable(\"AMP_WRONG_TOKEN\")==\"1\") { Console.WriteLine(\"AMP_RUNTIME_READY:WRONG\"); return 0; } "
                "Console.WriteLine(\"AMP_RUNTIME_READY:\"+args[token+1]); Console.Out.Flush(); "
                "System.Threading.Thread.Sleep(200); "
                "File.WriteAllText(Path.Combine(Environment.CurrentDirectory,\"app-started.flag\"),\"started\"); return 0; } "
                "File.WriteAllText(Path.Combine(Environment.CurrentDirectory,\"app-started.flag\"),\"started\"); "
                "return 0; } }",
                encoding="utf-8")
            stub_exe = root / "PythonStub.exe"
            compile_stub = subprocess.run([str(_framework_csc()), "/nologo", "/target:exe",
                                           "/out:" + str(stub_exe), str(stub_source)],
                                          capture_output=True, text=True, timeout=60)
            self.assertEqual(compile_stub.returncode, 0, compile_stub.stdout + compile_stub.stderr)
            shutil.copy2(stub_exe, runtime / "python.exe")
            shutil.copy2(stub_exe, runtime / "pythonw.exe")

            launcher = root / "LauncherTest.exe"
            compile_launcher = subprocess.run([
                str(_framework_csc()), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
                "/define:PORTABLE_BUILD",
                "/out:" + str(launcher), str(ROOT / "app" / "Launcher.cs")],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(compile_launcher.returncode, 0, compile_launcher.stdout + compile_launcher.stderr)

            env = dict(os.environ, AMP_VERIFIER_FAIL="1")
            failed = subprocess.run([str(launcher), "--smoke-test"], cwd=str(root), env=env,
                                    capture_output=True, timeout=30)
            self.assertEqual(failed.returncode, 5)
            self.assertFalse((root / "app-started.flag").exists(), "failed verifier must block app launch")

            marker.unlink()
            (root / "release-manifest.json").write_text("portable fixture", encoding="utf-8")
            missing_marker = subprocess.run([str(launcher), "--smoke-test"], cwd=str(root), env=env,
                                            capture_output=True, timeout=30)
            self.assertEqual(missing_marker.returncode, 7)
            self.assertFalse((root / "app-started.flag").exists())
            self.assertFalse(any((root / name).exists() for name in ("config", "output", "temp")))

            (root / "release-manifest.json").unlink()
            marker.mkdir()
            directory_marker = subprocess.run([str(launcher), "--smoke-test"], cwd=str(root), env=env,
                                               capture_output=True, timeout=30)
            self.assertEqual(directory_marker.returncode, 7)
            marker.rmdir()
            marker.write_text(PORTABLE_RELEASE_CONTENT, encoding="ascii")

            env["AMP_VERIFIER_FAIL"] = "0"
            quoted_data_root = str(root.parent / 'Unicode 数据 root with spaces')
            verified = subprocess.run([str(launcher), '--verify-runtime', '--data-root', quoted_data_root],
                                      cwd=str(root), env=env, capture_output=True, timeout=30)
            self.assertEqual(verified.returncode, 0)
            captured = (root / 'verify-args.txt').read_text(encoding='utf-8-sig').split('|')
            self.assertEqual(captured[captured.index('--data-root') + 1], quoted_data_root)
            self.assertFalse((root / 'app-started.flag').exists())
            count_before=len((root/'verification-count.txt').read_text().splitlines())
            passed = subprocess.run([str(launcher), "--smoke-test"], cwd=str(root), env=env,
                                    capture_output=True, timeout=30)
            self.assertEqual(passed.returncode, 0)
            self.assertEqual(len((root/'verification-count.txt').read_text().splitlines()),count_before+1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (root / "app-started.flag").exists():
                time.sleep(0.05)
            self.assertTrue((root / "app-started.flag").is_file(), "successful verifier should allow app launch")
            (root/'app-started.flag').unlink()
            env['AMP_WRONG_TOKEN']='1'
            wrong=subprocess.run([str(launcher),'--smoke-test'],cwd=str(root),env=env,capture_output=True,timeout=30)
            self.assertEqual(wrong.returncode,5)
            self.assertFalse((root/'app-started.flag').exists())
            env.pop('AMP_WRONG_TOKEN')
            # Shorten the ready wait in this test build only. A GUI session
            # longer than that deadline must survive once it sent ready.
            short_source=root/'ShortGateLauncher.cs'
            short_source.write_text((ROOT/'app/Launcher.cs').read_text(encoding='utf-8').replace(
                'startupSignal.WaitOne(930000)','startupSignal.WaitOne(100)'),encoding='utf-8')
            short_launcher=root/'ShortGate.exe'
            short_compiled=subprocess.run([str(_framework_csc()),'/nologo','/target:winexe',
                '/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll','/define:PORTABLE_BUILD',
                '/out:'+str(short_launcher),str(short_source)],capture_output=True,text=True,timeout=60)
            self.assertEqual(short_compiled.returncode,0,short_compiled.stdout+short_compiled.stderr)
            survived=subprocess.run([str(short_launcher),'--smoke-test'],cwd=str(root),env=env,capture_output=True,timeout=30)
            self.assertEqual(survived.returncode,0)
            self.assertTrue((root/'app-started.flag').exists())

            dev_root = root / "development"
            dev_runtime = dev_root / "tools" / "RVC" / "RVC-beta-v2-0618" / "runtime"
            dev_runtime.mkdir(parents=True)
            shutil.copy2(runtime / "pythonw.exe", dev_runtime / "pythonw.exe")
            (dev_root / "AI_Music_Processor.pyw").write_text("# dev launcher fixture\n", encoding="utf-8")
            dev_launcher = dev_root / "LauncherDevTest.exe"
            compile_dev = subprocess.run([
                str(_framework_csc()), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
                "/out:" + str(dev_launcher), str(ROOT / "app" / "Launcher.cs")],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(compile_dev.returncode, 0, compile_dev.stdout + compile_dev.stderr)
            dev_start = subprocess.run([str(dev_launcher), "--smoke-test"], cwd=str(dev_root),
                                       capture_output=True, timeout=30)
            self.assertEqual(dev_start.returncode, 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (dev_root / "app-started.flag").exists():
                time.sleep(0.05)
            self.assertTrue((dev_root / "app-started.flag").is_file(),
                            "unmarked dev build keeps direct pythonw launch behavior")


    def test_online_installer_handles_exit_code_and_retry_ui(self):
        with tempfile.TemporaryDirectory(prefix='amp online install test ') as directory:
            root = Path(directory)
            (root / 'LIGHTWEIGHT_RELEASE').write_text('AI Music Processor Lightweight Release v1\n', encoding='ascii')
            scripts = root / 'scripts'
            scripts.mkdir(parents=True)
            real_installer = scripts / 'install_components.ps1'
            real_installer.write_text('# placeholder', encoding='utf-8')

            mock_fail = root / 'mock_fail.ps1'
            mock_fail.write_text('exit 42', encoding='utf-8')

            harness_source = root / 'OnlineInstallHarness.cs'
            harness_source.write_text(r'''
using System;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
class OnlineInstallHarness {
  [STAThread] static int Main(string[] args) {
    bool failedStateObserved = false;
    Exception failure = null;
    Timer timer = new Timer { Interval = 100 };
    int ticks = 0;
    Button onlineBtn = null;
    Label statusLbl = null;
    Button cancelBtn = null;
    timer.Tick += delegate {
      ticks++;
      foreach (Form form in Application.OpenForms) {
        if (!form.Text.Contains("离线安装 / 修复")) continue;
        try {
          if (onlineBtn == null) {
            foreach (Control c in form.Controls) {
              if (c is Button && c.Text.Contains("安装 Python 与精修组件")) onlineBtn = (Button)c;
              if (c is Label) statusLbl = (Label)c;
              if (c is Button && c.Text == "取消") cancelBtn = (Button)c;
            }
            if (onlineBtn != null) {
              onlineBtn.PerformClick();
            }
          } else {
            if (statusLbl != null && statusLbl.Text.Contains("退出码 42") && statusLbl.Text.Contains("online-components-progress.jsonl")) {
              if (onlineBtn.Text.Contains("重试安装") && onlineBtn.Enabled) {
                failedStateObserved = true;
                timer.Stop();
                if (cancelBtn != null) cancelBtn.PerformClick();
                else form.Close();
              }
            }
          }
          if (ticks > 120) {
            failure = new Exception("Timeout waiting for online install exit handling");
            timer.Stop();
            form.Close();
          }
        } catch (Exception err) { failure = err; timer.Stop(); form.Close(); }
        break;
      }
    };
    timer.Start();
    Environment.SetEnvironmentVariable("AMP_TEST_INSTALLER_SCRIPT", args[1]);
    MethodInfo method = typeof(Launcher).GetMethod("ShowRepair", BindingFlags.NonPublic | BindingFlags.Static);
    bool accepted = (bool)method.Invoke(null, new object[] { args[0] });
    timer.Dispose();
    if (failure != null) { Console.Error.WriteLine(failure.Message); return 5; }
    return failedStateObserved && !accepted ? 0 : 6;
  }
}
''', encoding='utf-8')
            harness = root / 'OnlineInstallHarness.exe'
            compiled = subprocess.run([str(_framework_csc()), '/nologo', '/target:exe',
                                       '/main:OnlineInstallHarness', '/reference:System.Windows.Forms.dll',
                                       '/reference:System.Web.Extensions.dll',
                                       '/out:' + str(harness), str(ROOT / 'app/Launcher.cs'), str(harness_source)],
                                      capture_output=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            run = subprocess.run([str(harness), str(root), str(mock_fail)], capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
