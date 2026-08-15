#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
echo ">> script de esconder itens injetado no index do SPA?"
curl -s -m6 http://localhost:8000/ | grep -oE 'var HIDE=\[[^]]*\]' | sed 's/^/   /' | head -1
echo ">> ponte ainda presente (MAP)?"
curl -s -m6 http://localhost:8000/ | grep -oc 'corexia-bridge' | sed 's/^/   corexia-bridge ocorrencias: /'
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "bridge: esconde itens do menu do SPA (mosaicos, visualizar mosaicos, rastreamento, visitas, config servicos, agendamentos, ordens de servico, crm, marketing) via observer, sem tocar no bundle React"; fi
git log --oneline -1
