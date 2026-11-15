#!/bin/bash
cd "$(dirname "$0")"
python3 deploy_github.py
echo ""
echo "Nhấn phím Enter để đóng cửa sổ này."
read

