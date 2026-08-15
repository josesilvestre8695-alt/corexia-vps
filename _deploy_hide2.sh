#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
echo ">> lista HIDE atual no index do SPA:"
curl -s -m6 http://localhost:8000/ | grep -oE 'var HIDE=\[[^]]*\]' | sed 's/^/   /' | head -1
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "bridge: esconde tambem Ponto Eletronico e Config. Servicos do menu do SPA"; fi
git log --oneline -1
