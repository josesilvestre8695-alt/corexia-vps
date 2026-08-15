import requests, sys, os
API = sys.argv[1]
IMG = "/home/tvlan/car_plate.jpg"

# baixa uma foto de carro com placa visivel (tenta varias)
if not os.path.exists(IMG):
    urls = [
        "https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/2018_Volkswagen_Polo_SE_TSI_1.0_Front.jpg/640px-2018_Volkswagen_Polo_SE_TSI_1.0_Front.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2a/2019_Ford_Fiesta_ST-Line_1.0_Front.jpg/640px-2019_Ford_Fiesta_ST-Line_1.0_Front.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/3/39/2017_Vauxhall_Corsa_Energy_1.4_Front.jpg/640px-2017_Vauxhall_Corsa_Energy_1.4_Front.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e2/2015_Renault_Clio_Dynamique_S_Nav_1.5_Front.jpg/640px-2015_Renault_Clio_Dynamique_S_Nav_1.5_Front.jpg",
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and len(r.content) > 8000:
                open(IMG, "wb").write(r.content)
                print("imagem de carro baixada:", u.split("/")[-1], "(", len(r.content), "bytes )")
                break
        except Exception as e:
            print("falhou baixar:", u[:50], str(e)[:60])

import cv2
from inference import get_model
frame = cv2.imread(IMG) if os.path.exists(IMG) else None
print("tem imagem pra testar:", frame is not None)

candidatos = ["license-plate-recognition-rxg4e/4", "license-plate-recognition-rxg4e/6",
              "vehicle-registration-plates-trudk/2", "anpr-fkkgw/1",
              "license-plates-us-eu/3", "automatic-number-plate-recognition-mzr9m/1",
              "plate-detection-8mkbv/1", "license-plate-detection-oxr8f/1"]
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
