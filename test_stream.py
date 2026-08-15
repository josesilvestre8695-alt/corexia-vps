"""Testa ingestao do stream HLS ao vivo + inferencia por ~45s (sem alertar)."""
import os, sys, time
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from inference import InferencePipeline

API = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID = os.environ["MODEL_ID"]
URL = sys.argv[1]
SEGS = int(sys.argv[2]) if len(sys.argv) > 2 else 45

count = {"n": 0, "det": 0}
def sink(pred, frame):
    count["n"] += 1
    preds = pred.get("predictions", []) if isinstance(pred, dict) else []
    if preds:
        count["det"] += 1
        for p in preds:
            print(f"   deteccao: {p.get('class')} {p.get('confidence'):.2f}")
    if count["n"] % 5 == 0:
        print(f"frames processados: {count['n']} | com deteccao: {count['det']}")

print(f"Conectando no stream (max {SEGS}s)...")
pipe = InferencePipeline.init(model_id=MODEL_ID, video_reference=URL,
                              on_prediction=sink, api_key=API, max_fps=2)
pipe.start()
time.sleep(SEGS)
pipe.terminate()
pipe.join()
print(f"\nFIM. total frames processados: {count['n']} | frames com deteccao: {count['det']}")
if count["n"] > 0:
    print("[OK] O detector consumiu o stream HLS ao vivo e rodou inferencia.")
else:
    print("[!] Nenhum frame processado — investigar leitura do HLS.")
