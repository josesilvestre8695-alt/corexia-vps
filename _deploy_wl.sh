#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py server.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
echo ">> TESTE white-label (injecao por dominio + client script)"
./venv/bin/python - <<'PY'
import os, re, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
pid=requests.post(B+"/api/entities/Provedor",headers=H,json={"nome":"WL TESTE - APAGAR","origem_proposta":"x"},timeout=10).json().get("id")
requests.post(B+"/api/comercial/branding/salvar",headers=H,json={"provedor_id":pid,"nome_marca":"Viggia Teste","cor":"#22c55e","cor_menu":"#0b3d1f","dominio":"wltest.local"},timeout=10)
r=requests.get(B+"/",headers={"Host":"wltest.local"},timeout=15); html=r.text
print("   corexia-wl (client script) injetado:", "corexia-wl" in html)
print("   corexia-bridge injetado:", "corexia-bridge" in html)
m=re.search(r"<style id='wl-theme'>.*?</style>", html, re.S)
print("   tema por dominio:", (m.group(0)[:160] if m else "NAO ACHOU"))
print("   window.__WL__:", ("window.__WL__" in html))
mm=re.search(r"window.__WL__=\{[^}]*\}", html)
print("   __WL__:", mm.group(0) if mm else "-")
r2=requests.get(B+"/",headers={"Host":"grupocorexia.com.br"},timeout=15)
print("   dominio compartilhado NAO injeta tema por dominio (esp sem wl-theme):", "wl-theme" not in r2.text)
requests.delete(B+"/api/entities/Provedor/"+pid,headers=H,timeout=10)
print("   cleanup ok")
PY
echo ">> commit"
git add comercial.py server.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "white-label fase 1: branding por provedor (cor/menu/logo/dominio) + injecao no SPA (tema por dominio no head + client script aplica apos login) + endpoints /api/comercial/branding/*"; fi
git log --oneline -1
