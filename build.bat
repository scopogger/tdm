@echo off
echo Building ambient.exe ...

pyinstaller ^
  --onefile ^
  --windowed ^
  --icon=ambient.ico ^
  --add-data "ambient.ico;." ^
  --name ambient ^
  ambient_player.py

echo.
echo Done! Your exe is in the dist\ folder.
pause
