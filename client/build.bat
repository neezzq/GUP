@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" goto dependencies
where py >nul 2>nul
if errorlevel 1 (
    python -m venv .venv
) else (
    py -3.12 -m venv .venv
)
if errorlevel 1 exit /b 1

:dependencies
set "GUP_PYTHON=.venv\Scripts\python.exe"
"%GUP_PYTHON%" -c "import sys, struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8" || exit /b 1
"%GUP_PYTHON%" -m pip install -r requirements-dev.txt || exit /b 1
"%GUP_PYTHON%" -m pytest -q || exit /b 1
"%GUP_PYTHON%" -m PyInstaller --clean --noconfirm --onefile --windowed --name GameUpdater --paths . --collect-all customtkinter --hidden-import pywinauto.controls.uia_controls --hidden-import keyring.backends.Windows --hidden-import win32timezone --add-data "THIRD_PARTY.md:." --copy-metadata customtkinter --copy-metadata pywinauto --copy-metadata pywin32 --copy-metadata psutil --copy-metadata httpx --copy-metadata keyring --copy-metadata Pillow --copy-metadata vdf client\main.py || exit /b 1
"%GUP_PYTHON%" -c "import hashlib, pathlib; f=pathlib.Path('dist/GameUpdater.exe'); digest=hashlib.file_digest(f.open('rb'), 'sha256').hexdigest(); f.with_suffix('.exe.sha256').write_text(digest+'  GameUpdater.exe'+chr(10), encoding='ascii')" || exit /b 1
endlocal
