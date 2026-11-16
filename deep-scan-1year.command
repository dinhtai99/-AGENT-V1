#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "================================================================"
echo "  🚀 AGENT-V1 — QUÉT SÂU 1 NĂM CÁC NHÓM MỚI KHÁM PHÁ (>5K MEM)"
echo "================================================================"
TASK_FILE="task-baito-nhom-da-tham-gia.json"
if [[ -f "task-scan-discovered.json" ]]; then
  TASK_FILE="task-scan-discovered.json"
fi
PY_BIN="python3"
if [[ -x "$HOME/.venv-agent/bin/python" ]]; then
  PY_BIN="$HOME/.venv-agent/bin/python"
elif [[ -x .venv/bin/python ]]; then
  PY_BIN=".venv/bin/python"
fi
"$PY_BIN" browser_worker.py --task "$TASK_FILE" --deep --out result.json
"$PY_BIN" dashboard.py
"$PY_BIN" deploy_github.py
