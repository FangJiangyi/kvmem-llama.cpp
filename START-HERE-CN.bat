@echo off
setlocal
title KVMem (Chinese)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start-ui.ps1" -Language zh-CN %*
set "kvmem_exit=%errorlevel%"
if not "%kvmem_exit%"=="0" (
    echo.
    echo KVMem could not start. Read the message above for details.
    pause
)
exit /b %kvmem_exit%
