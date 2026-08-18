"""
Corexia Vision AI — detector SaaS com AUTO-SYNC (multiplas cameras, sem limite).

Fluxo:
  Base44 (listarCamerasIA) --lista de cameras + URLs frescas--> detector
  detector roda deteccao CONTINUA em todas as cameras validas
  ao detectar -> Gemini confirma -> webhookAlertas (com dados do cliente = WhatsApp)
  re-sincroniza a lista a cada SYNC_INTERVAL: camera nova entra sozinha,
  camera removida/invalida sai. Zero cameras.json manual.

Rodar:  CUDA_VISIBLE_DEVICES="" python detector_saas.py
"""
import os, sys, time, json, base64, subprocess, glob
from collections import deque
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))

# ---- GPU: (re)exec com o ambiente CUDA setado ANTES de importar cv2/inference ----
# (LD_LIBRARY_PATH tem que existir no launch do processo; por isso re-executamos a nos mesmos
#  com o env montado, evitando editar o systemd. DETECTOR_GPU=0 no .env forca CPU.)
_MM = os.path.join(_HERE, "mm_cuda")
if os.getenv("DETECTOR_GPU", "1") != "0" and os.path.isdir(_MM) and os.getenv("COREXIA_GPU_ENV") != "1":
    _sp = os.path.join(_HERE, "venv/lib/python3.10/site-packages/nvidia")
    _libs = [os.path.join(_MM, "targets/x86_64-linux/lib"), os.path.join(_MM, "lib")]
    _libs += sorted(glob.glob(os.path.join(_sp, "*/lib"))) + ["/usr/lib/x86_64-linux-gnu"]
    _env = dict(os.environ)
    _env["COREXIA_GPU_ENV"] = "1"
    _env["LD_LIBRARY_PATH"] = ":".join(_libs) + ((":" + os.environ["LD_LIBRARY_PATH"]) if os.environ.get("LD_LIBRARY_PATH") else "")
    _env["PATH"] = os.path.join(_MM, "bin") + ":" + _env.get("PATH", "")
    _env["CUDA_HOME"] = _MM
    _env.setdefault("ONNXRUNTIME_EXECUTION_PROVIDERS", "[CUDAExecutionProvider,CPUExecutionProvider]")
    # FORCA (nao setdefault): o unit systemd seta CUDA_VISIBLE_DEVICES="" (vazio) = nenhuma GPU.
    # Aqui a gente sobrepoe com as placas reais, sem precisar editar o unit (sem sudo).
    _env["CUDA_VISIBLE_DEVICES"] = os.getenv("DETECTOR_GPUS", "0,1")
    os.execve(sys.executable, [sys.executable] + sys.argv, _env)

import cv2, requests
from inference import InferencePipeline
import supervision as sv

# Opcoes do backend ffmpeg do OpenCV (usado pelo InferencePipeline):
# - Referer: streams com protecao de hotlink (ex.: analitico) exigem o header
# - live_start_index -1: em HLS, comeca no ULTIMO segmento (default -3 = ~9s de atraso
#   a mais). Reduz a latencia de QUALQUER link HLS recebido no SaaS (12s -> ~4s).
STREAM_REFERER = os.getenv("STREAM_REFERER", "")
_ffopts = ["live_start_index;-1"]
if STREAM_REFERER:
    _ffopts.insert(0, f"headers;Referer: {STREAM_REFERER}\r\n")
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(_ffopts)

API            = os.environ["ROBOFLOW_API_KEY"]
MODEL_ID       = os.getenv("MODEL_ID", "yolo-weapon-detection/2")
MODEL_ID_FIRE  = os.getenv("MODEL_ID_FIRE", "")   # 2o modelo (fogo/fumaca), opcional
MODEL_TO_RUN   = os.getenv("MODEL_TO_RUN", MODEL_ID)   # qual modelo ESTE processo roda
GEMINI_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
USE_GEMINI     = os.getenv("USE_GEMINI_VERIFY", "true").lower() == "true"
WEBHOOK_URL    = os.environ["WEBHOOK_URL"]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "corexia-webhook-2024")
CAMERAS_URL    = os.environ["CAMERAS_URL"]
CONF_MIN       = float(os.getenv("CONF_MIN", "0.40").replace(",", "."))

# ---- Limiar de confianca POR TIPO (anti falso-positivo calibrado na pratica) ----
# arma_fogo alto: celular/objeto na mao vira "Handgun" a 44-54% => so alerta >=65%.
# arma_branca baixo: faca e menor/dificil pro modelo => 30% ja conta.
def _parse_map(env, default):
    out = dict(default)
    for par in os.getenv(env, "").split(","):
        if ":" in par:
            k, v = par.split(":", 1)
            try: out[k.strip()] = float(v.strip().replace(",", "."))
            except ValueError: pass
    return out
TIPO_CONF    = _parse_map("TIPO_CONF", {"arma_fogo": 0.65, "arma_branca": 0.30, "fogo": 0.60, "placa": 0.50,
                                        "pessoa": 0.50, "veiculo": 0.50, "animal": 0.45, "epi": 0.45})
# limiar extra POR CLASSE do modelo: alavanca de cadeira = "Shotgun 84%" (frame-prova);
# arma LONGA em ambiente interno exige quase-certeza — pistola (handgun) continua no limiar do tipo
CLASS_CONF   = _parse_map("CLASS_CONF", {"shotgun": 0.88, "shot-gun": 0.88, "rifle": 0.85, "submachine-gun": 0.85})
TIPO_PERSIST = {k: int(v) for k, v in _parse_map("TIPO_PERSIST",
                {"arma_fogo": 5, "arma_branca": 2, "fogo": 4, "placa": 3}).items()}

# aquecimento: ignora deteccoes nos primeiros segundos apos (re)conectar o pipeline —
# frames corrompidos de inicio de stream viram blocos coloridos que o modelo le como
# fogo/arma (fogo 99% disparava exatamente no momento do re-sync)
WARMUP_SEG = float(os.getenv("WARMUP_SEG", "12").replace(",", "."))
_pipe_started = 0.0
COOLDOWN_SEG   = int(os.getenv("COOLDOWN_SEG", "300"))
MAX_FPS        = float(os.getenv("MAX_FPS", "4").replace(",", "."))
# modelos EXTRA (fogo, faca, placa) inferem em fps MENOR: fogo/faca/placa nao mudam em
# 0.25s como arma/movimento -> metade do custo de inferencia por camera, ajuda em escala
MAX_FPS_EXTRA  = float(os.getenv("MAX_FPS_EXTRA", "2").replace(",", "."))
SYNC_INTERVAL  = int(os.getenv("SYNC_INTERVAL", "300"))
VERIFY_COOLDOWN = int(os.getenv("VERIFY_COOLDOWN", "20"))   # nao re-chama Gemini p/ mesma cam+tipo antes disso (anti-spam)
HIGH_CONF       = float(os.getenv("HIGH_CONF", "0.80").replace(",", "."))  # YOLO acima disso alerta sem Gemini
PERSIST_N       = int(os.getenv("PERSIST_N", "4"))          # precisao: exige N deteccoes do mesmo tipo...
PERSIST_JANELA  = float(os.getenv("PERSIST_JANELA", "3").replace(",", "."))  # ...dentro de T segundos (anti-flicker)

