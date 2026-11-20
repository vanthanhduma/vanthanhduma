@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Goc Review Phim Video Studio v1.5.8.0 - Cai dat Qt
for %%I in ("%~dp0.") do set "ROOT=%%~fI"
set "LOG=%TEMP%\GocReview_QT_INSTALL_LOG.txt"
set "LOCALLOG=%ROOT%\CAI_DAT_LOG.txt"
set "PY_CMD="
set "PY_ARGS="
set "WSCRIPT=%SystemRoot%\System32\wscript.exe"
>"%LOG%" echo ===== GOC REVIEW PHIM QT INSTALL LOG =====
>>"%LOG%" echo Root: %ROOT%
>>"%LOG%" echo Date: %DATE% %TIME%

echo.
echo =====================================================
echo      GOC REVIEW PHIM VIDEO STUDIO v1.5.8.0
echo                 QT UI PILOT
echo =====================================================
echo.
echo Thu muc: "%ROOT%"
echo Log: "%LOG%"
echo.
if not exist "%ROOT%\qt_app.py" goto bad_root
if not exist "%ROOT%\engine_worker.py" goto bad_root
if not exist "%ROOT%\requirements.txt" goto bad_root
if not exist "%ROOT%\MO_GOC_REVIEW_PHIM.vbs" goto bad_root
if not exist "%WSCRIPT%" goto no_wscript
pushd "%ROOT%" >>"%LOG%" 2>&1
if errorlevel 1 goto path_error

echo [1/5] Kiem tra Python 3.11+ va Tkinter (processing engine)...
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >>"%LOG%" 2>&1
  if not errorlevel 1 (set "PY_CMD=py" & set "PY_ARGS=-3")
)
if not defined PY_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >>"%LOG%" 2>&1
    if not errorlevel 1 set "PY_CMD=python"
  )
)
if not defined PY_CMD goto no_python
%PY_CMD% %PY_ARGS% -c "import tkinter; print('Tkinter OK')" >>"%LOG%" 2>&1
if errorlevel 1 goto no_tk

echo [2/5] Tao moi truong Python rieng...
if not exist "%ROOT%\.venv\Scripts\python.exe" (
  %PY_CMD% %PY_ARGS% -m venv "%ROOT%\.venv" >>"%LOG%" 2>&1
  if errorlevel 1 goto failed
)
if not exist "%ROOT%\.venv\Scripts\python.exe" goto venv_missing

echo [3/5] Cai thu vien xu ly video, Qt va yt-dlp...
echo Luu y: PySide6 Essentials va yt-dlp can Internet trong lan cai dau.
"%ROOT%\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip >>"%LOG%" 2>&1
if errorlevel 1 goto failed
"%ROOT%\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%ROOT%\requirements.txt" >>"%LOG%" 2>&1
if errorlevel 1 goto failed

echo [4/5] Kiem tra dependency...
"%ROOT%\.venv\Scripts\python.exe" -c "import cv2,numpy,PIL,imageio_ffmpeg,shiboken6,yt_dlp,pytesseract,requests,cryptography; from PySide6.QtWidgets import QApplication; print('Dependencies OK', cv2.__version__)" >>"%LOG%" 2>&1
if errorlevel 1 goto failed

echo [5/5] Kiem tra Subscribe OCR va khoi dong Video Studio Qt...
set "TESSERACT_FOUND="
where tesseract >nul 2>nul && set "TESSERACT_FOUND=1"
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESSERACT_FOUND=1"
if exist "%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe" set "TESSERACT_FOUND=1"
if not defined TESSERACT_FOUND echo LUU Y: Subscribe Detector can Tesseract OCR. Neu chua cai, chay CAI_TESSERACT_OCR.bat sau khi cai dat.
"%WSCRIPT%" //B //NoLogo "%ROOT%\MO_GOC_REVIEW_PHIM.vbs" >>"%LOG%" 2>&1
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" goto app_failed
echo Dang mo giao dien bang Python khong console... >>"%LOG%"
"%WSCRIPT%" //B //NoLogo "%ROOT%\TAO_LOI_TAT_DESKTOP.vbs" >>"%LOG%" 2>&1
if errorlevel 1 echo LUU Y: Khong tao duoc shortcut Desktop. Ban van co the mo MO_GOC_REVIEW_PHIM.vbs.
goto success

:no_python
echo KHONG TIM THAY PYTHON 3.11 TRO LEN.
echo Cai Python tu https://www.python.org/downloads/windows/ va chon Add Python to PATH.
>>"%LOG%" echo ERROR: Python 3.11+ not found.
goto show_error
:no_wscript
echo Windows Script Host khong kha dung.
>>"%LOG%" echo ERROR: Windows Script Host not found: %WSCRIPT%
goto show_error
:no_tk
echo Python tim thay nhung Tkinter khong hoat dong; engine cu can Tkinter.
>>"%LOG%" echo ERROR: Tkinter import failed.
goto show_error
:venv_missing
echo Khong tao duoc moi truong .venv.
>>"%LOG%" echo ERROR: venv python.exe missing.
goto show_error
:path_error
echo Windows khong mo duoc thu muc tool: "%ROOT%"
>>"%LOG%" echo ERROR: pushd failed.
goto show_error
:bad_root
echo Khong tim thay qt_app.py, engine_worker.py hoac requirements.txt.
echo Hay giai nen day du ZIP vao mot thu muc.
>>"%LOG%" echo ERROR: required files missing.
goto show_error
:app_failed
echo Qt Video Studio gap loi khi khoi dong. Exit code: %APP_EXIT%
goto show_error
:failed
echo Cai dat that bai. Chi tiet nam trong log.
goto show_error
:show_error
echo.
echo -------- 80 DONG CUOI CUA LOG --------
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 80" 2>nul
copy /y "%LOG%" "%LOCALLOG%" >nul 2>nul
echo --------------------------------------
echo Log day du: "%LOG%"
echo Neu can ho tro, gui file CAI_DAT_LOG.txt.
goto finish
:success
copy /y "%LOG%" "%LOCALLOG%" >nul 2>nul
echo Cai dat xong. Loi tat Desktop da duoc tao/cap nhat.
echo Tu lan sau mo bang loi tat Goc Review Phim, khong can mo Terminal.
:finish
echo.
echo Nhan phim bat ky de dong cua so nay...
pause >nul
popd >nul 2>nul
exit /b
