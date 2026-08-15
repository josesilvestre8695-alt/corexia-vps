#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend) | COMERCIAL_ASAAS_LIVE=$(grep -c 'COMERCIAL_ASAAS_LIVE=1' .env)"
for r in /comercial/clientes /comercial/contratos; do echo "   $r -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000$r)"; done
echo ">> menu: Clientes agora e do admin?"
curl -s -m6 http://localhost:8000/comercial/clientes | grep -oE 'data-roles="[^"]*" href="/comercial/clientes"' | sed 's/^/   /' | head -2
echo ">> TESTE fluxo Clientes (cria provedor 'assinado', bloqueia/desbloqueia, sincroniza em modo teste, limpa)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
# simula provedor criado por assinatura de contrato (origem_proposta + ids TESTE)
pv=requests.post(B+"/api/entities/Provedor",headers=H,json={"nome":"REVENDA TESTE CLIENTE - APAGAR","email":"rt@corexia.local","document_type":"cnpj","document_number":"41901191000140","plano_nome":"Painel Local","valor_mensal":797.00,"contrato_meses":12,"status":"ativo","origem_proposta":"prop_fake","asaas_customer_id":"TESTE_cus_x","asaas_subscription_id":"TESTE_sub_x"},timeout=10).json().get("id")
# aparece no filtro de "assinados"?
lst=[p for p in requests.get(B+"/api/entities/Provedor?limit=2000",headers=H,timeout=10).json() if p.get("id")==pv]
print("   aparece na lista (origem_proposta):", bool(lst))
# bloquear / desbloquear
rb=requests.post(B+"/api/comercial/clientes/"+pv+"/bloquear",headers=H,timeout=10).json(); print("   bloquear ->", rb.get("status"))
rd=requests.post(B+"/api/comercial/clientes/"+pv+"/desbloquear",headers=H,timeout=10).json(); print("   desbloquear ->", rd.get("status"))
# sincronizar (deve ser modo teste, sem tocar no Asaas)
rs=requests.post(B+"/api/comercial/clientes/"+pv+"/sincronizar",headers=H,timeout=30).json()
print("   sincronizar -> modo:", rs.get("modo"), "| assinatura_criada:", rs.get("assinatura_criada"), "| sincronizadas:", rs.get("sincronizadas"))
print("   info:", (rs.get("info") or "")[:110])
requests.delete(B+"/api/entities/Provedor/"+pv,headers=H,timeout=10)
print("   cleanup ok (provedor de teste apagado)")
PY
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: tela Clientes (admin) - provedores que assinaram o contrato; Status ativo/bloqueado + Acoes editar/excluir/bloquear/desbloquear/sincronizar (cria assinatura Asaas por plano+tempo de contrato, com trava ASAAS_LIVE)"; fi
git log --oneline -1
