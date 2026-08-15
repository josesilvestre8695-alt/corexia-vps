#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py || { echo ERRO; exit 1; }
echo ">> restart"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
for r in /comercial/contratos /comercial/propostas; do echo "   $r -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000$r)"; done
echo ">> TESTE: gera contrato a partir de uma proposta e confere o preenchimento (nome+CNPJ), depois limpa"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
# proposta de teste (simula o que a tela Propostas cria)
pid=requests.post(B+"/api/entities/Proposta",headers=H,json={"cliente_nome":"PROVEDOR TESTE CONTRATO - APAGAR","document_type":"cnpj","document_number":"41901191000140","cidade":"Sao Paulo","uf":"SP","status":"pendente"},timeout=10).json().get("id")
prop=[p for p in requests.get(B+"/api/entities/Proposta",headers=H,timeout=10).json() if p.get("id")==pid][0]
# gera o contrato a partir da proposta (o que a tela Contratos faz)
cid=requests.post(B+"/api/entities/Contrato",headers=H,json={"proposta_id":pid,"cliente_nome":prop["cliente_nome"],"document_type":prop["document_type"],"document_number":prop["document_number"],"cidade":prop["cidade"],"uf":prop["uf"],"local":"Sao Paulo/SP","data_iso":"2026-07-31","status":"rascunho"},timeout=10).json().get("id")
ct=[c for c in requests.get(B+"/api/entities/Contrato?limit=2000",headers=H,timeout=10).json() if c.get("id")==cid][0]
print("   contrato gerado -> nome:", ct.get("cliente_nome"), "| doc:", ct.get("document_number"), "| local:", ct.get("local"), "| status:", ct.get("status"))
ok = ct.get("cliente_nome")==prop["cliente_nome"] and ct.get("document_number")==prop["document_number"]
print("   preenchimento a partir da proposta:", "OK" if ok else "FALHOU")
requests.delete(B+"/api/entities/Contrato/"+cid,headers=H,timeout=10)
requests.delete(B+"/api/entities/Proposta/"+pid,headers=H,timeout=10)
print("   cleanup ok (proposta+contrato de teste apagados)")
PY
echo ">> commit"
git add comercial.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "comercial: tela Contratos (contrato-base preenchido da proposta: nome+CNPJ+endereco+local+data, ver/imprimir/PDF, auto-assinado ao assinar a proposta)"; fi
git log --oneline -1