CLASS_MAP = {
    "gun":"arma_fogo","pistol":"arma_fogo","rifle":"arma_fogo","weapon":"arma_fogo",
    "handgun":"arma_fogo","firearm":"arma_fogo","shot-gun":"arma_fogo","shotgun":"arma_fogo",
    "submachine-gun":"arma_fogo","gunmen":"arma_fogo","arma":"arma_fogo",
    "knife":"arma_branca","knife_attacker":"arma_branca","faca":"arma_branca",
    "knives":"arma_branca",   # classe do knives-detection/1 (modelo dedicado de faca)
    "fire":"fogo","smoke":"fogo","fogo":"fogo","fumaca":"fogo","fumaça":"fogo",
    "license_plate":"placa","license-plate":"placa","licence_plate":"placa",
    "plate":"placa","number-plate":"placa","numberplate":"placa","placa":"placa",
    "vehicle-registration-plate":"placa","registration-plate":"placa",
    # --- deteccao geral (COCO / yolov8s-640) ---
    "person":"pessoa",
    "car":"veiculo","truck":"veiculo","bus":"veiculo","motorcycle":"veiculo","motorbike":"veiculo","bicycle":"veiculo","train":"veiculo",
    "dog":"animal","cat":"animal","horse":"animal","cow":"animal","sheep":"animal","bird":"animal","elephant":"animal","bear":"animal","zebra":"animal","giraffe":"animal",
    # --- EPI (ppes-kaxsi/8): SO as classes de AUSENCIA geram alerta ---
    "no_helmet":"epi","no_glove":"epi","no_goggles":"epi","no_mask":"epi","no_shoes":"epi",
}
TIPOS_ATIVOS = set(t.strip() for t in os.getenv("TIPOS_ATIVOS", "arma_fogo,arma_branca,fogo,movimento").split(","))
# tipos que NAO passam pelo Gemini (deteccao de objeto/EPI: YOLO ja confiavel; evita custo/ruido)
TIPOS_SEM_GEMINI = set(t.strip() for t in os.getenv("TIPOS_SEM_GEMINI", "pessoa,veiculo,animal,epi").split(",") if t.strip())

# classes do modelo IGNORADAS: "smoke" do fire-smoke-yolov8 marca cabelo/pele como
# fumaca a 99% em ambiente interno (verificado com frame real) — so a classe Fire presta
CLASSES_IGNORADAS = set(x.strip().lower() for x in os.getenv("CLASSES_IGNORADAS", "smoke").split(",") if x.strip())

# --- modelos extras (fogo, placa, faca): cada um roda num processo FILHO ---
MODEL_ID_PLATE = os.getenv("MODEL_ID_PLATE", "")   # placa (opt-in por camera: cam.ia_placa)
MODEL_ID_KNIFE = os.getenv("MODEL_ID_KNIFE", "")   # faca dedicado (o weapon-yolov8 e cego p/ faca: 33% em foto clara)
MODEL_ID_GENERAL = os.getenv("MODEL_ID_GENERAL", "yolov8s-640")  # COCO: pessoa/veiculo/animal/bolsa
MODEL_ID_EPI     = os.getenv("MODEL_ID_EPI", "ppes-kaxsi/8")     # EPI: no_helmet/no_glove/no_goggles/no_mask/no_shoes
EXTRA_MODELS   = [m for m in [MODEL_ID_FIRE, MODEL_ID_PLATE, MODEL_ID_KNIFE] if m]

# se ha modelo de placa configurado, 'placa' PRECISA estar em TIPOS_ATIVOS senao o filtro
# do _process descarta toda deteccao de placa (o opt-in real e por camera: cam.ia_placa).
if MODEL_ID_PLATE and "placa" not in TIPOS_ATIVOS:
    TIPOS_ATIVOS.add("placa")
    print("[cfg] MODEL_ID_PLATE setado -> 'placa' injetada em TIPOS_ATIVOS (opt-in real = cam.ia_placa)")

# --- detecao de MOVIMENTO (frame-diff, sempre ativa, so no processo pai) ---
IS_PARENT       = os.getenv("_CHILD") != "1"
MOTION_ATIVO    = "movimento" in TIPOS_ATIVOS
MOTION_W        = int(os.getenv("MOTION_W", "160"))     # resolucao reduzida p/ o diff (rapido)
MOTION_H        = int(os.getenv("MOTION_H", "90"))
MOTION_DELTA    = int(os.getenv("MOTION_DELTA", "22"))  # sensib. por pixel (0-255)
MOTION_FRAC     = float(os.getenv("MOTION_FRAC", "0.045").replace(",", "."))  # % da cena que mudou p/ contar
MOTION_COOLDOWN = int(os.getenv("MOTION_COOLDOWN", "180"))  # 1 alerta de movimento / cam a cada Xs
_prev_gray = {}   # camera_id -> frame cinza reduzido anterior

cam_by_idx = {}    # source_id -> camera dict
ultimo = {}        # (camera_id, tipo) -> ts do ultimo ALERTA confirmado
ultima_verif = {}  # (camera_id, tipo) -> ts da ultima chamada ao Gemini (anti-spam)
deteccoes_recentes = {}  # (camera_id, tipo) -> deque de ts (anti-flicker / persistencia)

# MEMORIA DE REJEICAO POR REGIAO: objeto fixo da cena (ex.: caixa branca na mesa que o
# modelo ve como faca 71%) e rejeitado pelo Gemini UMA vez e a regiao fica suprimida —
# senao ele "rouba" a verificacao pra sempre e a ameaca REAL (que se move) nunca e checada.
GEMINI_REJ_TTL  = int(os.getenv("GEMINI_REJ_TTL", "1800"))    # regiao rejeitada dorme 30min
GEMINI_REJ_RAIO = float(os.getenv("GEMINI_REJ_RAIO", "150"))  # raio (px) da regiao
rejeitados = {}    # (camera_id, tipo) -> [(x, y, ts), ...]

def _rejeitado_perto(cam_id, tipo, px, py, now):
    for rx, ry, rts in rejeitados.get((cam_id, tipo), []):
        if now - rts < GEMINI_REJ_TTL and (px-rx)**2 + (py-ry)**2 < GEMINI_REJ_RAIO**2:
            return True
    return False

# desenho da caixa vermelha na evidencia (Opcao A)
box_ann = sv.BoxAnnotator(color=sv.Color(r=255, g=0, b=0), thickness=4)
lbl_ann = sv.LabelAnnotator(text_scale=0.7, text_thickness=2)


