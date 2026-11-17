#!/bin/bash
cd "$(dirname "$0")"
echo "================================================================="
echo "  🤝 AGENT-V1 — HỢP NHẤT DỮ LIỆU TỪ MÁY PC VÀO MÁY MAC"
echo "================================================================="
python3 merge_leads.py leads-pc.json
echo ""
echo "Nhấn phím bất kỳ để đóng cửa sổ..."
read -n 1
