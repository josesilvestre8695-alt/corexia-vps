"""
Visualizador ao vivo OTIMIZADO: janela com a camera + caixas nas deteccoes.
- Limiar baixo (CONF_VIEW, padrao 0.15) p/ pegar mais deteccoes.
- Usa 'supervision' p/ desenhar as caixas na posicao CERTA.

Uso:
  python view.py webcam            -> webcam deste PC
  python view.py cam2              -> camera "servlink" (HLS)
  python view.py caminho.jpg       -> testa numa imagem e salva _annotated.jpg
  CONF_VIEW=0.1 python view.py webcam   -> ajusta o limiar
"""
import os, sys, cv2
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from inference import get_model
import supervision as sv

API = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID = os.environ["MODEL_ID"]
CONF = float(os.environ.get("CONF_VIEW", "0.15").replace(",", "."))

CAM2 = "https://live12.analitico.app.br/stream/get/cameramibotes00.m3u8?token=cameramibotes00"
arg = sys.argv[1] if len(sys.argv) > 1 else "webcam"
if arg == "webcam":   source = 0
elif arg == "cam2":   source = CAM2
elif arg.isdigit():   source = int(arg)
else:                 source = arg

print(f"Modelo={MODEL_ID} | limiar={CONF} | fonte={source}")
model = get_model(model_id=MODEL_ID, api_key=API)
box_ann = sv.BoxAnnotator(thickness=3)
lbl_ann = sv.LabelAnnotator(text_scale=0.6, text_thickness=2)

def annotate(frame):
    res = model.infer(frame, confidence=CONF)[0]
    det = sv.Detections.from_inference(res)
    labels = [f"{p.class_name} {int(p.confidence*100)}%" for p in res.predictions]
    out = box_ann.annotate(frame.copy(), det)
    out = lbl_ann.annotate(out, det, labels=labels)
    return out, labels

# --- Modo imagem (teste): annota e salva ---
if isinstance(source, str) and os.path.isfile(source):
    frame = cv2.imread(source)
    out, labels = annotate(frame)
    outpath = source.rsplit(".", 1)[0] + "_annotated.jpg"
    cv2.imwrite(outpath, out)
    print("Deteccoes:", labels)
    print("Salvo:", outpath)
    sys.exit()

# --- Modo ao vivo ---
if isinstance(source, int):
    print(f"Abrindo webcam (indice {source})... pode levar alguns segundos.")
    cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)   # DSHOW = mais confiavel no Windows
else:
    print("Conectando no stream...")
    cap = cv2.VideoCapture(source)
if not cap.isOpened():
    print("Nao consegui abrir a webcam.")
    print("- Feche apps que usam a camera (Teams, Zoom, navegador, app Camera).")
    print("- Configuracoes > Privacidade e seguranca > Camera > libere para apps da area de trabalho.")
    print("- Se tiver mais de uma camera, tente:  venv\\Scripts\\python.exe view.py 1")
    sys.exit(1)
try:
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
except Exception:
    pass
print("OK! Janela aberta. Aponte a arma pra camera. 'q' fecha.\n")
fails = 0
while True:
    ok, frame = cap.read()
    if not ok or frame is None:
        fails += 1
        if fails >= 60:
            print("webcam parou de enviar frames — encerrando."); break
        if cv2.waitKey(30) & 0xFF == ord("q"):
            break
        continue
    fails = 0
    out, labels = annotate(frame)
    if labels:
        print("detectou:", labels)
    cv2.imshow("Corexia - deteccao ('q' sai)", out)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break
cap.release()
cv2.destroyAllWindows()