# termos HUMANOS pro Gemini (e instrucao de julgar PRESENCA, nao intencao/perigo)
TIPO_TERMO = {
    "arma_fogo":   "uma ARMA DE FOGO (pistola, revolver, espingarda ou fuzil)",
    "arma_branca": "uma FACA ou outro objeto cortante (arma branca)",
    "fogo":        "FOGO / chamas reais",
    "placa":       "uma PLACA veicular",
}

# ---- Circuit breaker do Gemini (item 1) --------------------------------------------
# Antes: quando o verificador caia (quota 429 / rede), o fail-open transformava CADA
# deteccao de arma/faca em alerta -> enxurrada. Agora: numa falha SISTEMICA (um 429 ou
# N falhas seguidas) o circuito ABRE, o detector PARA de chamar a API morta e devolve
# INDEFINIDO (None) -> o _process poe em QUARENTENA (grava sem WhatsApp). Fecha sozinho
# quando o Gemini volta (chamada-sonda apos o cooldown). Loga a transicao UMA vez, nao 300.
# Falha ISOLADA (circuito fechado) mantem o comportamento do dev: fogo cai, arma/faca
# fail-open (um blip transitorio nao deve perder uma ameaca real).
GEMINI_CB_FAILS    = int(os.getenv("GEMINI_CB_FAILS", "3"))
GEMINI_CB_COOLDOWN = int(os.getenv("GEMINI_CB_COOLDOWN", "120"))
_gem_fails = 0
_gem_open_until = 0.0
_gem_down = False   # estado atual do circuito (p/ logar a transicao so 1x)


