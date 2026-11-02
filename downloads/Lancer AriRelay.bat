@echo off
title AriRelay
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 AriRelay.py %*
  goto fin
)
where python >nul 2>nul
if %errorlevel%==0 (
  python AriRelay.py %*
  goto fin
)
echo Python n'est pas installe sur ce VPS.
echo Installe-le depuis https://www.python.org/downloads/windows/ puis relance ce fichier.
:fin
pause
