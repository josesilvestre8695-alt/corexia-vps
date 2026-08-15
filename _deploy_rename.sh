#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
echo "   /comercial/clientes   -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/clientes)  (deve 200)"
echo "   /comercial/provedores -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/provedores)  (deve 404)"
echo ">> titulo da pagina renomeada:"
curl -s -m6 http://localhost:8000/comercial/clientes | grep -oE '<title>[^<]*</title>|<h1>[^<]*</h1>' | sed 's/^/   /' | head -3
echo ">> menu lateral (deve ter Provedor/Revenda -> /comercial/clientes e NAO ter /comercial/provedores):"
curl -s -m6 http://localhost:8000/comercial/clientes | grep -oE 'href="/comercial/(clientes|provedores)">[^<]*' | sed 's/^/   /'
echo ">> ponte SPA: 'Provedores' aponta para /comercial/clientes?"
curl -s -m6 http://localhost:8000/comercial/clientes | grep -oE '"Provedores":"/comercial/[a-z-]+"' | sed 's/^/   /'
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: remove aba de cadastro Provedor/Revenda; renomeia aba Clientes -> Provedor/Revenda (mesma pagina que lista quem assinou o contrato); ponte SPA repontada"; fi
git log --oneline -1
