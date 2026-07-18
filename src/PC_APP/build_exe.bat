@echo off
REM =====================================================================
REM  Build NEU_CABLE_JIG_TEST.exe  (chay tren Windows)
REM  Double-click file nay, hoac chay trong cmd.
REM =====================================================================
cd /d "%~dp0"

echo.
echo [1/3] Installing dependencies (pyinstaller, pyserial, openpyxl)...
python -m pip install --upgrade pyinstaller pyserial openpyxl
if errorlevel 1 goto :err

echo.
echo [2/3] Cleaning old build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo.
echo [3/3] Building EXE from NEU_CABLE_JIG_TEST.spec ...
pyinstaller --noconfirm NEU_CABLE_JIG_TEST.spec
if errorlevel 1 goto :err

echo.
echo =====================================================================
echo  DONE. EXE created at:
echo    %~dp0dist\NEU_CABLE_JIG_TEST.exe
echo =====================================================================
pause
exit /b 0

:err
echo.
echo *** BUILD FAILED - xem thong bao loi phia tren ***
pause
exit /b 1
