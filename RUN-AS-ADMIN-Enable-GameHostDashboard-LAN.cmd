@echo off
set SCRIPT=%~dp0Enable-GameHostDashboard-LAN-Admin.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File ""%SCRIPT%""'"
