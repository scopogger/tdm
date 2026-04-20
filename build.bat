@echo off
echo Activating venv...
call "%~dp0venv\Scripts\activate.bat"

echo Building ambient.exe ...
pyinstaller ^
  --onefile ^
  --windowed ^
  --icon="%~dp0ambient.ico" ^
  --add-data "%~dp0ambient.ico;." ^
  --name ambient ^
  "%~dp0ambient_player.py"

echo.
echo Done! Your exe is in the dist\ folder.
pause
