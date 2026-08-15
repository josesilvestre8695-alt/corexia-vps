import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from inference import get_model
API = os.environ["ROBOFLOW_API_KEY"]

def classes_of(m):
    for attr in ("class_names", "classes"):
        v = getattr(m, attr, None)
        if v: return list(v)
    return None

def test(titulo, cands):
    print("\n===== %s =====" % titulo)
    for mid in cands:
        try:
            m = get_model(model_id=mid, api_key=API)
            print("  [OK] %-58s classes=%s" % (mid, classes_of(m)))
        except Exception as e:
            print("  [x]  %-58s %s" % (mid, str(e)[:70]))

test("EPI / PPE", [
    "ppes-kaxsi/8", "ppes-kaxsi/11", "ppes-kaxsi/1",
    "roboflow-universe-projects/personal-protective-equipment-combined-model/1",
    "roboflow-universe-projects/personal-protective-equipment-combined-model/2",
    "ppe-yh4wn/personal-protective-equipment-dtt2i/1",
    "new-ja4hn/ppe-detection-q897z-2uiwp/1",
    "eduardo-mseom/ppe-detection-yolo-11-uwguf/1"])

test("BALACLAVA / TOCA NINJA / CAPACETE", [
    "thirdvisionfacecoverdetectionbackup/face-cover-detection/1",
    "thirdvisionfacecoverdetectionbackup/face-cover-detection/2",
    "thirdvisionfacecoverdetectionbackup/face-cover-detection/3",
    "jeeva-wrqzk/helmet-mask-detection-y3l25/1",
    "mask-and-helmet-detection/mask-and-helmet-detection/1"])
print("\n(fim)")
