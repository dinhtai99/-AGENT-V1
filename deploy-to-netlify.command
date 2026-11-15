#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "🚀 Đang tự động nén và đẩy bản mới nhất lên Netlify..."
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  "$HOME/.venv-agent/bin/python" deploy_netlify.py
elif [[ -x .venv/bin/python ]]; then
  .venv/bin/python deploy_netlify.py
else
  python3 deploy_netlify.py
fi
echo ""
echo "Nhấn Enter để đóng cửa sổ này."
read

