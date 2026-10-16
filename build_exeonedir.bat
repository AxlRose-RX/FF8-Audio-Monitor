@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem --- Set console color to bright cyan on black ---
color 0B

where py >nul 2>nul
if errorlevel 1 (
  color 0C
  echo Python launcher ^(py.exe^) not found.
  pause
  exit /b 1
)

rem --- Check the source files are all here ---
rem PyInstaller bundles blitzball_window.py and theme.py on its own (the main .py
rem imports them), but if one is missing it still builds and the .exe won't start.
echo ========================================
echo Checking source files...
echo ========================================
set "MISSING=0"
for %%F in ("echos_audio_monitor.py" "blitzball_window.py" "theme.py" "icon.ico") do (
  if exist "%%~F" (
    echo   [OK] %%~F
  ) else (
    echo   [MISSING] %%~F
    set "MISSING=1"
  )
)
if "%MISSING%"=="1" (
  color 0C
  echo.
  echo ========================================
  echo Build stopped: put the missing files
  echo next to this .bat and run it again.
  echo ========================================
  pause
  exit /b 1
)
echo.

rem --- Clean up previous build artifacts ---
echo ========================================
echo Cleaning up previous build artifacts...
echo ========================================

if exist "__pycache__" (
  echo   Removing __pycache__ folder...
  rmdir /s /q "__pycache__" 2>nul
)

if exist "build" (
  echo   Removing build folder...
  rmdir /s /q "build" 2>nul
)

if exist "dist" (
  echo   Removing dist folder...
  rmdir /s /q "dist" 2>nul
)

if exist "echos_audio_monitor.spec" (
  echo   Removing echos_audio_monitor.spec...
  del /f /q "echos_audio_monitor.spec" 2>nul
)

echo   Cleanup complete!
echo.

rem --- deps ---
echo ========================================
echo Installing/updating dependencies...
echo ========================================
py -3 -m pip install --upgrade pip pyinstaller tkinterdnd2 pillow numpy psutil 

set "MAIN_PY=echos_audio_monitor.py"
set "DIST_DIR=dist\echos_audio_monitor"

rem --- build ---
echo.
echo ========================================
echo Starting PyInstaller build...
echo ========================================
py -3 -m PyInstaller --noconfirm --clean --onedir --icon=icon.ico --name echos_audio_monitor ^
  --hidden-import psutil ^
  --hidden-import tkinter  ^
  --collect-all tkinterdnd2 ^
  --exclude-module pytest ^
  --exclude-module numpy.tests ^
  --exclude-module numpy.testing ^
  --exclude-module numpy.f2py.tests ^
  --exclude-module numpy.distutils.tests ^
  --windowed ^
  "%MAIN_PY%"

if errorlevel 1 (
  color 0C
  echo.
  echo ========================================
  echo Build failed!
  echo ========================================
  pause
  exit /b 1
)

if not exist "%DIST_DIR%\_internal" mkdir "%DIST_DIR%\_internal"

echo.
echo ========================================
echo Copying additional files into _internal...
echo ========================================
for %%F in ("icon.ico") do (
  if exist "%%~fF" (
    copy /Y "%%~fF" "%DIST_DIR%\_internal\" >nul
    echo   [OK] Copied to _internal: %%~F
  ) else (
    echo   [WARNING] %%~F not found, skipping...
  )
)

echo.
echo Built to "%CD%\%DIST_DIR%\"

rem --- Create zip archive ---
echo.
echo ========================================
echo Creating zip archive...
echo ========================================
cd "%DIST_DIR%\.."
if exist "echos_audio_monitor.zip" del "echos_audio_monitor.zip"
powershell -Command "Compress-Archive -Path 'echos_audio_monitor\*' -DestinationPath 'echos_audio_monitor.zip' -CompressionLevel Optimal"
cd /d "%~dp0"

rem --- Success! Change to green ---
color 0A
echo.
echo ========================================
echo            BUILD COMPLETE!
echo ========================================
echo.
echo Executable: 
echo   %CD%\%DIST_DIR%\echos_audio_monitor.exe
echo.
echo Zip archive: 
echo   %CD%\dist\echos_audio_monitor.zip
echo.
echo ========================================
echo.

pause
color