def gemini_confirma(jpg, nome, tipo, jpg_crop=None):
    # retorno: (True=confirmado | False=rejeitado/cai | None=INDEFINIDO -> quarentena)
    if not USE_GEMINI or not GEMINI_KEY:
        return True, f"{tipo} detectado"
    global _gem_fails, _gem_open_until, _gem_down
    _now = time.time()
    if _gem_open_until and _now < _gem_open_until:
        # circuito ABERTO: nao chama a API morta. fogo cai (fail-closed); arma/faca -> quarentena
        return (False if tipo == "fogo" else None), f"{tipo} nao verificado (gemini fora do ar)"
    b64 = base64.b64encode(jpg).decode()
    termo = TIPO_TERMO.get(tipo, tipo)
    # reforco anti falso-positivo de FOGO: o modelo marca qualquer coisa vermelha como fogo
    reforco = ("" if tipo != "fogo" else
               " CRITICO p/ FOGO: confirme SOMENTE se houver CHAMAS visiveis ou FUMACA de um "
               "incendio REAL. Objeto/parede/roupa/telhado/luz/LED/placa vermelha, reflexo, "
               "lampada, por-do-sol ou logo colorido NAO sao fogo -> responda false.")
    prompt = (f'Camera de seguranca "{nome}". O detector de objetos marcou {termo} '
              'na regiao da CAIXA VERMELHA da 1a imagem'
              + (' (a 2a imagem e o RECORTE AMPLIADO dessa regiao — examine-a com atencao)' if jpg_crop is not None else '')
              + '. Julgue apenas a PRESENCA do objeto, NAO a intencao ou perigo: mesmo em cena '
                'calma ou aparentando teste, se o objeto estiver visivel, confirme.'
              + reforco
              + ' Responda SO JSON: '
                '{"confirmado": true/false, "descricao": "o que ve em 1 frase"}')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    parts = [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]
    if jpg_crop is not None:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpg_crop).decode()}})
    try:
        r = requests.post(url, timeout=20, json={
            "contents": [{"parts": parts}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
        if r.status_code == 429:
            raise RuntimeError("quota 429 (RESOURCE_EXHAUSTED)")
        d = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        if _gem_down:
            print("[gemini] verificador VOLTOU — circuito fechado", flush=True)
        _gem_fails = 0; _gem_open_until = 0.0; _gem_down = False   # sucesso fecha o circuito
        return bool(d.get("confirmado")), d.get("descricao", f"{tipo} detectado")
    except Exception as e:
        _gem_fails += 1
        sistemico = ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS
        if sistemico:
            _gem_open_until = _now + GEMINI_CB_COOLDOWN
            if not _gem_down:
                _gem_down = True
                print(f"[gemini] FORA DO AR ({str(e)[:80]}) — circuito aberto por {GEMINI_CB_COOLDOWN}s; "
                      f"arma/faca -> QUARENTENA (grava sem WhatsApp), fogo cai", flush=True)
            # falha SISTEMICA: fogo fail-closed (cai); arma/faca -> INDEFINIDO (quarentena)
            return (False if tipo == "fogo" else None), f"{tipo} nao verificado"
        # falha ISOLADA (circuito ainda fechado): comportamento original do dev —
        # fogo fail-closed; arma/faca fail-open (blip transitorio nao deve perder ameaca)
        print("[gemini] erro (isolado):", e)
        if tipo == "fogo":
            return False, f"{tipo} nao verificado"
        return True, f"{tipo} detectado"


def gemini_balaclava(crop_jpg, nome):
    """True se a pessoa esta com o ROSTO COBERTO (balaclava/touca ninja/capacete integral).
    Fail-closed: sem Gemini / erro / circuito aberto -> False (nao alerta, evita falso alarme)."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return False, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return False, ""
    prompt = ('Camera de seguranca "' + str(nome) + '". A imagem e o RECORTE da cabeca/rosto de uma pessoa. '
              'A pessoa esta com o ROSTO COBERTO para ocultar a identidade — balaclava (touca ninja), '
              'mascara de esqui, capuz com mascara, ou CAPACETE INTEGRAL fechado? '
              'NAO conte como coberto: oculos/oculos escuros, boné/chapeu simples, mascara cirurgica comum, '
              'capuz sem mascara, ou rosto normal visivel. Responda true SOMENTE se a maior parte do rosto '
              'estiver OCULTA de forma proposital. Responda SO JSON: {"coberto": true/false, "descricao": "1 frase"}')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        r = requests.post(url, timeout=20, json={
            "contents": [{"parts": [{"text": prompt},
                          {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(crop_jpg).decode()}}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
        if r.status_code == 429:
            raise RuntimeError("429")
        d = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        _gem_fails = 0; _gem_open_until = 0.0; _gem_down = False
        return bool(d.get("coberto")), d.get("descricao", "rosto coberto")
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return False, ""


def gemini_piscina(crop_jpg, nome, still_secs=0):
    """True se ha sinal REAL de afogamento/perigo na area da agua. Fail-closed.
    AUXILIO: nao substitui salva-vidas/vigilancia; sujeito a falso negativo."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return False, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return False, ""
    ctx = (" A pessoa aparenta estar IMOVEL/parada na agua ha cerca de %d segundos." % int(still_secs)) if still_secs >= 20 else ""
    prompt = ('Camera de seguranca de uma PISCINA; a imagem e a AREA DA AGUA.' + ctx +
              ' Ha alguem em POSSIVEL AFOGAMENTO ou perigo? Sinais: pessoa BOIANDO IMOVEL ou de bruços, '
              'corpo/vulto parado sob a agua (submerso), ou pessoa se DEBATENDO com dificuldade de manter a cabeca fora. '
              'NAO alarme para: gente nadando normal, boiando ATIVA/se mexendo, sentada na borda, brincando, ou agua vazia. '
              'Responda true SOMENTE com sinal REAL de perigo. Responda SO JSON: {"perigo": true/false, "descricao": "1 frase"}')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        r = requests.post(url, timeout=20, json={
            "contents": [{"parts": [{"text": prompt},
                          {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(crop_jpg).decode()}}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
        if r.status_code == 429:
            raise RuntimeError("429")
        d = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        _gem_fails = 0; _gem_open_until = 0.0; _gem_down = False
        return bool(d.get("perigo")), d.get("descricao", "possivel afogamento")
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return False, ""


def envia_alerta(cam, tipo, conf, desc, imagem_b64=None, verificado=True):
    payload = {
        "secret": WEBHOOK_SECRET,
        "camera_nome": cam["nome"],
        "camera_id": cam.get("id", ""),
        "cliente_id": cam.get("cliente_id", ""),
        "cliente_nome": cam.get("cliente_nome", ""),
        "cliente_telefone": cam.get("cliente_telefone", ""),
        "tipo": tipo,
        "descricao": f"[IA] {desc}",
        "confianca": int(conf * 100),
        "verificado": verificado,   # item 1: False = quarentena (grava sem WhatsApp/push)
    }
    if imagem_b64:
        payload["imagem_base64"] = imagem_b64   # frame com a caixa vermelha (Opcao A)
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        print(f"[ALERTA] {cam['nome']} | {tipo} {int(conf*100)}% -> HTTP {r.status_code}")
    except Exception as e:
        print("[webhook] erro:", e)


def _motion_check(cam, frame_bgr, now):
    """Detecao de movimento por diferenca de frames (barata, sem YOLO). Sempre ativa."""
    cid = cam.get("id")
    try:
        gray = cv2.resize(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), (MOTION_W, MOTION_H))
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
    except Exception:
        return
    prev = _prev_gray.get(cid)
    _prev_gray[cid] = gray
    if prev is None:
        return
    diff = cv2.absdiff(prev, gray)
    _, th = cv2.threshold(diff, MOTION_DELTA, 255, cv2.THRESH_BINARY)
    frac = float((th > 0).sum()) / th.size    # fracao da cena que mudou
    if frac < MOTION_FRAC:
        return
    k = (cid, "movimento")
    if now - ultimo.get(k, 0) < MOTION_COOLDOWN:   # 1 alerta de movimento por cam a cada Xs
        return
    ultimo[k] = now
    imagem_b64 = None
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr)
        if ok:
            imagem_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    conf = min(0.99, 0.5 + frac)
    envia_alerta(cam, "movimento", conf, f"Movimento detectado ({int(frac*100)}% da cena)", imagem_b64)


# ---- gating por camera (tela "Analiticos por Camera" -> entidade ConfigAnalitico) ----
# o server (/listarCamerasIA) anexa cam["config_analitico"]; aqui decidimos, por camera e por
# horario, se o 'tipo' detectado vira alerta. SEM config = NADA roda (decisao do produto).
_CFG_ALIASES = {
    "arma_fogo":   ("arma_fogo", "arma"),
    "arma_branca": ("arma_branca", "arma", "faca"),
    "fogo":        ("fogo", "fogo_fumaca", "fumaca", "incendio"),
    "movimento":   ("movimento",),
    "placa":       ("placa",),
    "pessoa":      ("pessoa",),
    "veiculo":     ("veiculo", "veiculos", "carro"),
    "animal":      ("animal", "animais"),
    "epi":         ("epi",),
    "intruso":     ("intruso", "zona", "zona_intrusao"),
    "linha":       ("linha", "linha_virtual"),
    "heatmap":     ("heatmap", "mapa_calor"),
    "toca_ninja":  ("toca_ninja", "balaclava", "capacete_ninja"),
    "piscina":     ("piscina", "afogamento"),
}

# vocabulario da tela -> model_id(s) necessarios (carga/execucao ON-DEMAND por camera)
_VOCAB_MODEL = {
    "arma": (MODEL_ID, MODEL_ID_KNIFE), "arma_fogo": (MODEL_ID,),
    "arma_branca": (MODEL_ID_KNIFE,), "faca": (MODEL_ID_KNIFE,),
    "fogo": (MODEL_ID_FIRE,), "fogo_fumaca": (MODEL_ID_FIRE,), "fumaca": (MODEL_ID_FIRE,),
    "placa": (MODEL_ID_PLATE,),
    "pessoa": (MODEL_ID_GENERAL,), "veiculo": (MODEL_ID_GENERAL,), "veiculos": (MODEL_ID_GENERAL,),
    "animal": (MODEL_ID_GENERAL,), "animais": (MODEL_ID_GENERAL,),
    "epi": (MODEL_ID_EPI,),
    "intruso": (MODEL_ID_GENERAL,), "linha": (MODEL_ID_GENERAL,),  # zona/linha usam COCO (pessoa)
    "heatmap": (MODEL_ID_GENERAL,),  # mapa de calor usa COCO (pessoa)
    "toca_ninja": (MODEL_ID_GENERAL,),  # balaclava usa COCO (pessoa) + Gemini
    "piscina": (MODEL_ID_GENERAL,),  # afogamento usa COCO (pessoa) + Gemini
}


def models_for_cam(cam, now_ts=None):
    """Conjunto de model_ids que ESTA camera precisa AGORA (base: config + horario).
    Sem config / fora do horario com padrao vazio -> conjunto vazio (nada carrega/roda p/ ela)."""
    ativos = _analiticos_ativos_cam(cam, now_ts if now_ts is not None else time.time())
    if not ativos:
        return set()
    mids = set()
    for a in ativos:
        for mid in _VOCAB_MODEL.get(a, ()):
            if mid:
                mids.add(mid)
    return mids


_EPI_ITEM = {"no_helmet": "sem capacete", "no_glove": "sem luva", "no_goggles": "sem oculos",
             "no_mask": "sem mascara", "no_shoes": "sem calcado"}
_LABEL_TIPO = {"pessoa": "Pessoa detectada", "veiculo": "Veiculo detectado", "animal": "Animal detectado"}


def _desc_tipo(tipo, conf, p=None):
    pct = int(conf * 100)
    if tipo == "epi":
        cls = str((p or {}).get("class", "")).lower()
        return "EPI: %s (%d%%)" % (_EPI_ITEM.get(cls, "ausente"), pct)
    if tipo in _LABEL_TIPO:
        return "%s (%d%%)" % (_LABEL_TIPO[tipo], pct)
    return "%s (%d%%)" % (tipo, pct)


def _analiticos_ativos_cam(cam, now_ts):
    """Set de analiticos (vocabulario da tela) ativos AGORA para a camera.
    None => a camera NAO deve rodar nada (sem config ou config inativa)."""
    cfg = cam.get("config_analitico")
    if not cfg or not cfg.get("ativo", True):
        return None
    lt = time.localtime(now_ts)
    iso = lt.tm_wday + 1                 # 1=seg..7=dom
    dias_hoje = {iso, iso % 7}           # cobre convencao ISO (1=seg) e JS (dom=0)
    hhmm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
    for h in (cfg.get("horarios") or []):
        if dias_hoje & set(h.get("dias") or []):
            if (h.get("hora_inicio") or "00:00") <= hhmm <= (h.get("hora_fim") or "23:59"):
                return set(h.get("analiticos") or [])
    return set(cfg.get("analiticos_padrao") or [])


def _tipo_ativo_na_cam(cam, tipo, now_ts):
    ativos = _analiticos_ativos_cam(cam, now_ts)
    if ativos is None:
        return False
    return any(a in ativos for a in _CFG_ALIASES.get(tipo, (tipo,)))


# ---------- ZONA DE INTRUSAO / LINHA VIRTUAL (por presenca de PESSOA; sem rastreio) ----------
_zona_ultimo = {}   # (cam_id, "zona:<nome>") -> ts do ultimo alerta
ZONA_COOLDOWN  = int(os.getenv("ZONA_COOLDOWN", "60"))       # 1 alerta por zona/cam a cada Xs
ZONA_LINHA_TOL = float(os.getenv("ZONA_LINHA_TOL", "0.035").replace(",", "."))  # dist. (frac) p/ "pisar" na linha


def _pt_in_poly(x, y, poly):
    inside = False; n = len(poly); j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]; xj, yj = poly[j][0], poly[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _dist_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _zona_alerta(cam, desc, z, pred, frame_bgr, now, nome):
    k = (cam.get("id"), "zona:" + nome)
    if now - _zona_ultimo.get(k, 0) < ZONA_COOLDOWN:
        return
    _zona_ultimo[k] = now
    img_b64 = None
    try:
        import numpy as _np
        an = frame_bgr.copy(); H, W = an.shape[:2]
        arr = [(int(px * W), int(py * H)) for px, py in (z.get("pontos") or [])]
        if z.get("tipo") == "zona" and len(arr) >= 3:
            cv2.polylines(an, [_np.array(arr, dtype=_np.int32)], True, (0, 0, 255), 3)
        elif len(arr) >= 2:
            cv2.line(an, arr[0], arr[1], (0, 0, 255), 3)
        x = float(pred.get("x", 0)); y = float(pred.get("y", 0)); w = float(pred.get("width", 0)); h = float(pred.get("height", 0))
        cv2.rectangle(an, (int(x - w / 2), int(y - h / 2)), (int(x + w / 2), int(y + h / 2)), (0, 0, 255), 2)
        ok, buf = cv2.imencode(".jpg", an)
        if ok:
            img_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception as e:
        print("[zona] draw:", e)
    print(f"[zona] {cam.get('nome','')}: {desc}")
    envia_alerta(cam, "intruso", float(pred.get("confidence", 0.9)), desc, img_b64, verificado=True)


def _zona_check(cam, predictions, frame_bgr, now):
    """Se a camera tem zonas/linhas e o analitico esta ativo, alerta quando uma PESSOA
    esta dentro da zona (poligono) ou sobre a linha (faixa)."""
    cfg = cam.get("config_analitico") or {}
    zonas = cfg.get("zonas_intrusao") or []
    if not zonas or frame_bgr is None:
        return
    intruso_on = _tipo_ativo_na_cam(cam, "intruso", now)
    linha_on = _tipo_ativo_na_cam(cam, "linha", now)
    if not (intruso_on or linha_on):
        return
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    persons = _person_pts(preds, W, H)
    if not persons:
        return
    for z in zonas:
        tz = z.get("tipo", "zona"); pts = z.get("pontos") or []; nome = z.get("nome") or tz
        if tz == "zona" and intruso_on and len(pts) >= 3:
            hit = next((pp for (fx, fy, pp) in persons if _pt_in_poly(fx, fy, pts)), None)
            if hit is not None:
                _zona_alerta(cam, "Intruso na zona '%s'" % nome, z, hit, frame_bgr, now, nome)
        elif tz == "linha" and linha_on and len(pts) >= 2:
            (ax, ay), (bx, by) = pts[0], pts[1]
            hit = next((pp for (fx, fy, pp) in persons if _dist_seg(fx, fy, ax, ay, bx, by) <= ZONA_LINHA_TOL), None)
            if hit is not None:
                _zona_alerta(cam, "Cruzou a linha '%s'" % nome, z, hit, frame_bgr, now, nome)


def _person_pts(preds, W, H):
    """Pontos-pe (base do box) das PESSOAS detectadas, normalizados (0-1)."""
    out = []
    for p in preds:
        if CLASS_MAP.get(str(p.get("class", "")).lower()) != "pessoa":
            continue
        if float(p.get("confidence", 0)) < TIPO_CONF.get("pessoa", CONF_MIN):
            continue
        fx = float(p.get("x", 0)) / (W or 1)
        fy = (float(p.get("y", 0)) + float(p.get("height", 0)) / 2.0) / (H or 1)
        out.append((fx, fy, p))
    return out


# ---------- MAPA DE CALOR (acumula posicoes de pessoa numa grade; balde por hora) ----------
HEAT_W = int(os.getenv("HEAT_W", "48")); HEAT_H = int(os.getenv("HEAT_H", "27"))
HEAT_FLUSH = int(os.getenv("HEAT_FLUSH", "90"))     # envia o acumulo ao servidor a cada Xs
_HEAT_BASE = (CAMERAS_URL.rsplit("/", 1)[0] if "CAMERAS_URL" in dir() and CAMERAS_URL else "")
HEATMAP_URL = os.getenv("HEATMAP_URL", (_HEAT_BASE + "/api/comercial/heatmap/ingest") if _HEAT_BASE else "")
_heat = {}   # cam_id -> {"bucket": "YYYYMMDDHH", "grid": [ints], "last_flush": ts}


def _heat_flush(cam_id, st):
    if not HEATMAP_URL or not any(st["grid"]):
        st["last_flush"] = time.time(); return
    try:
        requests.post(HEATMAP_URL, json={"secret": WEBHOOK_SECRET, "camera_id": cam_id, "bucket": st["bucket"],
                      "gw": HEAT_W, "gh": HEAT_H, "grid": st["grid"]}, timeout=4)
        st["grid"] = [0] * (HEAT_W * HEAT_H)
    except Exception as e:
        print("[heat] flush:", str(e)[:90])
    st["last_flush"] = time.time()


def _heat_add(cam, predictions, frame_bgr, now):
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "heatmap", now):
        return
    cid = cam.get("id")
    bucket = time.strftime("%Y%m%d%H", time.localtime(now))
    st = _heat.get(cid)
    if st is None or st["bucket"] != bucket:
        if st is not None:
            _heat_flush(cid, st)
        st = {"bucket": bucket, "grid": [0] * (HEAT_W * HEAT_H), "last_flush": now}; _heat[cid] = st
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    area = None
    for z in (cam.get("config_analitico") or {}).get("zonas_intrusao", []) or []:
        if z.get("tipo") == "heatmap" and len(z.get("pontos") or []) >= 3:
            area = z["pontos"]; break
    for (fx, fy, p) in _person_pts(preds, W, H):
        if area and not _pt_in_poly(fx, fy, area):
            continue
        gx = min(HEAT_W - 1, max(0, int(fx * HEAT_W))); gy = min(HEAT_H - 1, max(0, int(fy * HEAT_H)))
        st["grid"][gy * HEAT_W + gx] += 1
    if now - st["last_flush"] >= HEAT_FLUSH:
        _heat_flush(cid, st)


# ---------- BALACLAVA / TOCA NINJA (COCO pessoa -> recorte da cabeca -> Gemini) ----------
_bala_check = {}    # cam_id -> ts do ultimo check Gemini
_bala_ultimo = {}   # cam_id -> ts do ultimo alerta
BALA_CHECK_SEC = int(os.getenv("BALA_CHECK_SEC", "25"))   # intervalo minimo entre checks Gemini/cam
BALA_COOLDOWN  = int(os.getenv("BALA_COOLDOWN", "120"))   # cooldown do alerta/cam


def _balaclava_check(cam, predictions, frame_bgr, now):
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "toca_ninja", now):
        return
    cid = cam.get("id")
    if now - _bala_check.get(cid, 0) < BALA_CHECK_SEC:   # throttle p/ nao gastar Gemini
        return
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    pts = _person_pts(preds, W, H)
    if not pts:
        return
    _bala_check[cid] = now
    p = max(pts, key=lambda t: float(t[2].get("width", 0)) * float(t[2].get("height", 0)))[2]  # maior pessoa
    x = float(p.get("x", 0)); y = float(p.get("y", 0)); w = float(p.get("width", 0)); h = float(p.get("height", 0))
    x1 = int(max(0, x - w * 0.55)); x2 = int(min(W, x + w * 0.55))
    y1 = int(max(0, y - h * 0.55)); y2 = int(min(H, y - h * 0.05))   # topo ~45% do box = cabeca
    if x2 - x1 < 24 or y2 - y1 < 24:
        return
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr[y1:y2, x1:x2])
        if not ok:
            return
        coberto, desc = gemini_balaclava(buf.tobytes(), cam.get("nome", ""))
    except Exception as e:
        print("[bala] erro:", e); return
    if not coberto or (now - _bala_ultimo.get(cid, 0) < BALA_COOLDOWN):
        return
    _bala_ultimo[cid] = now
    img_b64 = None
    try:
        an = frame_bgr.copy()
        cv2.rectangle(an, (int(x - w / 2), int(y - h / 2)), (int(x + w / 2), int(y + h / 2)), (0, 0, 255), 2)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    print(f"[bala] {cam.get('nome','')}: rosto coberto - {desc}")
    envia_alerta(cam, "toca_ninja", float(p.get("confidence", 0.9)),
                 "Pessoa com rosto coberto (touca ninja/capacete): " + (desc or ""), img_b64, verificado=True)


# ---------- PISCINA / AFOGAMENTO (AUXILIO): pessoa na agua imovel + Gemini na zona da agua ----------
_pisc = {}          # cam_id -> {"last_check":ts,"centroid":(fx,fy),"still_since":ts}
_pisc_ultimo = {}   # cam_id -> ts do ultimo alerta
PISCINA_CHECK_SEC = int(os.getenv("PISCINA_CHECK_SEC", "20"))   # intervalo entre checks Gemini
PISCINA_STILL     = int(os.getenv("PISCINA_STILL", "30"))       # imovel por Xs -> antecipa o check
PISCINA_COOLDOWN  = int(os.getenv("PISCINA_COOLDOWN", "60"))
PISCINA_MOVE_TOL  = float(os.getenv("PISCINA_MOVE_TOL", "0.05").replace(",", "."))


def _bbox_poly(poly, W, H):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return (int(max(0, min(xs) * W)), int(max(0, min(ys) * H)), int(min(W, max(xs) * W)), int(min(H, max(ys) * H)))


def _piscina_check(cam, predictions, frame_bgr, now):
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "piscina", now):
        return
    agua = None
    for z in (cam.get("config_analitico") or {}).get("zonas_intrusao", []) or []:
        if z.get("tipo") == "agua" and len(z.get("pontos") or []) >= 3:
            agua = z["pontos"]; break
    if not agua:
        return
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    inwater = [(fx, fy, p) for (fx, fy, p) in _person_pts(preds, W, H) if _pt_in_poly(fx, fy, agua)]
    cid = cam.get("id"); st = _pisc.get(cid) or {}
    if not inwater:
        _pisc[cid] = {"last_check": st.get("last_check", 0), "centroid": None, "still_since": now}
        return
    cx = sum(t[0] for t in inwater) / len(inwater); cy = sum(t[1] for t in inwater) / len(inwater)
    if st.get("centroid"):
        moved = ((cx - st["centroid"][0]) ** 2 + (cy - st["centroid"][1]) ** 2) ** 0.5
        still_since = now if moved > PISCINA_MOVE_TOL else st.get("still_since", now)
    else:
        still_since = now
    still = now - still_since
    last_check = st.get("last_check", 0)
    _pisc[cid] = {"last_check": last_check, "centroid": (cx, cy), "still_since": still_since}
    if not ((now - last_check >= PISCINA_CHECK_SEC) or (still >= PISCINA_STILL and now - last_check >= 8)):
        return
    _pisc[cid]["last_check"] = now
    x1, y1, x2, y2 = _bbox_poly(agua, W, H)
    if x2 - x1 < 24 or y2 - y1 < 24:
        return
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr[y1:y2, x1:x2])
        if not ok:
            return
        perigo, desc = gemini_piscina(buf.tobytes(), cam.get("nome", ""), still)
    except Exception as e:
        print("[piscina] erro:", e); return
    if not perigo or (now - _pisc_ultimo.get(cid, 0) < PISCINA_COOLDOWN):
        return
    _pisc_ultimo[cid] = now
    img_b64 = None
    try:
        import numpy as _np
        an = frame_bgr.copy()
        cv2.polylines(an, [_np.array([(int(px * W), int(py * H)) for px, py in agua], dtype=_np.int32)], True, (255, 0, 0), 3)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    print(f"[piscina] {cam.get('nome','')}: possivel afogamento - {desc}")
    envia_alerta(cam, "afogamento", 0.9, "PISCINA (auxilio): possivel afogamento - " + (desc or ""), img_b64, verificado=True)


