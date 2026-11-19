@echo off
rem 本檔以 Big5（cp950）編碼、CRLF 換行儲存，請勿改成 UTF-8，否則中文訊息與指令會被讀錯。
cd /d "%~dp0"
title 預算科目流用最佳解工具
set "PY="
set "INSTALLED="

:find_python
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto run
python --version >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto run
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto run
goto no_python

:run
%PY% "src\start_flow.py"
if errorlevel 3 if not errorlevel 4 goto no_python
goto finish

:no_python
if defined INSTALLED goto finish
echo.
echo 這台電腦沒有可用的 Python（需要 3.11 以上），本工具無法執行。
choice /c YN /n /m "要自動安裝 Python 3.13 嗎？（需要網路，約數分鐘）[Y/N] "
if errorlevel 2 goto finish
where winget >nul 2>&1
if errorlevel 1 goto no_winget
echo 正在安裝 Python 3.13，請稍候……
winget install --id Python.Python.3.13 --exact --source winget --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_failed
set "INSTALLED=1"
if not exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" goto restart_needed
set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
echo Python 安裝完成，繼續進行首次設定……
echo.
goto run

:restart_needed
echo Python 安裝完成。請關閉此視窗，再重新雙擊「啟動流用計算.bat」。
goto finish

:no_winget
echo 找不到 winget，無法自動安裝。請到 Python 官方網站下載安裝 Python 3.13：
echo https://www.python.org/downloads/windows/
echo 安裝完成後，再重新雙擊「啟動流用計算.bat」。
goto finish

:install_failed
echo Python 安裝未完成，請查看上方訊息；也可以到 Python 官方網站手動安裝：
echo https://www.python.org/downloads/windows/
goto finish

:finish
echo.
echo 請按任意鍵關閉視窗……
pause >nul
