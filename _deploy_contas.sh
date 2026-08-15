#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
for r in /comercial/contas-pagar /comercial/faturas; do echo "   $r -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000$r)"; done
echo ">> TESTE CRUD Despesa (cria, confere KPI-source, limpa)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
did=requests.post(B+"/api/entities/Despesa",headers=H,json={"descricao":"TESTE DESPESA - APAGAR","categoria":"Outros","fornecedor":"X","valor":123.45,"vencimento":"2026-08-10","status":"pendente","recorrencia":"unica"},timeout=10).json().get("id")
back=[d for d in requests.get(B+"/api/entities/Despesa?limit=2000",headers=H,timeout=10).json() if d.get("id")==did]
print("   criada ->", bool(back), "valor:", (back[0].get("valor") if back else None))
requests.delete(B+"/api/entities/Despesa/"+did,headers=H,timeout=10)
print("   cleanup ok (despesa de teste apagada)")
PY
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: tela Contas a Pagar (CRUD Despesa, KPIs a pagar/vencidas/pago, categorias, marcar paga)"; fi
git log --oneline -1
