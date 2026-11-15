#!/bin/zsh
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  exec "$HOME/.venv-agent/bin/python" -B share_ngrok.py
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -B share_ngrok.py
fi
exec python3 -B share_ngrok.py

