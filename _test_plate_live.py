import sys, cv2
from inference import get_model
API = sys.argv[1]
IMGS = sys.argv[2:]

modelos = ["license-plate-recognition-rxg4e/4",
           "license-plate-recognition-rxg4e/6",
           "vehicle-registration-plates-trudk/2"]

carregados = []
for mid in modelos:
    try:
        carregados.append((mid, get_model(model_id=mid, api_key=API)))
    except Exception as e:
        print(mid, "-> ERRO ao carregar:", str(e)[:80])

for img in IMGS:
    frame = cv2.imread(img)
    if frame is None:
        print(f"\n{img}: SEM FRAME")
        continue
    h, w = frame.shape[:2]
    print(f"\n=== {img}  ({w}x{h}) ===")
    for mid, m in carregados:
        try:
            res = m.infer(frame, confidence=0.20)[0]
            preds = sorted([round(p.confidence, 2) for p in res.predictions], reverse=True)
            print(f"  {mid}: {len(preds)} placa(s) | conf: {preds[:8]}")
        except Exception as e:
            print(f"  {mid}: ERRO {str(e)[:70]}")
