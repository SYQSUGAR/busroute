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
  --collect-all geopandas ^
  --collect-all pyogrio ^
  --collect-all pyproj ^
  --collect-all shapely ^
  app.py

echo.
echo Build finished: dist\TransitCollector\
pause
