#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> LIGANDO modo producao (COMERCIAL_ASAAS_LIVE=1)"
cp -a .env .env.bak-golive
if grep -q '^COMERCIAL_ASAAS_LIVE=' .env; then
  sed -i 's/^COMERCIAL_ASAAS_LIVE=.*/COMERCIAL_ASAAS_LIVE=1/' .env
else
  echo 'COMERCIAL_ASAAS_LIVE=1' >> .env
fi
grep '^COMERCIAL_ASAAS_LIVE=' .env | sed 's/^/   /'
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   backend: $(systemctl is-active corexia-backend)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
H={"Authorization":"Bearer "+requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")}
ping=requests.get(B+"/api/comercial/ping",headers=H,timeout=10).json()
print("   modo asaas_live:", ping.get("asaas_live"), "(esp True)")
prop={"cliente_nome":"TESTE GO-LIVE - APAGAR","document_type":"cnpj","document_number":"30668322000174",
 "email":"teste-golive@corexia.com.br","whatsapp":"81997335544","valor_mensal":10.0,
 "plano_nome":"Teste Go-live","tipo_plano":"teste","contrato_meses":12,"qtd_cameras":1,"status":"aprovada"}
pid=requests.post(B+"/api/entities/Proposta",headers=H,json=prop,timeout=10).json().get("id")
open("/tmp/golive_prop.txt","w").write(pid or "")
print("   proposta de teste criada:", pid)
r=requests.post(B+"/api/comercial/propostas/"+pid+"/enviar-codigo",headers=H,timeout=30).json()
print("   enviar-codigo ->", r)
PY
