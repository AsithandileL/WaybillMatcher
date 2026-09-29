@echo off
REM Builds a single-file WaybillMatcher.exe using PyInstaller.
REM Run this on a Windows machine with Python installed, from inside this folder.

pip install -r requirements.txt

pyinstaller --noconfirm --onefile --windowed ^
    --name WaybillMatcher ^
    --distpath . ^
    --hidden-import=win32timezone ^
    src\main.py

echo.
echo Build complete: WaybillMatcher.exe is in this folder.
echo Remember to copy config.json next to WaybillMatcher.exe before running it.
pause
