@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   🚀 BẮT ĐẦU QUÉT SÂU 1 NĂM CÁC NHÓM MỚI KHÁM PHÁ (>5K MEM)
echo ============================================================
if exist "task-scan-discovered.json" (
  python browser_worker.py --task task-scan-discovered.json --deep --out result.json
) else (
  python browser_worker.py --task task-baito-nhom-da-tham-gia.json --deep --out result.json
)
python dashboard.py
python deploy_github.py
pause
