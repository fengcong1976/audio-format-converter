rem Launch the GUI from source.
rem NOTE: ASCII-only on purpose. cmd.exe reads .bat as GBK, so Chinese text
rem left in this file would turn into mojibake and break the script.
rem
rem Interpreter search order:
rem   1. %PYTHON%  (set it manually if your Python lives somewhere unusual:)
rem        set PYTHON=D:\some\path\python.exe
rem   2. venv\Scripts\python.exe
rem   3. py launcher
rem   4. python on PATH
rem   5. common install folders (%LOCALAPPDATA%, %ProgramFiles%, C:\)
rem Every candidate is tested with "import PyQt6" first, because on Windows
rem "python" is often the Microsoft Store stub that does nothing useful.
setlocal
cd /d "%~dp0"

set "PY="

if defined PYTHON (
  "%PYTHON%" -c "import PyQt6" >nul 2>nul && set "PY=%PYTHON%"
)

if not defined PY (
  if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import PyQt6" >nul 2>nul && set "PY=venv\Scripts\python.exe"
  )
)

if not defined PY (
  py -3 -c "import PyQt6" >nul 2>nul && set "PY=py -3"
)

if not defined PY (
  python -c "import PyQt6" >nul 2>nul && set "PY=python"
)

if not defined PY (
  for %%R in ("%LOCALAPPDATA%\Programs\Python" "%ProgramFiles%\Python" "%SystemDrive%\Python") do (
    for /d %%D in ("%%~R\Python3*") do (
      if not defined PY if exist "%%D\python.exe" (
        "%%D\python.exe" -c "import PyQt6" >nul 2>nul && set "PY=%%D\python.exe"
      )
    )
  )
)

if not defined PY (
  echo [FAILED] No Python with PyQt6 found.
  echo   1. Install Python 3.10+ and run:  pip install -r requirements.txt
  echo   2. Or create a venv:              python -m venv venv
  echo   3. Or point this script at it:    set PYTHON=D:\path\to\python.exe
  pause
  exit /b 1
)

%PY% -B main.py
if errorlevel 1 (
  echo.
  echo [FAILED] The app exited with an error, see the message above.
  pause
)
endlocal
