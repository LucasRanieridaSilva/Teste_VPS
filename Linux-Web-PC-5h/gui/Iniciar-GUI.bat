@echo off
REM Central de Controle do Linux Web PC - lance pelo pen drive (Windows).
REM Requer apenas o Python 3 instalado (python.org). Nenhuma biblioteca extra.
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 nao encontrado. Instale em https://www.python.org/downloads/windows/
  echo ou coloque um Python portavel (WinPython) na raiz do pen drive.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8787
python control_center.py
pause
