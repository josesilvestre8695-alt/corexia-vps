"""Descobre modelos Roboflow p/ EPI, deteccao geral (COCO) e balaclava/toca ninja.
Imprime quais carregam e a lista de classes (p/ mapear no CLASS_MAP)."""
import os, requests, cv2
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from inference import get_model
API = os.environ["ROBOFLOW_API_KEY"]

def grab(path, urls):
    if os.path.exists(path):
        return path
    for u in urls:
        try:
            r = requests.get(u, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and len(r.content) > 8000:
                open(path, "wb").write(r.content); return path
        except Exception:
            pass
    return None

street = grab("/tmp/street.jpg", [
    "https://upload.wikimedia.org/wikipedia/commons/thumb/6/60/Times_Square_1-2.JPG/640px-Times_Square_1-2.JPG",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Busy_street_in_Shibuya.jpg/640px-Busy_street_in_Shibuya.jpg"])
worker = grab("/tmp/worker.jpg", [
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/US_Navy_...jpg/640px.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Bricklayer_...jpg/640px.jpg"])

def classes_of(m):
    for attr in ("class_names", "classes", "get_class_names"):
        v = getattr(m, attr, None)
        if callable(v):
            try: v = v()
            except Exception: v = None
        if v:
            return list(v)
    return None

def test(titulo, cands, img):
    print("\n===== %s =====" % titulo)
    frame = cv2.imread(img) if (img and os.path.exists(img)) else None
    ok = []
    for mid in cands:
        try:
            m = get_model(model_id=mid, api_key=API)
            cls = classes_of(m)
            det = ""
            if frame is not None:
                try:
                    res = m.infer(frame, confidence=0.12)[0]
                    det = " | viu: " + str(sorted(set(p.class_name for p in res.predictions))[:10])
                except Exception as e:
                    det = " | infer erro: " + str(e)[:50]
            print("  [OK] %-45s classes=%s%s" % (mid, (cls[:20] if cls else "?"), det))
            ok.append(mid)
        except Exception as e:
            print("  [x]  %-45s %s: %s" % (mid, type(e).__name__, str(e)[:80]))
    return ok

test("GERAL / COCO (pessoa, veiculo, animal, bolsa)",
     ["yolov8n-640", "yolov8s-640", "yolov8m-640", "yolov8l-640", "coco/9", "coco/3", "microsoft-coco/9"],
     street)
test("EPI / PPE (capacete, colete, luva, oculos, mascara, bota)",
     ["construction-site-safety-gsnvb/1", "eep-detection-u9qb2/1", "ppe-detection-czrnc/1",
      "personal-protective-equipment-combined-model/1", "hard-hat-workers/2", "hard-hat-workers/1",
      "ppes-kaxsw/1", "ppe-cv/1", "safety-detection-hg9d0/1", "construction-safety-dulth/1",
      "epi-2-hbfjr/1", "eep-2/1", "ppe-detection-2/1"],
     worker)
test("BALACLAVA / TOCA NINJA / CAPACETE",
     ["balaclava-detection/1", "ski-mask-detection/1", "balaclava-gjm4l/1", "balaclavas/1",
      "masked-faces-.../1", "helmet-detection-project/1", "motorcycle-helmet-detection/1",
      "face-mask-balaclava/1", "robbery-detection/1"],
     street)
print("\n(fim)")
