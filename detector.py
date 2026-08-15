"""
Corexia Vision AI — serviço de detecção real-time LOCAL (nas GPUs).
Fluxo: Flussonic (RTSP) -> Roboflow Inference LOCAL na GPU -> Gemini confirma
       -> POST no webhookAlertas do Base44 (que cria o Alerta + dispara WhatsApp).

Rodar:  CUDA_VISIBLE_DEVICES=0 python detector.py 0
        CUDA_VISIBLE_DEVICES=1 python detector.py 1
(o argumento é o grupo de GPU; cada câmera no cameras.json tem um campo "gpu")
"""
import os, sys, json, time, base64
import cv2, requests
from dotenv import load_dotenv
from inference import InferencePipeline

load_dotenv()

# ---------------- CONFIG (.env) ----------------
ROBOFLOW_API_KEY = os.environ["ROBOFLOW_API_KEY"]
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_URL      = os.environ["WEBHOOK_URL"]                 # .../webhookAlertas
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "corexia-webhook-2024")
MODEL_ID         = os.getenv("MODEL_ID", "SEU-MODELO/1")    # do Roboflow Universe
CONF_MIN         = float(os.getenv("CONF_MIN", "0.45"))     # confiança mínima do YOLO
COOLDOWN_SEG     = int(os.getenv("COOLDOWN_SEG", "300"))    # não repetir alerta por câmera+tipo
USE_GEMINI       = os.getenv("USE_GEMINI_VERIFY", "true").lower() == "true"
MAX_FPS          = float(os.getenv("MAX_FPS", "4"))         # 4fps já basta p/ arma
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Mapeia a classe do modelo -> tipo do enum Alerta do Base44
CLASS_MAP = {
    "gun": "arma_fogo", "pistol": "arma_fogo", "rifle": "arma_fogo",
    "weapon": "arma_fogo", "handgun": "arma_fogo", "firearm": "arma_fogo", "arma": "arma_fogo",
    "knife": "arma_branca", "faca": "arma_branca",
    "fire": "fogo", "smoke": "fogo", "fogo": "fogo",
    "person": "intruso", "pessoa": "intruso",
}
# Quais tipos realmente geram alerta (ex.: tire "intruso" p/ não spammar de dia)
TIPOS_ATIVOS = set(t.strip() for t in os.getenv("TIPOS_ATIVOS", "arma_fogo,arma_branca,fogo").split(","))

# ---------------- Câmeras deste grupo de GPU ----------------
GPU = sys.argv[1] if len(sys.argv) > 1 else "0"
cams_all = json.load(open("cameras.json", encoding="utf-8"))
cams = [c for c in cams_all if str(c.get("gpu", "0")) == str(GPU)]
if not cams:
    print(f"[GPU {GPU}] Nenhuma câmera configurada para esta GPU."); sys.exit(0)

urls   = [c["rtsp_url"] for c in cams]
by_idx = {i: c for i, c in enumerate(cams)}
ultimo = {}  # (nome, tipo) -> timestamp do último alerta (cooldown)


def gemini_confirma(jpg_bytes, camera_nome, tipo):
    """2º estágio: Gemini olha o frame e confirma. Fail-open (na dúvida, alerta)."""
    if not USE_GEMINI or not GEMINI_API_KEY:
        return True, f"{tipo} detectado"
    b64 = base64.b64encode(jpg_bytes).decode()
    prompt = (f'Câmera de segurança "{camera_nome}". Um detector marcou possível "{tipo}". '
              'Olhe a imagem e confirme. Responda SÓ JSON: '
              '{"confirmado": true/false, "descricao": "o que você vê em 1 frase"}')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    try:
        r = requests.post(url, timeout=20, json={
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
        })
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(txt)
        return bool(data.get("confirmado")), data.get("descricao", f"{tipo} detectado")
    except Exception as e:
        print(f"[Gemini] erro ({e}) — fail-open, alertando mesmo assim")
        return True, f"{tipo} detectado"


def envia_alerta(cam, tipo, conf, descricao):
    """POST no webhookAlertas do Base44 -> cria Alerta + dispara WhatsApp."""
    payload = {
        "secret": WEBHOOK_SECRET,
        "camera_nome": cam["nome"],
        "camera_id": cam.get("camera_id", ""),
        "tipo": tipo,
        "descricao": f"[IA local] {descricao}",
        "confianca": int(conf * 100),
    }
    if cam.get("cliente_telefone"):
        payload["cliente_telefone"] = cam["cliente_telefone"]
    if cam.get("cliente_nome"):
        payload["cliente_nome"] = cam["cliente_nome"]
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        print(f"[ALERTA] {cam['nome']} | {tipo} {int(conf*100)}% -> HTTP {r.status_code}")
    except Exception as e:
        print(f"[Webhook] erro ao enviar: {e}")


def _processa(predictions, video_frame):
    idx = getattr(video_frame, "source_id", 0) or 0
    cam = by_idx.get(idx, cams[0])
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    if not preds:
        return

    # pega a melhor detecção mapeável acima do limiar
    melhor = None
    for p in preds:
        classe = str(p.get("class", "")).lower()
        tipo = CLASS_MAP.get(classe)
        conf = float(p.get("confidence", 0))
        if tipo and tipo in TIPOS_ATIVOS and conf >= CONF_MIN:
            if melhor is None or conf > melhor[1]:
                melhor = (tipo, conf)
    if not melhor:
        return
    tipo, conf = melhor

    # cooldown por câmera+tipo
    now = time.time()
    k = (cam["nome"], tipo)
    if now - ultimo.get(k, 0) < COOLDOWN_SEG:
        return

    # encoda o frame p/ enviar ao Gemini
    ok, buf = cv2.imencode(".jpg", video_frame.image)
    if not ok:
        return
    confirmado, desc = gemini_confirma(buf.tobytes(), cam["nome"], tipo)
    if not confirmado:
        print(f"[x] Gemini rejeitou {tipo} em {cam['nome']} (falso positivo)")
        return

    ultimo[k] = now
    envia_alerta(cam, tipo, conf, desc)


def sink(predictions, video_frame):
    # suporta 1 fonte ou lote de várias fontes
    if isinstance(video_frame, list):
        for p, vf in zip(predictions, video_frame):
            if vf is not None:
                _processa(p, vf)
    else:
        _processa(predictions, video_frame)


print(f"[GPU {GPU}] iniciando {len(urls)} câmera(s): {', '.join(c['nome'] for c in cams)}")
print(f"          modelo={MODEL_ID} | tipos={sorted(TIPOS_ATIVOS)} | gemini={USE_GEMINI}")
pipeline = InferencePipeline.init(
    model_id=MODEL_ID,
    video_reference=urls,
    on_prediction=sink,
    api_key=ROBOFLOW_API_KEY,
    max_fps=MAX_FPS,
)
pipeline.start()
pipeline.join()
