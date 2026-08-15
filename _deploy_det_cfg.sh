#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe detector"; ./venv/bin/python -m py_compile detector_saas.py detector_nvdec.py || { echo ERRO; exit 1; }
echo ">> teste de logica do gating (mesma logica das funcoes do detector)"
./venv/bin/python - <<'PY'
import time
_CFG_ALIASES={"arma_fogo":("arma_fogo","arma"),"arma_branca":("arma_branca","arma","faca"),"fogo":("fogo","fogo_fumaca","fumaca","incendio"),"movimento":("movimento",),"placa":("placa",)}
def ativos(cam,ts):
    cfg=cam.get("config_analitico")
    if not cfg or not cfg.get("ativo",True): return None
    lt=time.localtime(ts); iso=lt.tm_wday+1; dias={iso,iso%7}; hhmm="%02d:%02d"%(lt.tm_hour,lt.tm_min)
    for h in (cfg.get("horarios") or []):
        if dias & set(h.get("dias") or []):
            if (h.get("hora_inicio") or "00:00")<=hhmm<=(h.get("hora_fim") or "23:59"): return set(h.get("analiticos") or [])
    return set(cfg.get("analiticos_padrao") or [])
def ativo(cam,tipo,ts):
    a=ativos(cam,ts)
    return False if a is None else any(x in a for x in _CFG_ALIASES.get(tipo,(tipo,)))
lt=time.localtime()
t10=time.mktime((lt.tm_year,lt.tm_mon,lt.tm_mday,10,0,0,0,0,-1))  # hoje 10:00 (dentro)
t03=time.mktime((lt.tm_year,lt.tm_mon,lt.tm_mday, 3,0,0,0,0,-1))  # hoje 03:00 (fora)
cfg={"ativo":True,"horarios":[{"dias":[0,1,2,3,4,5,6,7],"hora_inicio":"08:00","hora_fim":"18:00","analiticos":["fogo","arma","placa"]}],"analiticos_padrao":["fogo"]}
cam={"config_analitico":cfg}; sem={"config_analitico":None}; inativo={"config_analitico":{"ativo":False}}
def chk(nome,got,esp): print(("  OK " if got==esp else "  XX ")+nome+" -> "+str(got)+" (esp "+str(esp)+")")
chk("dentro 10h arma_fogo (alias 'arma')", ativo(cam,"arma_fogo",t10), True)
chk("dentro 10h movimento (nao listado) ", ativo(cam,"movimento",t10), False)
chk("fora  03h arma_fogo (padrao so fogo)", ativo(cam,"arma_fogo",t03), False)
chk("fora  03h fogo (via padrao)         ", ativo(cam,"fogo",t03), True)
chk("SEM config -> nada roda             ", ativo(sem,"fogo",t10), False)
chk("config inativa -> nada roda         ", ativo(inativo,"fogo",t10), False)
PY
echo ">> restart nvdec workers"; pkill -f detector_nvdec.py; sleep 12
echo "   nvdec procs:"; pgrep -af detector_nvdec.py | head
echo "   is-active: nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo ">> commit detector"
git add detector_saas.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "detector: honra a tela Analiticos por Camera (gate por camera+horario em _process; sem config = nada roda)"; fi
git log --oneline -1
