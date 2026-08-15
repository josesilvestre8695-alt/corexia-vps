#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
./venv/bin/python - <<'PY'
import os, time, requests
from dotenv import load_dotenv
load_dotenv(".env")
import asaas
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
pid=open("/tmp/golive_prop.txt").read().strip()
print(">> ASSINANDO proposta", pid)
r=requests.post(B+"/api/comercial/propostas/"+pid+"/assinar",headers=H,json={"codigo":"050790"},timeout=60).json()
print("   assinar ->", r)
modo=r.get("modo"); cid=r.get("asaas_customer_id"); sid=r.get("asaas_subscription_id"); alvo_id=r.get("alvo_id")
print(">> VERIFICACAO no Asaas (conta da Corexia)")
if modo=="real" and sid and not str(sid).startswith("TESTE_"):
    try:
        sub=asaas.get_subscription(sid)
        print("   assinatura:", sub.get("id"),"| valor R$",sub.get("value"),"| ciclo:",sub.get("cycle"),"| status:",sub.get("status"),"| maxPayments:",sub.get("maxPayments"))
    except Exception as e: print("   erro get sub:", str(getattr(e,'body',e))[:150])
    try:
        cust=asaas._req("GET","/customers/"+cid)
        print("   cliente:", cust.get("name"),"| CNPJ:",cust.get("cpfCnpj"))
    except Exception as e: print("   erro get customer:", str(getattr(e,'body',e))[:120])
    pays=[]
    try:
        pays=asaas.list_payments(subscription_id=sid).get("data",[])
        print("   cobranca gerada:", [(p.get("value"),p.get("status"),p.get("billingType"),p.get("dueDate")) for p in pays])
    except Exception as e: print("   erro list pays:", e)
else:
    print("   modo NAO real (modo=%s) -> algo errado; ids:" % modo, cid, sid)
print(">> WEBHOOK: fatura espelhada no sistema?")
time.sleep(3)
fats=[f for f in requests.get(B+"/api/entities/Fatura?limit=200",headers=H,timeout=15).json() if f.get("asaas_customer_id")==cid]
print("   faturas no sistema p/ o cliente:", len(fats), [(f.get("valor"),f.get("status")) for f in fats])
print(">> LIMPEZA (apaga cobranca+assinatura+cliente no Asaas e dados de teste)")
if sid and not str(sid).startswith("TESTE_"):
    for p in pays:
        try: asaas._req("DELETE","/payments/"+p["id"]); print("   cobranca apagada:", p["id"])
        except Exception as e: print("   err del pay:", str(getattr(e,'body',e))[:80])
    try: asaas.delete_subscription(sid); print("   assinatura apagada")
    except Exception as e: print("   err del sub:", str(getattr(e,'body',e))[:100])
    try: asaas.delete_customer(cid); print("   cliente apagado")
    except Exception as e: print("   err del cliente:", str(getattr(e,'body',e))[:100])
if alvo_id:
    requests.delete(B+"/api/entities/Provedor/"+alvo_id,headers=H,timeout=10); print("   Provedor de teste removido")
requests.delete(B+"/api/entities/Proposta/"+pid,headers=H,timeout=10); print("   Proposta de teste removida")
import sqlite3, json
c=sqlite3.connect("corexia.db"); n=0
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='Fatura'").fetchall():
    if json.loads(data).get("asaas_customer_id")==cid: c.execute("DELETE FROM entities WHERE id=?", (rid,)); n+=1
c.commit(); c.close(); print("   faturas de teste removidas:", n)
print(">> FIM DO TESTE")
PY
