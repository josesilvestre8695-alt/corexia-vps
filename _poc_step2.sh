#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> aguardando sync + frames (~100s)"; sleep 100
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo ">> VRAM depois (deve subir se carregou yolov8s/ppes):"
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader | sed 's/^/   /'
echo ">> logs do nvdec (carga on-demand / erros de inferencia):"
echo 'tvlantvlan' | sudo -S journalctl -u vigia_nvdec@0 -u vigia_nvdec@1 --since "4 min ago" --no-pager 2>/dev/null | grep -iE "on-demand|carregado|nvdec:infer|Traceback|Error|detect]" | tail -30
echo "(fim)"
