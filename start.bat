
@echo off
chcp 65001 >nul
cd /d "%~dp0"

title IP-TOOLS


python Tools-baner.py
if errorlevel 1 (
    echo.
    echo Tools-baner.py でエラーが発生しました。
    pause
    exit /b 1
)

echo.

python local_diagnostic.py --tunnel
if errorlevel 1 (
    echo.
    echo local_diagnostic.py でエラーが発生しました。
    pause
    exit /b 1
)

echo.
echo すべての処理が終了しました。
pause
```