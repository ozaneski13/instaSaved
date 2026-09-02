@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" ( echo .venv bulunamadi. Once kurulum: uv venv --python 3.10 .venv ^&^& uv pip install --python .venv\Scripts\python.exe -r requirements.txt & pause & exit /b 1 )
if "%~1"=="" goto menu
"%PY%" -m igsaved %*
set RC=%ERRORLEVEL%
echo %cmdcmdline% | findstr /i /c:"/c" >nul && ( echo. & echo [Bitti, cikis kodu %RC%] & pause )
exit /b %RC%

:menu
cls
echo ==============================================
echo  instaSaved - Instagram kaydedilenler dokumu
echo ==============================================
echo  1 - Instagram girisi (mobil API, bir kez)   ^<- ilk adim
echo  2 - Koleksiyonlari listele
echo  3 - Deneme kosusu, 3 post (run --limit 3)
echo  4 - Tam kosu (run)
echo  5 - Belirli koleksiyonda kosu
echo  6 - Tum postlari yeniden analiz et (process --redo + report)
echo  7 - Durum (status)
echo  8 - Sadece rapor (report)
echo  9 - Chrome ile giris (alternatif kaynak: source=browser)
echo  0 - Cikis
echo.
set "SECIM="
set /p "SECIM=Secim: "
if "%SECIM%"=="" exit /b 0
if "%SECIM%"=="0" exit /b 0
if "%SECIM%"=="1" "%PY%" -m igsaved ig-login
if "%SECIM%"=="2" "%PY%" -m igsaved collections
if "%SECIM%"=="3" "%PY%" -m igsaved run --limit 3
if "%SECIM%"=="4" "%PY%" -m igsaved run
if "%SECIM%"=="5" ( set /p "COLS=Koleksiyon adi: " ) & if "%SECIM%"=="5" "%PY%" -m igsaved run --collection "%COLS%"
if "%SECIM%"=="6" "%PY%" -m igsaved process --redo & if "%SECIM%"=="6" "%PY%" -m igsaved report
if "%SECIM%"=="7" "%PY%" -m igsaved status
if "%SECIM%"=="8" "%PY%" -m igsaved report
if "%SECIM%"=="9" "%PY%" -m igsaved login
echo.
echo [Bitti, cikis kodu %ERRORLEVEL%] - devam icin bir tusa bas
pause >nul
goto menu
