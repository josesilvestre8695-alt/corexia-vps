"""Captura 1 frame do restream local e roda os modelos (arma + fogo) desenhando as caixas."""
import os, sys
os.environ["CUDA_VISIBLE_DEVICES"] = ""
import cv2
from inference import get_model

API = sys.argv[1]
FRAME = "/tmp/office_frame.jpg"

# frame do restream local (gravador) — sem referer, e local
cap = cv2.VideoCapture("/home/tvlan/corexia-vision-ai/gravacoes_live/6313df722d27e27d7999fb78/index.m3u8")
ok, frame = cap.read()
cap.release()
if not ok:
    print("SEM FRAME do restream"); sys.exit(1)
print("frame:", frame.shape)

anotado = frame.copy()
for mid, cor in [("weapon-detection-using-yolov8/1", (0, 0, 255)), ("fire-smoke-yolov8/1", (0, 165, 255))]:
    m = get_model(model_id=mid, api_key=API)
    res = m.infer(frame, confidence=0.25)[0]
    for p in res.predictions:
        x, y, w, h = int(p.x), int(p.y), int(p.width), int(p.height)
        x1, y1 = x - w // 2, y - h // 2
        cv2.rectangle(anotado, (x1, y1), (x1 + w, y1 + h), cor, 4)
        cv2.putText(anotado, f"{p.class_name} {int(p.confidence*100)}%", (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, cor, 3)
        print(f"  {mid}: {p.class_name} {int(p.confidence*100)}% @ ({x1},{y1},{w}x{h})")

cv2.imwrite(FRAME, anotado, [cv2.IMWRITE_JPEG_QUALITY, 80])
print("salvo:", FRAME)
