#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "================================================================"
echo "  🚀 AGENT-V1 — QUÉT SÂU 1 NĂM (TOP 10 NGÀNH NGHỀ BÀI ĐĂNG CŨ)"
echo "  Tìm kiếm từ khóa 10 ngành nghề trên toàn bộ các nhóm Facebook"
echo "  Tự động gom khách hàng trong 1 năm qua và cộng dồn vào danh sách."
echo "================================================================"
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  exec "$HOME/.venv-agent/bin/python" browser_worker.py --deep
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python browser_worker.py --deep
fi
exec python3 browser_worker.py --deep

