import os, sys, time
modo = sys.argv[1]  # "gpu" ou "cpu"
if modo == "gpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["ONNXRUNTIME_EXECUTION_PROVIDERS"] = "[CUDAExecutionProvider,CPUExecutionProvider]"
else:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
API = open(".env").read().split("ROBOFLOW_API_KEY=")[1].split("\n")[0].strip()

import cv2, numpy as np
from inference import get_model
img = cv2.imread("/tmp/office_frame.jpg")
if img is None:
    img = (np.random.rand(1440, 2560, 3) * 255).astype("uint8")

m = get_model(model_id="weapon-detection-using-yolov8/1", api_key=API)
# qual provider a sessao REALMENTE usa?
prov = "?"
try:
    prov = m.onnx_session.get_providers()
except Exception:
    try: prov = m.model.onnx_session.get_providers()
    except Exception: pass

m.infer(img, confidence=0.4)  # warmup
N = 30
t0 = time.time()
for _ in range(N):
    m.infer(img, confidence=0.4)
dt = time.time() - t0
print(f"MODO={modo} | providers={prov} | {N} inferencias em {dt:.2f}s = {dt/N*1000:.0f} ms/frame = {N/dt:.1f} fps")
