"""Verifica qual versao do modelo Roboflow carrega e roda numa imagem."""
import sys, os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from inference import get_model

API = os.environ["ROBOFLOW_API_KEY"]
PROJECT = os.environ.get("MODEL_PROJECT", "yolo-weapon-detection")
img = sys.argv[1] if len(sys.argv) > 1 else "cam2.jpg"
print(f"Projeto: {PROJECT} | Imagem: {img}\n")

loaded = None
for v in range(1, 5):
    mid = f"{PROJECT}/{v}"
    try:
        print(f"tentando {mid} ...")
        m = get_model(model_id=mid, api_key=API)
        loaded = mid
        res = m.infer(img)[0]
        preds = res.predictions
        print(f"[OK] {mid} carregou e rodou. deteccoes na imagem: {len(preds)}")
        for p in preds:
            print(f"   - {p.class_name}  {p.confidence:.2f}")
        break
    except Exception as e:
        print(f"[x] {mid}: {type(e).__name__}: {str(e)[:160]}")

print()
if loaded:
    print(f"==> USAR MODEL_ID={loaded}")
else:
    print("Nenhuma versao carregou. Confere o slug do projeto no Roboflow.")
