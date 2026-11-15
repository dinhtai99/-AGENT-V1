#!/bin/zsh
cd "$(dirname "$0")" || exit 1
echo "================================================================"
echo "  🛑 DỪNG MÁY CHỦ VÀ TIẾN TRÌNH QUÉT NGẦM AGENT-V1"
echo "================================================================"
# Kill any process listening on port 8080 or running server.py
pids=$(lsof -ti :8080)
if [ -n "$pids" ]; then
  kill -9 $pids 2>/dev/null
  echo "✅ Đã dừng hoàn toàn máy chủ trên cổng 8080."
else
  echo "ℹ️ Máy chủ hiện không chạy."
fi
pkill -f "browser_worker.py" 2>/dev/null
echo "✅ Đã dừng tất cả các tiến trình quét ngầm."
echo "Bạn có thể đóng cửa sổ này."