def _process(predictions, video_frame):
    idx = getattr(video_frame, "source_id", 0) or 0
    cam = cam_by_idx.get(idx)
    if not cam:
        return

    # AQUECIMENTO: frames corrompidos no inicio do stream geram falso fogo/arma/movimento
    if _pipe_started and time.time() - _pipe_started < WARMUP_SEG:
        return

    # ZONA/LINHA: checa presenca de pessoa dentro da zona/linha (roda so nas preds do COCO)
    try:
        _zona_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[zona] erro:", e)
    # MAPA DE CALOR: acumula posicoes de pessoa (roda so nas preds do COCO)
    try:
        _heat_add(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[heat] erro:", e)
    # BALACLAVA/TOCA NINJA: recorte da cabeca da pessoa -> Gemini (throttle por camera)
    try:
        _balaclava_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[bala] erro:", e)
    # PISCINA/AFOGAMENTO (auxilio): pessoa imovel na agua + Gemini na zona
    try:
        _piscina_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[piscina] erro:", e)

    # MOVIMENTO: roda por frame, so no processo pai (evita duplicar nos filhos fogo/placa)
    if IS_PARENT and MOTION_ATIVO:
        try:
            _motion_check(cam, video_frame.image, time.time())
        except Exception as e:
            print("[motion] erro:", e)
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    melhor = None
    melhor_p = None   # a predicao vencedora (box) — usada no recorte ampliado pro Gemini
    _agora = time.time()
    for p in preds:
        cls = str(p.get("class", "")).lower()
        if cls in CLASSES_IGNORADAS:
            continue
        tipo = CLASS_MAP.get(cls)
        conf = float(p.get("confidence", 0))
        # limiar POR TIPO (arma_fogo 65%, faca 30%) e POR CLASSE (shotgun/rifle 88% — cadeira!)
        minimo = max(TIPO_CONF.get(tipo, CONF_MIN), CLASS_CONF.get(cls, 0.0))
        if not (tipo and tipo in TIPOS_ATIVOS and conf >= minimo):
            continue
        # honra a tela "Analiticos por Camera": sem config / fora do horario -> nao alerta
        if not _tipo_ativo_na_cam(cam, tipo, _agora):
            continue
        # regiao ja rejeitada pelo Gemini (objeto fixo)? nao deixa "roubar" a verificacao
        if _rejeitado_perto(cam.get("id"), tipo, float(p.get("x", 0)), float(p.get("y", 0)), _agora):
            continue
        if melhor is None or conf > melhor[1]:
            melhor = (tipo, conf); melhor_p = p
    if not melhor:
        return
    tipo, conf = melhor
    now = time.time()
    k = (cam.get("id"), tipo)

    # PLACA: opt-in por camera. Modelo de placa da falso-positivo em padrao retangular
    # (grade de janela, etc.) em cena geral -> so vale em camera apontada p/ nivel de carro.
    if tipo == "placa" and not cam.get("ia_placa"):
        return

    if os.getenv("DEBUG_DET"):
        print(f"[raw] {cam.get('nome','')}: {tipo} {int(conf*100)}%", flush=True)

    # ANTI-FLICKER (precisao): registra a deteccao e so segue se for PERSISTENTE
    # (>= PERSIST_N deteccoes do mesmo tipo dentro de PERSIST_JANELA seg). Mata falso-positivo
    # momentaneo de cena de rua (Times Square) e reduz muito as chamadas ao Gemini.
    dq = deteccoes_recentes.setdefault(k, deque(maxlen=64))
    dq.append(now)
    # persistencia POR TIPO: faca precisa de so 2 hits (passa rapido), arma de 5 (mata flicker)
    if sum(1 for t in dq if now - t <= PERSIST_JANELA) < TIPO_PERSIST.get(tipo, PERSIST_N):
        return

    # anti-spam: nao verifica a mesma cam+tipo de novo tao cedo
    if now - ultima_verif.get(k, 0) < VERIFY_COOLDOWN:
        return
    ultima_verif[k] = now
    # cooldown do alerta ja confirmado
    if now - ultimo.get(k, 0) < COOLDOWN_SEG:
        return

    print(f"[detect] {cam['nome']}: YOLO viu {tipo} {int(conf*100)}%")
    raw = video_frame.image

    # 1) frame ANOTADO (caixa vermelha) — vira a evidencia E orienta o Gemini
    annotated = raw
    try:
        annotated = raw.copy()
        det = sv.Detections.from_inference(predictions)
        labels = [f"{p.get('class','')} {int(float(p.get('confidence',0))*100)}%" for p in preds]
        annotated = box_ann.annotate(annotated, det)
        annotated = lbl_ann.annotate(annotated, det, labels=labels)
    except Exception as e:
        print("[draw] erro ao desenhar caixa:", e)
    ok, buf = cv2.imencode(".jpg", annotated)
    if not ok:
        return

    # 2) RECORTE AMPLIADO da deteccao: em quadro aberto 2K o objeto (faca!) fica pequeno
    #    demais pro Gemini ver — o zoom da regiao resolve
    jpg_crop = None
    try:
        if melhor_p:
            H, W = raw.shape[:2]
            cx, cy = float(melhor_p.get("x", 0)), float(melhor_p.get("y", 0))
            m = max(float(melhor_p.get("width", 0)), float(melhor_p.get("height", 0))) * 1.3 + 80
            x1, y1 = int(max(0, cx - m)), int(max(0, cy - m))
            x2, y2 = int(min(W, cx + m)), int(min(H, cy + m))
            if x2 - x1 > 40 and y2 - y1 > 40:
                okc, bufc = cv2.imencode(".jpg", raw[y1:y2, x1:x2])
                if okc:
                    jpg_crop = bufc.tobytes()
    except Exception as e:
        print("[crop] erro:", e)

    # YOLO muito confiante -> alerta direto; senao Gemini confirma (frame anotado + zoom).
    # EXCECAO: fogo SEMPRE passa pelo Gemini (o modelo marca qualquer vermelho como fogo com
    # confianca alta -> o bypass deixava passar falso-positivo; o Gemini derruba isso).
    if tipo in TIPOS_SEM_GEMINI or (conf >= HIGH_CONF and tipo != "fogo") or not USE_GEMINI:
        veredito, desc = True, _desc_tipo(tipo, conf, melhor_p)
    else:
        veredito, desc = gemini_confirma(buf.tobytes(), cam["nome"], tipo, jpg_crop)
    if veredito is False:   # so a rejeicao EXPLICITA do Gemini (None = indefinido = quarentena)
        print(f"[x] Gemini rejeitou {tipo} em {cam['nome']} ({int(conf*100)}%)")
        # grava a REGIAO rejeitada: objeto fixo dorme por GEMINI_REJ_TTL e deixa de
        # roubar a verificacao — a ameaca real (em outra posicao) passa a ser checada
        try:
            if melhor_p:
                lst = rejeitados.setdefault((cam.get("id"), tipo), [])
                lst.append((float(melhor_p.get("x", 0)), float(melhor_p.get("y", 0)), now))
                del lst[:-32]
                print(f"[x] regiao x={int(melhor_p.get('x',0))} y={int(melhor_p.get('y',0))} suprimida por {GEMINI_REJ_TTL//60}min")
        except Exception:
            pass
        # auditoria: salva o que o Gemini viu e rejeitou (anotado + recorte)
        try:
            if os.getenv("DEBUG_SAVE_REJ", "1") == "1":
                base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alertas_img",
                                    f"_rej_{time.strftime('%H%M%S')}_{tipo}_{int(conf*100)}")
                open(base + "_anot.jpg", "wb").write(buf.tobytes())
                if jpg_crop:
                    open(base + "_crop.jpg", "wb").write(jpg_crop)
        except Exception:
            pass
        return

    # veredito True = confirmado; None = INDEFINIDO (verificador fora do ar) -> QUARENTENA
    verificado = veredito is True
    if not verificado:
        print(f"[quarentena] {tipo} em {cam['nome']} ({int(conf*100)}%) — gemini fora do ar, grava sem WhatsApp")
    # evidencia do alerta = o frame anotado (ja montado antes do Gemini)
    imagem_b64 = base64.b64encode(buf.tobytes()).decode()
    ultimo[k] = now
    envia_alerta(cam, tipo, conf, desc, imagem_b64, verificado=verificado)


