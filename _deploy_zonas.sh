#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> sintaxe"; ./venv/bin/python -m py_compile comercial.py server.py detector_saas.py || { echo ERRO; exit 1; }
echo ">> teste geometria (point-in-poly / dist-seg)"
./venv/bin/python - <<'PY'
def pip(x,y,poly):
    inside=False; n=len(poly); j=n-1
    for i in range(n):
        xi,yi=poly[i]; xj,yj=poly[j]
        if ((yi>y)!=(yj>y)) and (x < (xj-xi)*(y-yi)/((yj-yi) or 1e-9)+xi): inside=not inside
        j=i
    return inside
def dseg(px,py,ax,ay,bx,by):
    dx,dy=bx-ax,by-ay
    if dx==0 and dy==0: return ((px-ax)**2+(py-ay)**2)**.5
    t=max(0,min(1,((px-ax)*dx+(py-ay)*dy)/(dx*dx+dy*dy))); cx,cy=ax+t*dx,ay+t*dy
    return ((px-cx)**2+(py-cy)**2)**.5
sq=[(0.2,0.2),(0.8,0.2),(0.8,0.8),(0.2,0.8)]
print("  centro dentro:", pip(0.5,0.5,sq), "(esp True)")
print("  canto fora:   ", pip(0.05,0.05,sq), "(esp False)")
print("  sobre a linha (diag):", round(dseg(0.5,0.5,0,0,1,1),3), "-> pisa?", dseg(0.5,0.5,0,0,1,1)<=0.035)
print("  longe da linha:      ", round(dseg(0.5,0.9,0,0,1,1),3), "-> pisa?", dseg(0.5,0.9,0,0,1,1)<=0.035)
PY
echo ">> restart backend"; pkill -f 'uvicorn server:app'; sleep 8
echo "   is-active: $(systemctl is-active corexia-backend) | /comercial/analiticos -> $(curl -s -m6 -o /dev/null -w '%{http_code}' http://localhost:8000/comercial/analiticos)"
echo ">> teste: salva zona + confere que volta no /listarCamerasIA (o que o detector le) + camthumb"
./venv/bin/python - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv(".env")
B="http://localhost:8000"; email=(os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower(); pw=os.getenv("ADMIN_PASSWORD","")
tok=requests.post(B+"/api/auth/login",json={"email":email,"password":pw},timeout=10).json().get("token","")
H={"Authorization":"Bearer "+tok}
cams=requests.get(B+"/api/comercial/analiticos/cameras",headers=H,timeout=30).json()
cam=cams[0]; cid=cam["id"]
z=[{"tipo":"zona","nome":"Teste","pontos":[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}]
requests.post(B+"/api/comercial/analiticos/salvar",headers=H,json={"camera_id":cid,"camera_nome":cam["nome"],"ativo":True,"horarios":[],"analiticos_padrao":["intruso"],"zonas_intrusao":z},timeout=10)
sec=os.getenv("WEBHOOK_SECRET","")
lc=requests.post(B+"/listarCamerasIA",json={"secret":sec,"validar":False,"decode_engine":"nvdec"},timeout=60).json().get("cameras",[])
mine=[c for c in lc if c["id"]==cid]
found=mine and mine[0].get("config_analitico",{}).get("zonas_intrusao")
print("   zona chega no detector (listarCamerasIA):", bool(found), "->", found)
t=requests.get(B+"/camthumb/"+cid,headers=H,timeout=30)
print("   camthumb c/ token -> HTTP", t.status_code, "|", len(t.content), "bytes")
# limpa
requests.post(B+"/api/comercial/analiticos/limpar",headers=H,json={"camera_id":cid},timeout=10)
print("   cleanup ok")
PY
echo ">> commit"
git add comercial.py server.py detector_saas.py
if git diff --cached --quiet; then echo "(nada novo)"; else git commit -q -m "analiticos: zona de intrusao + linha virtual (por presenca de pessoa) - editor canvas sobre snapshot na tela + geometria no detector (point-in-poly / dist-seg) + zonas no payload do detector"; fi
git log --oneline -1
echo ">> restart nvdec"; pkill -f detector_nvdec.py
echo "(deploy ok; nvdec reiniciando ~45s)"
