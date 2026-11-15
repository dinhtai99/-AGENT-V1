#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "================================================================"
echo "  🚀 ĐĂNG NHẬP FACEBOOK CHO TOOL AGENT-V1"
echo "  Trình duyệt sẽ mở ra, bạn đăng nhập xong rồi quay lại Terminal nhấn Enter."
echo "================================================================"
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  exec "$HOME/.venv-agent/bin/python" browser_worker.py --login
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python browser_worker.py --login
fi
exec python3 browser_worker.py --login