_last_frame = time.time()   # heartbeat p/ o watchdog

def sink(predictions, video_frame):
    global _last_frame
    _last_frame = time.time()
    if isinstance(video_frame, list):
        for p, vf in zip(predictions, video_frame):
            if vf is not None:
                _process(p, vf)
    else:
        _process(predictions, video_frame)


def fetch_cameras():
    # so o PAI revalida os streams (validar=True); os filhos consomem sem revalidar (evita 3x)
    r = requests.post(CAMERAS_URL, json={"secret": WEBHOOK_SECRET, "validar": IS_PARENT}, timeout=45)
    data = r.json()
    cams = [c for c in data.get("cameras", []) if c.get("stream_url") and (c.get("stream_valido") or not IS_PARENT)]
    # o filho de PLACA so precisa abrir cameras marcadas como "de entrada" (ia_placa) — economiza RTSP/CPU
    if MODEL_ID_PLATE and MODEL_TO_RUN == MODEL_ID_PLATE:
        cams = [c for c in cams if c.get("ia_placa")]
    return cams


# GPUs disponiveis pro rateio dos processos (parent + filhos) — balanceia carga nas 2 placas
_GPUS = [g.strip() for g in os.getenv("DETECTOR_GPUS", "0,1").split(",") if g.strip()]

