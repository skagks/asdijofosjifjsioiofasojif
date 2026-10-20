@echo off
chcp 65001 >nul
rem Windows용 단일 실행 파일(dist\MHWildsModManager.exe)을 만듭니다.
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller -r requirements.txt || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name MHWildsModManager ^
    --icon assets\icon.ico ^
    --collect-all tkinterdnd2 ^
    --collect-submodules zstandard ^
    --add-data "mhwmm\matfix\rsz_types.json.xz;mhwmm\matfix" ^
    --add-data "mhwmm\equip\equip_paths.txt.xz;mhwmm\equip" ^
    MHWildsModManager.pyw || goto :error
echo.
echo 완료: dist\MHWildsModManager.exe
pause
exit /b 0
:error
echo 빌드에 실패했습니다.
pause
exit /b 1
