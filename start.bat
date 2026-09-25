@echo off
chcp 65001 >nul
rem Feige AI Usage Dashboard: regenerate + open browser (exits when done)
rem Add --refresh to force re-query provider quotas
rem Console-session quotas (ali/glm): silent no-op without sessions
py "%~dp0scripts\console_quota.py" --fetch all --quiet
py "%~dp0scripts\usage_dashboard.py" %*
if errorlevel 1 pause
