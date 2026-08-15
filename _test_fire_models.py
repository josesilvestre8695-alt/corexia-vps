import requests, sys, os
API = sys.argv[1]
IMG = "/home/tvlan/fire.jpg"

# baixa uma imagem de fogo (tenta varias fontes)
if not os.path.exists(IMG):
    urls = [
        "https://upload.wikimedia.org/wikipedia/commons/0/06/Fire.JPG",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/36/Large_bonfire.jpg/640px-Large_bonfire.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/8/86/Fire_from_brazier.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/6/68/Feu_de_camp.jpg",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and len(r.content) > 8000:
                open(IMG, "wb").write(r.content)
                print("imagem de fogo baixada:", u, "(", len(r.content), "bytes )")
                break
        except Exception as e:
            print("falhou baixar:", u, str(e)[:60])

import cv2
from inference import get_model
frame = cv2.imread(IMG) if os.path.exists(IMG) else None
print("tem imagem pra testar:", frame is not None)

candidatos = ["continuous_fire/8", "continuous_fire/6", "fire-and-smoke-detection-hiwia/1",
              "fire-smoke-yolov8/1", "improved-yolov8-model/1", "fire-2qfru/1",
              "wildfire-smoke-detection/1", "fire-smoke-detection-yolov11/1"]
for mid in candidatos:
    try:
        m = get_model(model_id=mid, api_key=API)
        if frame is not None:
            res = m.infer(frame, confidence=0.20)[0]
            preds = [(p.class_name, round(p.confidence, 2)) for p in res.predictions]
            print(mid, "-> OK | deteccoes:", preds[:6])
        else:
            print(mid, "-> CARREGOU (sem imagem)")
    except Exception as e:
        print(mid, "-> ERRO |", str(e)[:110])
