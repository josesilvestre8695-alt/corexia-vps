#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "== Corexia Vision AI - setup (Ubuntu + GPU) =="
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "!! nvidia-smi nao encontrado. Instale o driver e reinicie:"
  echo "   sudo ubuntu-drivers autoinstall && sudo reboot"
  exit 1
fi
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip ffmpeg
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements-gpu.txt
echo "-- Providers do onnxruntime (precisa listar CUDAExecutionProvider):"
./venv/bin/python -c "import onnxruntime as o; print(o.get_available_providers())"
echo "== Setup ok. Teste 1 camera:  CUDA_VISIBLE_DEVICES=0 ./venv/bin/python detector.py 0 =="