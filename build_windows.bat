@echo off
setlocal
cd /d "%~dp0"

py -m pip install --upgrade pip
py -m pip install -r requirements.txt

py -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name TransitCollector ^
  --collect-all keyring ^
  app.py

echo.
echo Build finished: dist\TransitCollector\
pause
