@echo off
title AI Music Processor 1.0 一体化音乐工作台
cd /d "%~dp0"
if exist "AI-Music-Processor.exe" (
  start "" "AI-Music-Processor.exe" %*
  exit /b 0
)
if exist "PORTABLE_RELEASE" goto release_missing
if exist "release-manifest.json" goto release_missing
if not exist "tools\RVC\RVC-beta-v2-0618\runtime\pythonw.exe" (
  echo Bundled Python is missing. Restore the full offline application directory.
  exit /b 2
)
start "" "tools\RVC\RVC-beta-v2-0618\runtime\pythonw.exe" AI_Music_Processor.pyw
exit /b 0
:release_missing
echo Portable release is missing AI-Music-Processor.exe; refusing to bypass runtime verification.
exit /b 2
