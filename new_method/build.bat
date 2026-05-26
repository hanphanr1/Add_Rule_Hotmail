@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo  Building RuleCreator.exe
echo ========================================

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] install requirements failed
    pause
    exit /b 1
)

pyinstaller --noconfirm --onefile --windowed --clean --name "RuleCreator" rule_creator_gui.py
if errorlevel 1 (
    echo [ERROR] build failed
    pause
    exit /b 1
)

echo.
echo DONE: dist\RuleCreator.exe
pause
