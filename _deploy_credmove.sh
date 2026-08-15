#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
for r in /comercial/clientes /comercial/provedores; do echo "   $r -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000$r)"; done
echo ">> credenciais SO em Clientes? (conta o botao abrirCred em cada pagina)"
echo "   provedores abrirCred: $(curl -s -m6 http://localhost:8000/comercial/provedores | grep -oc 'abrirCred')"
echo "   clientes   abrirCred: $(curl -s -m6 http://localhost:8000/comercial/clientes  | grep -oc 'abrirCred')"
echo "   provedores colunas Asaas/Z-API no thead: $(curl -s -m6 http://localhost:8000/comercial/provedores | grep -oE '<th>Asaas</th><th>Z-API</th>' | wc -l)"
echo "   clientes   colunas Asaas/Z-API no thead: $(curl -s -m6 http://localhost:8000/comercial/clientes  | grep -oE '<th>Asaas</th><th>Z-API</th>' | wc -l)"
echo ">> TESTE cred na aba Clientes (cria provedor assinado, salva Z-API, confere cred-status, limpa)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
pv=requests.post(B+"/api/entities/Provedor",headers=H,json={"nome":"REVENDA CRED TESTE - APAGAR","document_type":"cnpj","document_number":"41901191000140","status":"ativo","origem_proposta":"prop_fake"},timeout=10).json().get("id")
# salva Z-API do provedor (sem testar numero -> so armazena)
requests.post(B+"/api/comercial/provedores/"+pv+"/zapi",headers=H,json={"zapi_ativa":True,"zapi_instance_id":"INST_TESTE","zapi_token":"TOK_TESTE","zapi_client_token":"CLI_TESTE"},timeout=15)
cs=requests.get(B+"/api/comercial/provedores/"+pv+"/cred-status",headers=H,timeout=10).json()
print("   cred-status -> asaas_configurado:", cs.get("asaas_configurado"), "| zapi_ativa:", cs.get("zapi_ativa"), "| zapi_configurado:", cs.get("zapi_configurado"), "| zapi_mask:", cs.get("zapi_mask"))
# limpa credencial (entidade ProvedorCred) e o provedor
import sqlite3, json
c=sqlite3.connect("corexia.db"); c.execute("DELETE FROM entities WHERE entity='ProvedorCred' AND id=?", (pv,)); c.commit(); c.close()
requests.delete(B+"/api/entities/Provedor/"+pv,headers=H,timeout=10)
print("   cleanup ok (provedor + credencial de teste apagados)")
PY
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: move gestao Asaas+Z-API de Provedor/Revenda para a aba Clientes (colunas + acao credenciais); Provedores fica so cadastro"; fi
git log --oneline -1
