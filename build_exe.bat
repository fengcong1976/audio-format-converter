rem Build the portable app folder: dist\<app name>\<app name>.exe
rem Requires: pip install -r requirements.txt pyinstaller
rem Requires the ffmpeg runtime in ffmpeg\bin\ (ffmpeg.exe + ffprobe.exe + DLLs).
rem Uses build.spec so the exclude list / size trimming stays in one place.
rem NOTE: this file is intentionally ASCII-only. cmd.exe reads .bat as GBK,
rem so Chinese path names here would turn into mojibake and break the build.
setlocal
cd /d "%~dp0"

rem Interpreter order: %PYTHON% -> project venv -> py launcher -> PATH python.
rem We test the imports first because "python" on Windows may be the Store stub.
set "PY="
if defined PYTHON (
  "%PYTHON%" -c "import PyQt6, PyInstaller" >nul 2>nul && set "PY=%PYTHON%"
)
if not defined PY (
  if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import PyQt6, PyInstaller" >nul 2>nul && set "PY=venv\Scripts\python.exe"
  )
)
if not defined PY (
  py -3 -c "import PyQt6, PyInstaller" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import PyQt6, PyInstaller" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [FAILED] No Python with PyQt6 + PyInstaller found.
  echo Install them with:  pip install -r requirements.txt pyinstaller
  exit /b 1
)

if not exist "ffmpeg\bin\ffmpeg.exe" (
  echo [FAILED] ffmpeg\bin\ffmpeg.exe not found.
  echo Copy the shared ffmpeg build ^(ffmpeg.exe, ffprobe.exe, *.dll^) into ffmpeg\bin\ first.
  exit /b 1
)

%PY% -m PyInstaller --noconfirm --clean "build.spec"
if errorlevel 1 (
  echo.
  echo [FAILED] PyInstaller build failed, see the log above.
  exit /b 1
)

echo.
echo [OK] PyInstaller build finished, see the dist folder.
endlocal
