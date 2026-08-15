#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
# 1) token secreto no .env
TOK=$(openssl rand -hex 32)
if grep -q '^COMERCIAL_ASAAS_WEBHOOK_TOKEN=' .env; then
  sed -i "s|^COMERCIAL_ASAAS_WEBHOOK_TOKEN=.*|COMERCIAL_ASAAS_WEBHOOK_TOKEN=$TOK|" .env
else
  echo "COMERCIAL_ASAAS_WEBHOOK_TOKEN=$TOK" >> .env
fi
echo ">> token do webhook gravado no .env (nao exibido)"
# 2) reinicia backend p/ carregar o token
pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
# 3) registra o webhook na conta Asaas (chave da Corexia) via API
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
BASE=os.getenv("ASAAS_BASE_URL","https://api.asaas.com/v3").rstrip("/")
KEY=os.getenv("ASAAS_API_KEY",""); TOK=os.getenv("COMERCIAL_ASAAS_WEBHOOK_TOKEN","")
H={"access_token":KEY,"Content-Type":"application/json","User-Agent":"Corexia/1.0"}
URL="https://www.grupocorexia.com.br/api/comercial/asaas/webhook"
EVENTS=["PAYMENT_CREATED","PAYMENT_UPDATED","PAYMENT_CONFIRMED","PAYMENT_RECEIVED","PAYMENT_OVERDUE","PAYMENT_DELETED"]
body={"name":"Corexia Faturas","url":URL,"email":"contato.heavenmkt@gmail.com","enabled":True,
      "interrupted":False,"apiVersion":3,"authToken":TOK,"sendType":"SEQUENTIALLY","events":EVENTS}
try:
    r=requests.get(BASE+"/webhooks",headers=H,timeout=30)
    existing=None
    if r.ok:
        for w in (r.json().get("data") or []):
            if w.get("url")==URL: existing=w; break
    if existing:
        rr=requests.put(BASE+"/webhooks/"+existing["id"],headers=H,json=body,timeout=30); acao="ATUALIZADO"
    else:
        rr=requests.post(BASE+"/webhooks",headers=H,json=body,timeout=30); acao="CRIADO"
    if rr.ok:
        w=rr.json()
        print("   webhook %s: id=%s | url=%s | enabled=%s | eventos=%d" % (acao, w.get("id"), w.get("url"), w.get("enabled"), len(w.get("events") or EVENTS)))
    else:
        print("   FALHA (%s): %s" % (rr.status_code, rr.text[:300]))
except Exception as e:
    print("   ERRO:", str(e)[:200])
PY
# 4) teste local do endpoint (sem token -> 401 ; com token -> 200)
T=$(grep '^COMERCIAL_ASAAS_WEBHOOK_TOKEN=' .env | cut -d= -f2)
echo -n "   sem token  -> "; curl -s -o /dev/null -w '%{http_code} (esp 401)\n' -X POST -H 'Content-Type: application/json' -d '{"event":"PAYMENT_RECEIVED","payment":{}}' http://127.0.0.1:8000/api/comercial/asaas/webhook
echo -n "   com token  -> "; curl -s -o /dev/null -w '%{http_code} (esp 200)\n' -X POST -H "asaas-access-token: $T" -H 'Content-Type: application/json' -d '{"event":"PAYMENT_RECEIVED","payment":{}}' http://127.0.0.1:8000/api/comercial/asaas/webhook
