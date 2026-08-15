#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
U="$(whoami)"
N=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
echo "Instalando $N servico(s) systemd (dir=$DIR user=$U)"
for i in $(seq 0 $((N-1))); do
  sudo tee /etc/systemd/system/vigia$i.service >/dev/null <<EOF
[Unit]
Description=Corexia Vigia GPU$i
After=network-online.target
[Service]
WorkingDirectory=$DIR
Environment=CUDA_VISIBLE_DEVICES=$i
ExecStart=$DIR/venv/bin/python $DIR/detector.py $i
Restart=always
RestartSec=5
User=$U
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl enable vigia$i
  sudo systemctl restart vigia$i
  echo "vigia$i ativo."
done
echo "Logs:  journalctl -u vigia0 -f"