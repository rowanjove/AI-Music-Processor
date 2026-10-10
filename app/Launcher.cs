using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Runtime.InteropServices;
class Launcher {
  const string StartupTitle="AI Music Processor — 正在启动";
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string className,string title);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window,int command);
  static bool FocusWindow(string title) {
    IntPtr window=FindWindow(null,title);
    if(window==IntPtr.Zero)return false;
    ShowWindow(window,9);SetForegroundWindow(window);return true;
  }
  sealed class StartupWindow : Form {
    public bool Cancelled;
    readonly Label status;
    public StartupWindow() {
      Text=StartupTitle;Width=590;Height=215;StartPosition=FormStartPosition.CenterScreen;
      FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;MinimizeBox=false;
      status=new Label {Left=20,Top=20,Width=535,Height=75,Text="正在校验运行环境。首次启动需检查模型和依赖，可能需要数分钟。\n请勿重复双击；校验通过后将自动打开工作台。"};
      Controls.Add(status);
      Controls.Add(new ProgressBar {Left=20,Top=100,Width=535,Height=18,Style=ProgressBarStyle.Marquee});
      Button cancel=new Button {Left=445,Top=135,Width=110,Text="取消启动"};
      cancel.Click+=delegate {Cancelled=true;};Controls.Add(cancel);
      FormClosing+=delegate(object sender,FormClosingEventArgs e) {if(e.CloseReason==CloseReason.UserClosing){Cancelled=true;e.Cancel=true;}};
    }
    public void SetStatus(string text) {status.Text=text;}
  }
  static string QuoteArg(string value) {
    StringBuilder quoted=new StringBuilder("\"");
    int slashes=0;
    foreach(char letter in value) {
      if(letter=='\\') { slashes++; continue; }
      quoted.Append('\\',letter=='"' ? slashes*2+1 : slashes);
      quoted.Append(letter); slashes=0;
    }
    quoted.Append('\\',slashes*2); quoted.Append('"'); return quoted.ToString();
  }

  static string HashFile(string path) {
    using(var sha=SHA256.Create()) using(var file=File.OpenRead(path))
      return BitConverter.ToString(sha.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
  }

  static string ManagedPath(string root,string relative) {
    if(string.IsNullOrEmpty(relative)||relative.Contains("\\")||relative.Contains(":")||relative.StartsWith("/"))
      throw new InvalidDataException("组件路径无效");
    foreach(string part in relative.Split('/'))
      if(part==""||part=="."||part=="..") throw new InvalidDataException("组件路径穿越");
    if(!(relative.StartsWith("core/")||relative.StartsWith("runtime/")||
         relative.StartsWith("tools/RVC/RVC-beta-v2-0618/")||relative.StartsWith("tools/UVR5/models/")||
         relative=="AI_Music_Processor.pyw"||relative=="process_music.py"||relative=="VERSION"))
      throw new InvalidDataException("不是可修复的托管组件: "+relative);
    string path=Path.GetFullPath(Path.Combine(root,relative.Replace('/',Path.DirectorySeparatorChar)));
    string boundary=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
    if(!path.StartsWith(boundary,StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("路径超出组件目录");
    string current=path;
    while(current!=null&&current.Length>=boundary.TrimEnd(Path.DirectorySeparatorChar).Length) {
      if((File.Exists(current)||Directory.Exists(current))&&
         (File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0)
        throw new InvalidDataException("修复拒绝符号链接: "+current);
      current=Path.GetDirectoryName(current);
    }
    return path;
  }

  static int RepairFromDirectory(string root,string source,Action<string> progress) {
    if(Path.GetFullPath(root).TrimEnd('\\').Equals(Path.GetFullPath(source).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase))
      throw new InvalidDataException("请选择独立的完整离线组件目录");
    string inventory=Path.Combine(root,"runtime-manifest\\inventory.json");
    if(!File.Exists(inventory)||(File.GetAttributes(inventory)&FileAttributes.ReparsePoint)!=0||
       (File.GetAttributes(Path.GetDirectoryName(inventory))&FileAttributes.ReparsePoint)!=0)
      throw new InvalidDataException("本地运行清单缺失；请重新解压完整离线包，不能猜测组件哈希");
    var serializer=new JavaScriptSerializer { MaxJsonLength=64*1024*1024 };
    var document=(Dictionary<string,object>)serializer.DeserializeObject(File.ReadAllText(inventory,Encoding.UTF8));
    var records=(object[])document["files"];
    string backupRoot=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                   "AI-Music-Processor","repair-backups",Guid.NewGuid().ToString("N"));
    int repaired=0;
    foreach(object value in records) {
      var row=(Dictionary<string,object>)value;
      string relative=(string)row["path"];
      // Unshipped development metadata and user-owned files are never repair targets.
      if(!(relative.StartsWith("core/")||relative.StartsWith("runtime/")||
           relative.StartsWith("tools/RVC/RVC-beta-v2-0618/")||relative.StartsWith("tools/UVR5/models/")||
           relative=="AI_Music_Processor.pyw"||relative=="process_music.py"||relative=="VERSION")) continue;
      string digest=(string)row["sha256"];
      if(digest.Length!=64||System.Text.RegularExpressions.Regex.IsMatch(digest,"[^0-9a-f]"))
        throw new InvalidDataException("清单SHA-256无效");
      long size=Convert.ToInt64(row["size"]);
      if(size<0) throw new InvalidDataException("清单文件大小无效");
      string target=ManagedPath(root,relative);
      if(File.Exists(target)&&new FileInfo(target).Length==size&&HashFile(target)==digest) continue;
      string donor=ManagedPath(source,relative);
      if(!File.Exists(donor)||new FileInfo(donor).Length!=size||HashFile(donor)!=digest)
        throw new InvalidDataException("离线组件缺失或与当前版本哈希不符: "+relative);
      progress("正在校验并修复 "+relative);
      Directory.CreateDirectory(Path.GetDirectoryName(target));
      string temporary=target+".repair."+Guid.NewGuid().ToString("N")+".tmp";
      try {
        File.Copy(donor,temporary,false);
        if(new FileInfo(temporary).Length!=size||HashFile(temporary)!=digest)
          throw new InvalidDataException("复制期间组件变化: "+relative);
        if(File.Exists(target)) {
          string backup=Path.Combine(backupRoot,relative.Replace('/',Path.DirectorySeparatorChar));
          Directory.CreateDirectory(Path.GetDirectoryName(backup));
          File.Replace(temporary,target,backup);
        } else File.Move(temporary,target);
        repaired++;
      } finally { if(File.Exists(temporary)) File.Delete(temporary); }
    }
    return repaired;
  }

  static bool ShowRepair(string root) {
    using(Form form=new Form()) {
      form.Text="AI Music Processor 离线安装 / 修复";
      form.Width=620; form.Height=230; form.StartPosition=FormStartPosition.CenterScreen;
      Label status=new Label { Left=20,Top=20,Width=565,Height=85,
        Text="运行组件缺失或损坏。请选择已解压的完整同版本离线包目录。\n只按当前运行清单的SHA-256修复托管文件，原文件会备份；任务与用户数据保留。\n修复后仍须通过运行环境门禁。" };
      Button repair=new Button { Left=20,Top=120,Width=250,Height=32,Text="选择完整离线组件目录并修复" };
      Button cancel=new Button { Left=285,Top=120,Width=100,Height=32,Text="取消",DialogResult=DialogResult.Cancel };
      form.Controls.Add(status); form.Controls.Add(repair); form.Controls.Add(cancel);
      string installer=Path.Combine(root,"scripts\\install_components.ps1");
      if(File.Exists(installer) && File.Exists(Path.Combine(root,"LIGHTWEIGHT_RELEASE")) &&
         !File.Exists(Path.Combine(root,"runtime-manifest\\runtime.json"))) {
        form.Height=280;
        Button online=new Button {Left=20,Top=165,Width=365,Height=32,Text="安装 Python 与精修组件（需要网络）"};
        form.Controls.Add(online);
        online.Click+=delegate {
          try {
            string shell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell\\v1.0\\powershell.exe");
            Process.Start(new ProcessStartInfo {FileName=shell,
              Arguments="-NoProfile -ExecutionPolicy Bypass -File "+QuoteArg(installer)+" -Root "+QuoteArg(root)+" -InstallPython -Components polish",
              WorkingDirectory=root,UseShellExecute=true});
            status.Text="安装器已打开，将显示下载与安装进度。完成后请关闭本窗口并重新启动。\n换声/转谱和用户声线模型可随后按需安装；不会自动下载未核验来源。";
          } catch(Exception error){status.Text="安装器未启动："+error.Message;}
        };
      }
      form.CancelButton=cancel;
      repair.Click += delegate {
        using(FolderBrowserDialog folder=new FolderBrowserDialog()) {
          folder.Description="选择完整同版本离线包（目录内包含 tools、core、runtime）";
          if(folder.ShowDialog(form)!=DialogResult.OK) return;
          string source=folder.SelectedPath;
          repair.Enabled=false; cancel.Enabled=false; form.ControlBox=false;
          Thread worker=new Thread(delegate() {
            try {
              int count=RepairFromDirectory(root,source,delegate(string text) {
                form.BeginInvoke((MethodInvoker)delegate { status.Text=text; });
              });
              if(count==0) throw new InvalidDataException("托管文件哈希完整；当前问题不是组件缺失。请查看运行门禁的实际错误，或重新解压完整离线包。");
              form.BeginInvoke((MethodInvoker)delegate {
                MessageBox.Show(form,"已校验并修复 "+count+" 个组件文件。将继续执行运行门禁。","离线修复完成");
                form.DialogResult=DialogResult.OK; form.Close();
              });
            } catch(Exception error) {
              form.BeginInvoke((MethodInvoker)delegate {
                status.Text=error.Message+"\n已成功修复的文件保留，未匹配文件未覆盖。";
                repair.Enabled=true; cancel.Enabled=true; form.ControlBox=true;
              });
            }
          });
          worker.IsBackground=true; worker.Start();
        }
      };
      return form.ShowDialog()==DialogResult.OK;
    }
  }
  static void AppendTail(StringBuilder output, object gate, string line) {
    if(line==null) return;
    lock(gate) {
      output.AppendLine(line);
      if(output.Length>12000) output.Remove(0,output.Length-12000);
    }
  }

  static void ShowFailure(string message,bool smokeTest) {
    if(smokeTest) Console.Error.WriteLine(message);
    else MessageBox.Show(message,"AI Music Processor");
  }

  static void TerminateVerifier(Process verifier) {
    try {
      using(Process killer=Process.Start(new ProcessStartInfo {
        FileName=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"taskkill.exe"),
        Arguments="/PID "+verifier.Id+" /T /F",UseShellExecute=false,CreateNoWindow=true
      })) { if(killer!=null) killer.WaitForExit(10000); }
    } catch { try { verifier.Kill(); } catch { } }
  }

  [STAThread] static int Main(string[] args) {
    bool interactive=!(args.Length>0&&(args[0]=="--smoke-test"||args[0]=="--verify-runtime"||args[0]=="--repair-from"));
    if(!interactive)return Run(args);
    bool created;
    using(Mutex instance=new Mutex(false,"Local\\AI_Music_Processor_Launcher_1_Local",out created)) {
      if(FocusWindow("AI Music Processor — 本地音乐工作台"))return 0;
      if(!created) {
        if(!FocusWindow(StartupTitle))ShowFailure("程序正在启动，请等待当前校验完成，无需重复双击。",false);
        return 0;
      }
      return Run(args);
    }
  }
  static int Run(string[] args) {
    string root=AppDomain.CurrentDomain.BaseDirectory;
    string pythonConsole=Path.Combine(root, @"tools\RVC\RVC-beta-v2-0618\runtime\python.exe");
    string python=Path.Combine(root,@"tools\RVC\RVC-beta-v2-0618\runtime\pythonw.exe");
    string app=Path.Combine(root,"AI_Music_Processor.pyw");
    string marker=Path.Combine(root,"PORTABLE_RELEASE");
    string releaseManifest=Path.Combine(root,"release-manifest.json");
    bool smokeTest=args.Length==1 && args[0]=="--smoke-test";
    bool fullVerify=args.Length>=1 && args[0]=="--verify-runtime";
    bool nonInteractive=smokeTest||fullVerify;
    bool repairTest=args.Length==2 && args[0]=="--repair-from";
    if(repairTest) {
      try { Console.WriteLine("Repaired: "+RepairFromDirectory(root,args[1],Console.WriteLine)); return 0; }
      catch(Exception ex) { Console.Error.WriteLine(ex.Message); return 8; }
    }
    if(!File.Exists(python)||!File.Exists(app)){
      if(!smokeTest&&!fullVerify&&ShowRepair(root)&&File.Exists(python)&&File.Exists(app)) {
        // Recovery cannot bypass the release verifier below.
      } else {
      ShowFailure("请保留完整目录，未找到 Python 或主程序。",nonInteractive);
      return 2;
      }
    }
    try {
      bool releaseSentinel=File.Exists(marker)||Directory.Exists(marker)||
        File.Exists(releaseManifest)||Directory.Exists(releaseManifest);
#if PORTABLE_BUILD
      releaseSentinel=true;
#endif
      if(releaseSentinel) {
        if(!File.Exists(marker)) {
          ShowFailure("Portable 启动标记缺失或不是普通文件，已拒绝绕过运行环境校验。",nonInteractive);
          return 7;
        }
        if(!File.Exists(pythonConsole)) {
          if(!smokeTest&&!fullVerify&&ShowRepair(root)&&File.Exists(pythonConsole)) {
            // Repairing the verifier prerequisite never skips the gate.
          } else {
            ShowFailure("Portable 运行时缺少控制台 Python，校验未执行，主程序未启动。",nonInteractive);
            return 3;
          }
        }
        StringBuilder output=new StringBuilder();
        object outputGate=new object();
        byte[] tokenBytes=new byte[32];
        using(RandomNumberGenerator rng=RandomNumberGenerator.Create()) { rng.GetBytes(tokenBytes); }
        string startupToken=BitConverter.ToString(tokenBytes).Replace("-","");
        bool runtimeReady=false;
        bool guiReady=false;
        string progressText=null;
        using(StartupWindow startup=nonInteractive?null:new StartupWindow())
        using(ManualResetEvent startupSignal=new ManualResetEvent(false))
        using(Process verifier=new Process()) {
          if(startup!=null){startup.Show();Application.DoEvents();}
          string[] quotedArgs=Array.ConvertAll(args,QuoteArg);
          string verifyArgs = fullVerify
            ? "-B "+QuoteArg(app)+" "+string.Join(" ", quotedArgs)
            : "-B "+QuoteArg(app)+" --verify-and-run --startup-token "+QuoteArg(startupToken)+" "+string.Join(" ",quotedArgs);
          verifier.StartInfo=new ProcessStartInfo {
            FileName=pythonConsole,
            Arguments=verifyArgs,
            WorkingDirectory=root,
            UseShellExecute=false,
            CreateNoWindow=true,
            RedirectStandardOutput=true,
            RedirectStandardError=true,
            StandardOutputEncoding=Encoding.UTF8,
            StandardErrorEncoding=Encoding.UTF8
          };
          verifier.StartInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1";
          verifier.StartInfo.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";
          verifier.OutputDataReceived += delegate(object sender,DataReceivedEventArgs eventArgs) {
            if(!nonInteractive&&eventArgs.Data=="AMP_GUI_READY:"+startupToken) {
              lock(outputGate){guiReady=true;}
              startupSignal.Set();
            }
            if(eventArgs.Data!=null&&eventArgs.Data.StartsWith("AMP_RUNTIME_PROGRESS:")) {
              lock(outputGate){progressText=eventArgs.Data.Substring("AMP_RUNTIME_PROGRESS:".Length);}
            }
            if(!fullVerify && eventArgs.Data=="AMP_RUNTIME_READY:"+startupToken) {
              lock(outputGate) { runtimeReady=true; }
              startupSignal.Set();
            }
            AppendTail(output,outputGate,eventArgs.Data);
          };
          verifier.ErrorDataReceived += delegate(object sender,DataReceivedEventArgs eventArgs) {
            AppendTail(output,outputGate,eventArgs.Data);
          };
          verifier.EnableRaisingEvents=true;
          verifier.Exited += delegate { startupSignal.Set(); };
          verifier.Start();
          verifier.BeginOutputReadLine();
          verifier.BeginErrorReadLine();
          bool signalled=false;
          if(startup==null)signalled=fullVerify?verifier.WaitForExit(930000):startupSignal.WaitOne(930000);
          else {
            Stopwatch wait=Stopwatch.StartNew();
            while(wait.ElapsedMilliseconds<930000) {
              Application.DoEvents();
              if(startup.Cancelled){TerminateVerifier(verifier);return 9;}
              string text;lock(outputGate){text=progressText;}
              if(text!=null)startup.SetStatus(text+"\n校验通过后自动打开工作台；无需重复双击。");
              if(startupSignal.WaitOne(50)){signalled=true;break;}
            }
            startup.Hide();
          }
          if(!signalled) {
            TerminateVerifier(verifier);
            ShowFailure("运行环境校验超时，主程序未启动。",nonInteractive);
            return 4;
          }
          if(!fullVerify) {
            if(verifier.HasExited) verifier.WaitForExit(); // Drain any final ready line before testing it.
            bool accepted;
            lock(outputGate) { accepted=runtimeReady||(!nonInteractive&&guiReady); }
            if(!accepted) {
              if(!verifier.HasExited) TerminateVerifier(verifier);
              string details;
              lock(outputGate) { details=output.ToString(); }
              if(!smokeTest&&ShowRepair(root)) return Run(args);
              ShowFailure("Portable 运行环境未通过校验，主程序未启动。\n\n"+details,nonInteractive);
              return 5;
            }
            // This same verified process now owns Tk and long audio jobs.
            // Keep draining its pipes; the gate timeout no longer applies.
            verifier.WaitForExit();
            if(verifier.ExitCode!=0) {
              string details;
              lock(outputGate) { details=output.ToString(); }
              ShowFailure("应用通过运行校验后异常退出。\n\n"+details,nonInteractive);
              return 6;
            }
            return 0;
          }
          verifier.WaitForExit();
          if(verifier.ExitCode!=0) {
            string details;
            lock(outputGate) { details=output.ToString(); }
            if(!smokeTest&&!fullVerify&&ShowRepair(root)) return Run(args);
            ShowFailure("Portable 运行环境校验失败，主程序未启动。\n\n"+details,nonInteractive);
            return 5;
          }
        }
        if(fullVerify) return 0;
      }
      string smoke=smokeTest ? " --smoke-test" : "";
      ProcessStartInfo startInfo=new ProcessStartInfo {
        FileName=python,
        Arguments="-B \""+app+"\""+smoke,
        WorkingDirectory=root,
        UseShellExecute=false,
        CreateNoWindow=true
      };
      startInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"]="1";
      startInfo.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";
      Process.Start(startInfo);
      return 0;
    } catch(Exception ex) {
      ShowFailure(ex.Message,nonInteractive);
      return 6;
    }
  }
}

