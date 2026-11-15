@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   🚀 BẮT ĐẦU QUÉT SÂU 1 NĂM TOÀN BỘ CÁC NHÓM MỤC TIÊU
echo ============================================================
python browser_worker.py --task task-baito-nhom-da-tham-gia.json --deep --out result.json
pause
