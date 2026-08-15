#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
echo ">> rotas do portal (esperado 200):"
for r in /provedor /provedor/clientes /provedor/faturas /provedor/cameras; do echo "   $r -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000$r)"; done
echo -n "   sem token /prov/clientes -> "; curl -s -o /dev/null -w '%{http_code} (esp 403)\n' http://localhost:8000/api/comercial/prov/clientes
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
tok=requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")
r=requests.get(B+"/api/comercial/prov/clientes",headers={"Authorization":"Bearer "+tok},timeout=10)
print("   admin /prov/clientes ->", r.status_code, "(esp 403; admin usa /comercial)")
PY
echo ">> como o servidor faz hash de senha:"
grep -niE 'password_hash|def _hash|hashlib|bcrypt|pbkdf2|scrypt|check_password' server.py | head -12
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "portal do provedor (MVP): casca + dashboard + Meus Clientes + Cobranca escopados ao provedor; /provedor/* + /api/comercial/prov/*"; fi
git log --oneline -1
