@echo off
title RVC ªª»À…˘ WebUI
cd /d "%~dp0RVC\RVC-beta-v2-0618"
runtime\python.exe infer-web.py --pycmd runtime\python.exe --port 7865
pause
