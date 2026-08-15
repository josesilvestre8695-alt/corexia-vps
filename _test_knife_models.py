"""Benchmark de modelos p/ FACA: precisa (a) detectar faca em foto clara e
(b) NAO detectar nada no frame do escritorio com a cadeira (fonte de falso Shotgun)."""
import requests, sys, os
os.environ["CUDA_VISIBLE_DEVICES"] = ""
API = sys.argv[1]
KNIFE = "/tmp/knife.jpg"
OFFICE = "/tmp/alerta_faca.jpg"   # frame real do escritorio (cadeira)

if not os.path.exists(KNIFE):
    urls = [
        "https://commons.wikimedia.org/wiki/Special:FilePath/Kitchen_knife.jpg",
        "https://commons.wikimedia.org/wiki/Special:FilePath/Chefs_knife.jpg",
        "https://commons.wikimedia.org/wiki/Special:FilePath/Couteau_de_cuisine.jpg",
        "https://commons.wikimedia.org/wiki/Special:FilePath/Global_G-2_chef%27s_knife.jpg",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and len(r.content) > 8000:
                open(KNIFE, "wb").write(r.content)
                print("faca baixada:", u.split("/")[-1], len(r.content), "bytes")
                break
        except Exception as e:
            print("falhou:", u[:60], str(e)[:50])

import cv2
from inference import get_model
kimg = cv2.imread(KNIFE) if os.path.exists(KNIFE) else None
oimg = cv2.imread(OFFICE) if os.path.exists(OFFICE) else None
print("faca img:", kimg is not None, "| office img:", oimg is not None)

candidatos = [
    "weapon-detection-using-yolov8/1",   # ATUAL (baseline)
    "knife-detection/2", "knife-detection/1",
    "knife-detect/1", "knives-detection/1",
    "knife-dataset/1", "sharp-objects/1",
    "weapons-and-knives/1", "knife-2gyps/1",
    "yolo-weapon-detection/2",
]
for mid in candidatos:
    try:
        m = get_model(model_id=mid, api_key=API)
    except Exception as e:
        print(f"{mid} -> NAO EXISTE ({str(e)[:50]})"); continue
    linha = f"{mid}:"
    try:
        if kimg is not None:
            res = m.infer(kimg, confidence=0.15)[0]
            dets = [(p.class_name, round(p.confidence, 2)) for p in res.predictions]
            linha += f" FACA={dets[:4] or 'NADA'}"
        if oimg is not None:
            res = m.infer(oimg, confidence=0.30)[0]
            dets = [(p.class_name, round(p.confidence, 2)) for p in res.predictions]
            linha += f" | ESCRITORIO(FP)={dets[:4] or 'limpo'}"
        print(linha)
    except Exception as e:
        print(f"{mid} -> ERRO infer: {str(e)[:60]}")
