#!/bin/zsh
cd "$(dirname "$0")" || exit 1
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  exec "$HOME/.venv-agent/bin/python" server.py --open
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python server.py --open
fi
exec python3 server.py --open
