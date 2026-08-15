#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> aguardando sync + inferencia (~150s)"; sleep 150
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo ">> VRAM (deve ter subido c/ yolov8s + ppes carregados):"
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader | sed 's/^/   /'
echo ">> LOGS: carga on-demand + deteccao + erros (ultimos 6 min):"
echo 'tvlantvlan' | sudo -S journalctl -u vigia_nvdec@0 -u vigia_nvdec@1 --since "6 min ago" --no-pager 2>/dev/null | grep -iE "on-demand|carregado|\[detect\]|\[ALERTA\]|nvdec:infer|Traceback|Error|camera02rafae01" | tail -30
echo "(fim)"
