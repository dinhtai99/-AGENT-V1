#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "================================================================"
echo "  🔄 ĐỒNG BỘ CODE & DỮ LIỆU KHÁCH HÀNG (MAC <-> WINDOWS)"
echo "================================================================"
echo "[1/3] Kéo code mới nhất từ nhánh main..."
git pull --rebase origin main
echo ""
echo "[2/3] Gộp dữ liệu khách hàng (leads.json, CRM, seen.json) và cập nhật GitHub Pages..."
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  "$HOME/.venv-agent/bin/python" deploy_github.py
elif [[ -x .venv/bin/python ]]; then
  .venv/bin/python deploy_github.py
else
  python3 deploy_github.py
fi
echo ""
echo "[3/3] Hoàn tất đồng bộ! Nhấn Enter để đóng."
read
