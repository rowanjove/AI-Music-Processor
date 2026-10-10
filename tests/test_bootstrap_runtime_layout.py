"""Exercise the actual PowerShell layout and rollback with a CPython host."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.name=='nt','Windows bootstrap')
class BootstrapLayoutTests(unittest.TestCase):
    def test_fresh_runtime_survives_move_and_failed_swap_restores_old_directory(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT/'temp'),prefix='bootstrap-layout-') as folder:
            base=Path(folder);script=base/'layout-test.ps1'
            quote=lambda value:"'"+str(value).replace("'","''")+"'"
            script.write_text("""$ErrorActionPreference='Stop'
$source=%s
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'PowerShell parse failed' }
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Test-TkPython','Assert-RuntimeMutationPath','Ensure-RuntimeLayout')},$true)
foreach($f in $functions) { Invoke-Expression $f.Extent.Text }
$Root=%s
New-Item -ItemType Directory -Path $Root | Out-Null
$runtime=Ensure-RuntimeLayout -Python %s
if (-not (Test-TkPython $runtime)) { throw 'fresh runtime failed' }
$moved=$Root+' moved'
Microsoft.PowerShell.Management\\Move-Item -LiteralPath $Root -Destination $moved
$relocated=Join-Path $moved 'tools\\RVC\\RVC-beta-v2-0618\\runtime\\python.exe'
if (-not (Test-TkPython $relocated)) { throw 'relocated runtime failed' }
$origin=& $relocated -B -c "import pathlib,tkinter; print(pathlib.Path(tkinter.__file__).resolve())"
if (-not $origin.StartsWith($moved,[StringComparison]::OrdinalIgnoreCase)) { throw 'external tkinter' }
$Root=%s
$old=Join-Path $Root 'tools\\RVC\\RVC-beta-v2-0618\\runtime'
New-Item -ItemType Directory -Force -Path $old | Out-Null
Set-Content -LiteralPath (Join-Path $old 'sentinel.txt') -Value 'preserve old'
function Move-Item {
    [CmdletBinding()]param([string]$LiteralPath,[string]$Destination)
    if ((Split-Path -Leaf $LiteralPath).StartsWith('.runtime-stage-')) { throw 'injected swap failure' }
    Microsoft.PowerShell.Management\\Move-Item @PSBoundParameters
}
$failed=$false
try { Ensure-RuntimeLayout -Python %s | Out-Null } catch { $failed=$true }
if (-not $failed -or -not (Test-Path -LiteralPath (Join-Path $old 'sentinel.txt'))) { throw 'rollback failed' }
@{status='PASS';relocation=$true;rollback=$true} | ConvertTo-Json -Compress
"""%(quote(ROOT/'scripts/install_components.ps1'),quote(base/'fresh'),quote(sys.executable),quote(base/'rollback'),quote(sys.executable)),encoding='utf-8-sig')
            result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])['status'],'PASS')
