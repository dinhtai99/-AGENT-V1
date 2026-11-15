#!/bin/zsh
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  exec "$HOME/.venv-agent/bin/python" -B share_server.py
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -B share_server.py
fi
exec python3 -B share_server.py