def _spawn_child(em, gpu=None):
    env = {**os.environ, "MODEL_TO_RUN": em, "_CHILD": "1"}
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)   # pina o filho numa GPU (rateio)
    return subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env)

# cada modelo EXTRA (fogo, placa, faca) roda num processo FILHO proprio
# (2+ InferencePipeline no mesmo processo travam; processos separados nao)
children = []   # [(model_id, gpu, Popen)] — supervisionados pelo pai
if IS_PARENT and __name__ == "__main__":   # so quando rodado direto (nao ao ser importado pelo detector_nvdec)
    for i, em in enumerate(EXTRA_MODELS):
        g = _GPUS[(i + 1) % len(_GPUS)] if _GPUS else None   # alterna GPUs (parent fica na 1a)
        children.append((em, g, _spawn_child(em, g)))
        print(f"[spawn] filho {em} na GPU {g}")

print(f"Corexia SaaS detector | modelo={MODEL_TO_RUN} | tipos={sorted(TIPOS_ATIVOS)} | "
      f"movimento={MOTION_ATIVO and IS_PARENT} | gemini={USE_GEMINI}")
print(f"Auto-sync a cada {SYNC_INTERVAL}s de: {CAMERAS_URL}\n")

STALL_SEG = int(os.getenv("STALL_SEG", "150"))   # sem frames por tanto tempo -> reinicia
LOOP_SEG  = 20                                    # cadencia do watchdog

