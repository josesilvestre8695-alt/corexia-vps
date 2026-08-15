#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe server"; ./venv/bin/python -m py_compile server.py || { echo ERRO; exit 1; }
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend)"
echo ">> /listarCamerasIA devolve config_analitico?"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
sec=os.getenv("WEBHOOK_SECRET","")
cams={}
for eng in ["nvdec", None]:
    body={"secret":sec,"validar":False}
    if eng: body["decode_engine"]=eng
    r=requests.post("http://localhost:8000/listarCamerasIA", json=body, timeout=60)
    for c in r.json().get("cameras",[]): cams[c["id"]]=c
cams=list(cams.values())
comcfg=[c for c in cams if c.get("config_analitico")]
print("   total cams:",len(cams),"| COM config:",len(comcfg),"| SEM config (off):",len(cams)-len(comcfg))
for c in comcfg[:3]:
    cf=c["config_analitico"]; print("    -",c.get("nome"),"| ativo:",cf.get("ativo"),"| horarios:",len(cf.get("horarios",[])),"| padrao:",cf.get("analiticos_padrao"))
PY
echo ">> commit server"
git add server.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "listarCamerasIA: anexa config_analitico por camera (tela Analiticos por Camera) p/ o detector filtrar"; fi
git log --oneline -1
