#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> camera de teste esta ONLINE p/ o nvdec? (stream_valido + engine + config)"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
for eng in ["nvdec", None]:
    body={"secret":sec,"validar":True}
    if eng: body["decode_engine"]=eng
    r=requests.post("http://localhost:8000/listarCamerasIA", json=body, timeout=90)
    for c in r.json().get("cameras",[]):
        if c.get("config_analitico"):
            print("  eng=%s | %s | valido=%s | engine=%s | id=%s" % (eng, c.get("nome"), c.get("stream_valido"), c.get("decode_engine"), c.get("id")))
PY
echo ">> qual worker (hash%2) pega o id da camera de teste?"
./venv/bin/python - <<'PY'
ids=["6313df722d27e27d7999fb78"]
for i in ids: print("  id=%s -> worker %d" % (i, hash(str(i))%2))
print("  (obs: hash do python varia por processo; o worker real usa o mesmo algoritmo internamente)")
PY
echo ">> journalctl funciona? ultimas linhas cruas do nvdec@0:"
echo 'tvlantvlan' | sudo -S journalctl -u vigia_nvdec@0 -n 15 --no-pager 2>&1 | tail -16