pipe = None
current_sig = None
last_fetch = 0
cams = []
while __name__ == "__main__":   # loop principal so no run direto; import (detector_nvdec) reusa so as funcoes
    # supervisiona os filhos (fogo/placa): se algum morreu, respawna
    if IS_PARENT and children:
        for i, (em, g, proc) in enumerate(children):
            if proc.poll() is not None:
                print(f"[spawn] filho {em} (GPU {g}) caiu (rc={proc.returncode}) -> respawnando")
                children[i] = (em, g, _spawn_child(em, g))

    # re-busca a lista de cameras a cada SYNC_INTERVAL (o watchdog roda mais rapido)
    if time.time() - last_fetch >= SYNC_INTERVAL or current_sig is None:
        try:
            cams = fetch_cameras()
            last_fetch = time.time()
        except Exception as e:
            print("[sync] erro ao buscar cameras:", e)
            time.sleep(LOOP_SEG)
            continue

    sig = sorted((c["id"], c.get("stream_url", "")) for c in cams)
    stalled = pipe is not None and bool(cams) and (time.time() - _last_frame) > STALL_SEG
    if sig != current_sig or stalled:
        motivo = "watchdog: pipeline travado" if (stalled and sig == current_sig) else "lista/urls mudou"
        print(f"[sync] {motivo} -> {len(cams)} camera(s): {', '.join(c['nome'] for c in cams) or '(nenhuma)'}")
        if pipe:
            try:
                pipe.terminate(); pipe.join()
            except Exception:
                pass
            pipe = None
        if cams:
            cam_by_idx = {i: c for i, c in enumerate(cams)}
            try:
                pipe = InferencePipeline.init(
                    model_id=MODEL_TO_RUN,
                    video_reference=[c["stream_url"] for c in cams],
                    on_prediction=sink, api_key=API,
                    max_fps=(MAX_FPS if IS_PARENT else MAX_FPS_EXTRA))
                # use_main_thread=False: roda o pipeline numa thread separada e RETORNA,
                # senao start() bloqueia o loop e o detector nunca re-sincroniza (camera
                # excluida/adicionada/alterada so seria percebida ao reiniciar).
                pipe.start(use_main_thread=False)
                _last_frame = time.time()    # janela de aquecimento antes do watchdog
                _pipe_started = time.time()  # inicia o WARMUP (descarta frames corrompidos)
            except Exception as e:
                print("[pipe] erro ao iniciar:", str(e)[:150]); pipe = None
        current_sig = sig

    time.sleep(LOOP_SEG)
