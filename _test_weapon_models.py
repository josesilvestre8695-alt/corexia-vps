import sys, cv2, os, requests
from inference import get_model
API = sys.argv[1]

# tenta baixar uma imagem REALISTA (pessoa com pistola) — cenario de camera de verdade
REAL = "/home/tvlan/pessoa_arma.jpg"
if not os.path.exists(REAL):
    for u in ["https://commons.wikimedia.org/wiki/Special:FilePath/Beretta_M9_being_fired.jpg",
              "https://commons.wikimedia.org/wiki/Special:FilePath/USMC-120508-M-6684S-004.jpg",
              "https://commons.wikimedia.org/wiki/Special:FilePath/Soldier_firing_M9_pistol.jpg"]:
        try:
            r = requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and len(r.content) > 8000:
                open(REAL, "wb").write(r.content); print("imagem realista:", u.split('/')[-1]); break
        except Exception:
            pass

imgs = {"gun.jpg (top-down, DIFICIL)": "/home/tvlan/corexia-vision-ai/gun.jpg"}
if os.path.exists(REAL):
    imgs["pessoa_arma (realista)"] = REAL

modelos = ["yolo-weapon-detection/2", "weapon-detection-using-yolov8/1",
           "weapon-detection-using-yolov8/2", "weapon-detection-using-yolov8/3",
           "pistol-rifle-knife/1"]
for nome, path in imgs.items():
    frame = cv2.imread(path)
    print(f"\n### {nome} ({None if frame is None else frame.shape}) ###")
    for mid in modelos:
        try:
            m = get_model(model_id=mid, api_key=API)
            res = m.infer(frame, confidence=0.10)[0]
            confs = sorted([round(p.confidence, 2) for p in res.predictions], reverse=True)
            alta = sum(1 for c in confs if c >= 0.60)
            print(f"  {mid:34} -> {len(confs):2} det | {alta} c/>=60% | max {max(confs, default=0):.2f}")
        except Exception as e:
            print(f"  {mid:34} -> ERRO {str(e)[:45]}")
