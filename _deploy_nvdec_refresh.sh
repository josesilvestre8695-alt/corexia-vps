#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile detector_nvdec.py || { echo ERRO; exit 1; }
echo ">> confirma o refresh de metadados no sync (linha nova):"
grep -nE "refaz os metadados|runtimes\[cid\].cam = c" detector_nvdec.py | sed 's/^/   /'
echo ">> restart nvdec"; pkill -f detector_nvdec.py; sleep 12
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo "   procs:"; pgrep -af detector_nvdec.py | head
echo ">> commit"
git add detector_nvdec.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "nvdec: refresh de metadados por sync (config_analitico/ia_placa) sem reabrir stream -> edicao na tela vale no proximo sync (~120s)"; fi
git log --oneline -1
