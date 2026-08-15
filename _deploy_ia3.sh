#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> backup .env + TIPOS_ATIVOS"
cp -a .env .env.bak-ia3
sed -i 's/^TIPOS_ATIVOS=.*/TIPOS_ATIVOS=arma_fogo,arma_branca,fogo,placa,pessoa,veiculo,animal,epi/' .env
grep '^TIPOS_ATIVOS=' .env | sed 's/^/   /'
echo ">> sintaxe"; ./venv/bin/python -m py_compile detector_saas.py detector_nvdec.py || { echo ERRO; exit 1; }
echo ">> teste de roteamento models_for_cam (mesma logica do core)"
./venv/bin/python - <<'PY'
import os, time
from dotenv import load_dotenv
load_dotenv(".env")
MID=os.getenv("MODEL_ID",""); FIRE=os.getenv("MODEL_ID_FIRE",""); KNIFE=os.getenv("MODEL_ID_KNIFE","")
PLATE=os.getenv("MODEL_ID_PLATE",""); GEN=os.getenv("MODEL_ID_GENERAL","yolov8s-640"); EPI=os.getenv("MODEL_ID_EPI","ppes-kaxsi/8")
VOCAB={"arma":(MID,KNIFE),"arma_fogo":(MID,),"arma_branca":(KNIFE,),"faca":(KNIFE,),"fogo":(FIRE,),
       "placa":(PLATE,),"pessoa":(GEN,),"veiculo":(GEN,),"animal":(GEN,),"epi":(EPI,)}
def ativos(cam,ts):
    cfg=cam.get("config_analitico")
    if not cfg or not cfg.get("ativo",True): return None
    lt=time.localtime(ts); iso=lt.tm_wday+1; dias={iso,iso%7}; hhmm="%02d:%02d"%(lt.tm_hour,lt.tm_min)
    for h in (cfg.get("horarios") or []):
        if dias & set(h.get("dias") or []):
            if (h.get("hora_inicio") or "00:00")<=hhmm<=(h.get("hora_fim") or "23:59"): return set(h.get("analiticos") or [])
    return set(cfg.get("analiticos_padrao") or [])
def models_for(cam,ts):
    a=ativos(cam,ts)
    if not a: return set()
    m=set()
    for x in a:
        for mid in VOCAB.get(x,()):
            if mid: m.add(mid)
    return m
ts=time.time()
cam={"config_analitico":{"ativo":True,"horarios":[],"analiticos_padrao":["fogo","arma","pessoa","epi"]}}
sem={"config_analitico":None}
r=models_for(cam,ts)
print("  fogo+arma+pessoa+epi -> %d modelos: %s" % (len(r), sorted(r)))
print("  inclui GERAL=%s EPI=%s FIRE=%s WEAPON=%s KNIFE=%s" % (GEN in r, EPI in r, FIRE in r, MID in r, KNIFE in r))
print("  SEM config -> %s (esp: set() vazio)" % models_for(sem,ts))
PY
echo ">> commit"
git add detector_saas.py detector_nvdec.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "detector: analiticos ON-DEMAND por camera (carga lazy + roda so onde habilitado); +modelos geral(COCO yolov8s-640) e EPI(ppes-kaxsi/8); pessoa/veiculo/animal/epi sem Gemini"; fi
git log --oneline -1
echo ">> restart nvdec (shutdown CUDA e lento ~45s)"; pkill -f detector_nvdec.py
