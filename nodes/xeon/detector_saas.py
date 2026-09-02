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
if MODEL_ID_EPI and "epi" not in TIPOS_ATIVOS:
    TIPOS_ATIVOS.add("epi")
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
    """Classifica o que cobre o rosto/cabeca: 'balaclava' (touca ninja), 'capacete' (moto) ou 'nenhum'.
    Fail-closed: sem Gemini / erro / circuito aberto -> ('nenhum','') (nao alerta, evita falso alarme)."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return "nenhum", ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return "nenhum", ""
    prompt = ('Camera de seguranca "' + str(nome) + '". A imagem e o RECORTE da cabeca/rosto de uma pessoa. '
              'Classifique o que cobre o rosto/cabeca em UMA categoria: '
              '"balaclava" = touca ninja, mascara de esqui, pano ou capuz cobrindo o rosto para OCULTAR a identidade (tipico de assalto a pe). '
              '"capacete" = CAPACETE DE MOTO / motociclista (casco rigido, com viseira), incluindo capacete integral fechado. '
              '"nenhum" = rosto visivel, oculos ou oculos escuros, bone ou chapeu, mascara cirurgica comum, ou capuz SEM mascara. '
              'IMPORTANTE: diferencie bem CAPACETE DE MOTO (casco rigido, viseira) de BALACLAVA (tecido macio) — nao confunda. '
              'Responda SO JSON: {"tipo": "balaclava|capacete|nenhum", "descricao": "1 frase"}')
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
        tipo = str(d.get("tipo", "nenhum")).strip().lower()
        if tipo not in ("balaclava", "capacete"):
            tipo = "nenhum"
        return tipo, d.get("descricao", "")
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return "nenhum", ""


def gemini_queda(crop_jpg, nome, still_secs=0):
    # True se ha pessoa CAIDA no chao / desmaiada / imovel sugerindo queda ou mal subito. Fail-closed.
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return False, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return False, ""
    prompt = ('Camera de seguranca "' + str(nome) + '". A imagem e o RECORTE de uma pessoa que esta ha ~' + str(int(still_secs)) +
              's DEITADA e IMOVEL. Ela parece ter CAIDO / desmaiado / passado mal e estar no CHAO precisando de ajuda? '
              'Responda false se for situacao NORMAL: pessoa sentada/agachada, deitada em CAMA/SOFA/REDE/espreguicadeira, '
              'fazendo exercicio/alongamento no chao, tomando sol, nadando, ou trabalhando deitada. '
              'Responda true SOMENTE se parecer uma pessoa desamparada no chao (queda/mal subito). '
              'Responda SO JSON: {"caida": true/false, "descricao": "1 frase"}')
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
        return bool(d.get("caida")), d.get("descricao", "possivel pessoa caida")
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return False, ""


def gemini_piscina(crop_jpg, nome, still_secs=0, crowd=False):
    """True se ha sinal REAL de afogamento/perigo na area da agua. Fail-closed.
    AUXILIO: nao substitui salva-vidas/vigilancia; sujeito a falso negativo."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return False, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return False, ""
    if crowd:
        prompt = ('Camera de seguranca de uma PISCINA com VARIAS pessoas na agua. Examine CADA pessoa, uma a uma. '
                  'Dispare (perigo=true) se QUALQUER UMA tiver sinal de afogamento: boiando de BRUCOS (rosto na agua) e imovel, '
                  'boiando de costas imovel sem nadar, corpo submerso/parado sob a agua, pessoa na VERTICAL com a cabeca pra tras '
                  'lutando pra manter o rosto fora (afogamento silencioso), ou sendo puxada pra baixo. '
                  'NAO se tranquilize porque as OUTRAS estao nadando/brincando normal - basta UMA em perigo. '
                  'NAO alarme para: mergulho voluntario breve, nado normal, brincadeira ativa, gente sentada na borda. '
                  'Responda true SOMENTE com sinal REAL. SO JSON: {"perigo": true/false, "descricao": "1 frase (quem e onde)"}')
    else:
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
        "epoch": int(time.time()),   # epoch do evento p/ o video do ocorrido do webhook
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
    "capacete":    ("capacete", "moto", "moto_capacete", "capacete_moto"),
    "piscina":     ("piscina", "afogamento"),
    "queda":       ("queda", "pessoa_caida", "caido"),
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
    "capacete": (MODEL_ID_GENERAL,),  # capacete/moto: COCO (pessoa) + Gemini (classifica capacete vs balaclava)
    "piscina": (MODEL_ID_GENERAL,),  # afogamento usa COCO (pessoa) + Gemini
    "queda": (MODEL_ID_GENERAL,),  # pessoa caida/queda: COCO (pessoa) + Gemini
    "facial": (MODEL_ID_GENERAL,),  # controle de acesso: COCO so p/ a camera ser processada; recon = YuNet+SFace em _facial_check
    "guarda_piscina": (MODEL_ID_GENERAL,),  # guarda-piscina: COCO (pessoa/animal) na agua quando ARMADO
    "suspeito": (MODEL_ID_GENERAL,),  # detector de suspeitos: COCO (pessoa) -> merodeio/permanencia
    "furto": (MODEL_ID_GENERAL,),  # ocultacao/furto (varejo): COCO (pessoa) + Gemini no recorte
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
# classe do modelo -> item do catalogo (regra por setor: cam.config_analitico.epi_itens)
_EPI_CLASSE_ITEM = {"no_helmet": "capacete", "no_glove": "luva", "no_goggles": "oculos",
                    "no_mask": "mascara", "no_shoes": "botina"}
def _epi_item_obrigatorio(cam, cls):
    """True se o item ausente (cls) e OBRIGATORIO nesta camera. Sem lista epi_itens = alerta todos (padrao)."""
    req = ((cam.get("config_analitico") or {}).get("epi_itens")) or []
    if not req:
        return True
    item = _EPI_CLASSE_ITEM.get(str(cls).lower())
    return (item in req) if item else True
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
    # UNIAO (agenda por analitico): base 24h (analiticos_padrao) + TODAS as janelas que casam agora
    ativos = set(cfg.get("analiticos_padrao") or [])
    for h in (cfg.get("horarios") or []):
        if not (dias_hoje & set(h.get("dias") or [])):
            continue
        ini = h.get("hora_inicio") or "00:00"
        fim = h.get("hora_fim") or "23:59"
        dentro = (ini <= hhmm <= fim) if ini <= fim else (hhmm >= ini or hhmm <= fim)  # suporta cruzar meia-noite
        if dentro:
            ativos |= set(h.get("analiticos") or [])
    return ativos


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
    if frame_bgr is None:
        return
    toca_on = _tipo_ativo_na_cam(cam, "toca_ninja", now)
    cap_on = _tipo_ativo_na_cam(cam, "capacete", now)
    if not (toca_on or cap_on):
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
        tipo, desc = gemini_balaclava(buf.tobytes(), cam.get("nome", ""))
    except Exception as e:
        print("[bala] erro:", e); return
    # roteia: balaclava -> toca_ninja ; capacete -> capacete (moto). Capacete NUNCA dispara toca_ninja.
    if tipo == "balaclava" and toca_on:
        alerta_tipo = "toca_ninja"; texto = "Pessoa com rosto coberto (touca ninja): " + (desc or "")
    elif tipo == "capacete" and cap_on:
        alerta_tipo = "capacete"; texto = "Pessoa de capacete / motociclista: " + (desc or "")
    else:
        return
    if now - _bala_ultimo.get((cid, alerta_tipo), 0) < BALA_COOLDOWN:
        return
    _bala_ultimo[(cid, alerta_tipo)] = now
    img_b64 = None
    try:
        an = frame_bgr.copy()
        cv2.rectangle(an, (int(x - w / 2), int(y - h / 2)), (int(x + w / 2), int(y + h / 2)), (0, 0, 255), 2)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    print(f"[bala] {cam.get('nome','')}: {tipo} - {desc}")
    envia_alerta(cam, alerta_tipo, float(p.get("confidence", 0.9)), texto, img_b64, verificado=True)


# ---------- PISCINA / AFOGAMENTO (AUXILIO): pessoa na agua imovel + Gemini na zona da agua ----------
_pisc = {}          # cam_id -> {"last_check":ts,"centroid":(fx,fy),"still_since":ts}
_pisc_ultimo = {}   # cam_id -> ts do ultimo alerta
PISCINA_CHECK_SEC = int(os.getenv("PISCINA_CHECK_SEC", "20"))   # intervalo entre checks Gemini
PISCINA_STILL     = int(os.getenv("PISCINA_STILL", "30"))       # imovel por Xs -> antecipa o check
PISCINA_COOLDOWN  = int(os.getenv("PISCINA_COOLDOWN", "60"))
PISCINA_MOVE_TOL  = float(os.getenv("PISCINA_MOVE_TOL", "0.05").replace(",", "."))
PISCINA_CROWD_N     = int(os.getenv("PISCINA_CROWD_N", "4"))       # >= N pessoas na agua = modo multidao
PISCINA_CHECK_CROWD = int(os.getenv("PISCINA_CHECK_CROWD", "12"))  # em multidao varre o Gemini a cada Xs (vs PISCINA_CHECK_SEC)
# --- v2 SUBMERSAO (auxilio): rastreio leve p/ detectar pessoa que some na agua e nao reaparece ---
PISCINA_SUBMERSO_ON  = os.getenv("PISCINA_SUBMERSO_ON", "1") not in ("0", "false", "False", "")
PISCINA_SUBMERSO_SEC = int(os.getenv("PISCINA_SUBMERSO_SEC", "35"))            # sumiu na agua por Xs -> alerta (faixa 30-45)
PISCINA_TRACK_TOL    = float(os.getenv("PISCINA_TRACK_TOL", "0.12").replace(",", "."))  # dist (frac) p/ casar deteccao<->track
PISCINA_MIN_IDADE    = float(os.getenv("PISCINA_MIN_IDADE", "5").replace(",", "."))     # track precisa ter vivido Xs antes de contar sumico
PISCINA_BORDA        = float(os.getenv("PISCINA_BORDA", "0.06").replace(",", "."))      # margem p/ "interior" (longe da borda = saida)
_pisc_tracks = {}    # cid -> [...]  (legado do modelo por-track, nao usado)
_pisc_tid    = {}    # cid -> contador de id de track
_pisc_pres   = {}    # cid -> {first,last_any,hits_int,cx,cy,last_interior,alerted,veto_ts}
_pisc_ptracks = {}   # cid -> [ {id,cx,cy,first,last,hits,interior,alerted,crop} ] (rastreio POR PESSOA - piscina cheia)
_pisc_ptid    = {}   # cid -> contador de id de track por pessoa
PISCINA_MIN_HITS = int(os.getenv("PISCINA_MIN_HITS", "2"))   # nº min de deteccoes interior p/ nao ser ghost
PISCINA_VETO_ON       = os.getenv("PISCINA_VETO_ON", "1") not in ("0", "false", "False", "")  # Gemini derruba falso positivo
PISCINA_VETO_COOLDOWN = int(os.getenv("PISCINA_VETO_COOLDOWN", "30"))   # apos um veto, espera Xs p/ reavaliar
PISCINA_DEBUG = os.getenv("PISCINA_DEBUG", "0") not in ("0", "false", "False", "")
_pisc_dbg_last = {}  # cid -> ts do ultimo print de debug


def _bbox_poly(poly, W, H):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return (int(max(0, min(xs) * W)), int(max(0, min(ys) * H)), int(min(W, max(xs) * W)), int(min(H, max(ys) * H)))


def _interior_pt(x, y, poly, margin):
    """True se (x,y) esta DENTRO do poligono e a >= margin de qualquer borda (longe da saida)."""
    if not _pt_in_poly(x, y, poly):
        return False
    n = len(poly); dmin = 1e9
    for i in range(n):
        ax, ay = poly[i][0], poly[i][1]
        bx, by = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
        d = _dist_seg(x, y, ax, ay, bx, by)
        if d < dmin:
            dmin = d
    return dmin >= margin


def _pisc_alerta_submersao(cam, agua, frame_bgr, W, H, track, gone, now):
    img_b64 = None
    try:
        import numpy as _np
        an = frame_bgr.copy()
        cv2.polylines(an, [_np.array([(int(px * W), int(py * H)) for px, py in agua], dtype=_np.int32)], True, (255, 0, 0), 3)
        px, py = int(track["cx"] * W), int(track["cy"] * H)
        cv2.circle(an, (px, py), 26, (0, 0, 255), 3)
        cv2.putText(an, "sumiu aqui ~%ds" % int(gone), (max(0, px - 70), max(24, py - 32)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    desc = ("PISCINA (auxilio): possivel AFOGAMENTO por SUBMERSAO - uma pessoa sumiu dentro da agua ha ~%ds e nao reapareceu na superficie" % int(gone))
    print("[piscina] %s: SUBMERSAO - track %s gone %ds" % (cam.get("nome", ""), track.get("id"), int(gone)))
    envia_alerta(cam, "afogamento", 0.85, desc, img_b64, verificado=True)


def _gemini_veto_submersao(frame_bgr, agua, cx, cy, W, H, nome):
    """True = CANCELAR o alerta (ha pessoa claramente presente/tranquila = falso positivo).
    Fail-safe: sem Gemini/erro/duvida -> False (NAO cancela = envia o alerta)."""
    if not USE_GEMINI or not GEMINI_KEY:
        return False, ""
    try:
        import numpy as _np
        an = frame_bgr.copy()
        cv2.polylines(an, [_np.array([(int(px * W), int(py * H)) for px, py in agua], dtype=_np.int32)], True, (255, 0, 0), 2)
        cv2.circle(an, (int(cx * W), int(cy * H)), 26, (0, 0, 255), 3)
        ok, buf = cv2.imencode(".jpg", an)
        if not ok:
            return False, ""
        crop = buf.tobytes()
    except Exception:
        return False, ""
    prompt = ('Camera de PISCINA. Um alerta automatico suspeita que a pessoa no CIRCULO VERMELHO AFUNDOU '
              '(submergiu) e nao reapareceu. Olhe a imagem inteira. Responda cancelar=true SOMENTE se ha uma '
              'pessoa CLARAMENTE VISIVEL e TRANQUILA que apenas saiu da agua ou nunca afundou (sentada, em pe, '
              'na beira/deck, andando normal) = FALSO ALARME. Responda cancelar=false se a agua no circulo parece '
              'VAZIA, se alguem parece em APUROS/submerso, ou se voce esta em DUVIDA. Na duvida NAO cancele. '
              'SO JSON: {"cancelar": true/false, "motivo": "1 frase"}')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        r = requests.post(url, timeout=15, json={
            "contents": [{"parts": [{"text": prompt},
                          {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(crop).decode()}}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0}})
        d = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        return bool(d.get("cancelar")), d.get("motivo", "")
    except Exception:
        return False, ""


def _pisc_submerso(cam, cid, inwater, agua, frame_bgr, W, H, now):
    """OCUPACAO da zona (robusto a deteccao esparsa). Dispara SO se: houve pessoa no INTERIOR
    (>= MIN_HITS), a ULTIMA deteccao na agua foi no INTERIOR (nao na borda=saindo), a zona ficou
    vazia por SUBMERSO_SEC, e o Gemini NAO vetar (nao ve pessoa tranquila). Assim 'sair da piscina'
    NAO dispara (ultima deteccao vai pra borda), e falso positivo por perda de deteccao cai no veto."""
    st = _pisc_pres.get(cid)
    if inwater:
        interior_pts = [(fx, fy) for (fx, fy, _p) in inwater if _interior_pt(fx, fy, agua, PISCINA_BORDA)]
        if st is None or (now - st["last_any"]) > PISCINA_SUBMERSO_SEC:
            st = {"first": now, "last_any": now, "hits_int": 0, "cx": None, "cy": None,
                  "last_interior": False, "alerted": False, "veto_ts": 0}
        st["last_any"] = now
        st["last_interior"] = bool(interior_pts)
        if interior_pts:
            st["hits_int"] += 1
            st["cx"] = sum(p[0] for p in interior_pts) / len(interior_pts)
            st["cy"] = sum(p[1] for p in interior_pts) / len(interior_pts)
            st["alerted"] = False
        _pisc_pres[cid] = st
        return
    if st is None:
        return
    gone = now - st["last_any"]
    if (not st["alerted"] and st["hits_int"] >= PISCINA_MIN_HITS and st["cx"] is not None
            and st.get("last_interior") and gone >= PISCINA_SUBMERSO_SEC
            and (now - _pisc_ultimo.get(cid, 0) >= PISCINA_COOLDOWN)
            and (now - st.get("veto_ts", 0) >= PISCINA_VETO_COOLDOWN)):
        vetar, motivo = False, ""
        if PISCINA_VETO_ON:
            try:
                vetar, motivo = _gemini_veto_submersao(frame_bgr, agua, st["cx"], st["cy"], W, H, cam.get("nome", ""))
            except Exception:
                vetar, motivo = False, ""
        if vetar:
            st["veto_ts"] = now
            print("[piscina] %s: submersao VETADA pelo Gemini - %s" % (cam.get("nome", ""), motivo))
        else:
            st["alerted"] = True
            _pisc_ultimo[cid] = now
            _pisc_alerta_submersao(cam, agua, frame_bgr, W, H, {"cx": st["cx"], "cy": st["cy"], "id": "zona"}, gone, now)
        _pisc_pres[cid] = st
    if gone > PISCINA_SUBMERSO_SEC + 120:
        _pisc_pres.pop(cid, None)


def _crop_person(frame_bgr, p, W, H):
    try:
        cx = float(p.get("x", 0)); cy = float(p.get("y", 0)); w = float(p.get("width", 0)); h = float(p.get("height", 0))
        mx = int(w * 0.15); my = int(h * 0.12)
        x1 = max(0, int(cx - w / 2) - mx); y1 = max(0, int(cy - h / 2) - my)
        x2 = min(W, int(cx + w / 2) + mx); y2 = min(H, int(cy + h / 2) + my)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        return frame_bgr[y1:y2, x1:x2].copy()
    except Exception:
        return None


def _pisc_alerta_pessoa(cam, agua, frame_bgr, W, H, t, gone, now):
    import numpy as _np
    an = frame_bgr.copy()
    try:
        cv2.polylines(an, [_np.array([(int(px * W), int(py * H)) for px, py in agua], dtype=_np.int32)], True, (255, 0, 0), 2)
        px, py = int(t["cx"] * W), int(t["cy"] * H)
        cv2.circle(an, (px, py), 34, (0, 0, 255), 4)
        cv2.arrowedLine(an, (px, max(0, py - 95)), (px, max(0, py - 40)), (0, 0, 255), 4, tipLength=0.35)
        cv2.putText(an, "SUMIU ~%ds" % int(gone), (max(0, px - 78), max(28, py - 102)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        crop = t.get("crop")
        if crop is not None and getattr(crop, "size", 0) > 0:
            ih = 130; iw = max(46, min(int(crop.shape[1] * ih / max(1, crop.shape[0])), 190))
            th = cv2.resize(crop, (iw, ih))
            an[10:10 + ih, 10:10 + iw] = th
            cv2.rectangle(an, (10, 10), (10 + iw, 10 + ih), (0, 0, 255), 3)
            cv2.putText(an, "ultima vez visto", (10, 10 + ih + 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
    except Exception as _e:
        print("[piscina-pessoa] desenho:", _e)
    img_b64 = None
    try:
        ok, buf = cv2.imencode(".jpg", an)
        if ok:
            img_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    desc = "PISCINA: uma pessoa SUMIU dentro da agua ha ~%ds e NAO reapareceu (nao saiu pela borda) - possivel afogamento" % int(gone)
    print("[piscina] %s: PESSOA SUMIU (track %s) ~%ds" % (cam.get("nome", ""), t.get("id"), int(gone)))
    envia_alerta(cam, "afogamento", 0.9, desc, img_b64, verificado=True)


def _pisc_person_check(cam, cid, inwater, agua, frame_bgr, W, H, now):
    """Rastreio POR PESSOA (piscina CHEIA): cada pessoa na agua vira um track. Se um track INTERIOR
    (nao na borda = nao saiu andando) some por PISCINA_SUBMERSO_SEC sem reaparecer, e ja tinha
    >= PISCINA_MIN_HITS deteccoes (nao e fantasma), dispara marcando a pessoa/lugar."""
    tracks = _pisc_ptracks.get(cid) or []
    used = [False] * len(tracks)
    novos = []
    for (fx, fy, p) in inwater:
        best, bestd = -1, PISCINA_TRACK_TOL
        for i, tk in enumerate(tracks):
            if used[i]:
                continue
            d = ((fx - tk["cx"]) ** 2 + (fy - tk["cy"]) ** 2) ** 0.5
            if d <= bestd:
                bestd, best = d, i
        interior = _interior_pt(fx, fy, agua, PISCINA_BORDA)
        crop = _crop_person(frame_bgr, p, W, H)
        if best >= 0:
            tk = tracks[best]; used[best] = True
            tk["cx"] = fx; tk["cy"] = fy; tk["last"] = now; tk["hits"] += 1
            tk["interior"] = interior; tk["alerted"] = False
            if crop is not None:
                tk["crop"] = crop
        else:
            _pisc_ptid[cid] = _pisc_ptid.get(cid, 0) + 1
            novos.append({"id": _pisc_ptid[cid], "cx": fx, "cy": fy, "first": now, "last": now,
                          "hits": 1, "interior": interior, "alerted": False, "crop": crop})
    keep = []
    for i, tk in enumerate(tracks):
        if used[i]:
            keep.append(tk); continue
        gone = now - tk["last"]
        if (tk["interior"] and not tk["alerted"] and tk["hits"] >= PISCINA_MIN_HITS
                and gone >= PISCINA_SUBMERSO_SEC
                and (now - _pisc_ultimo.get(cid, 0) >= PISCINA_COOLDOWN)):
            tk["alerted"] = True; _pisc_ultimo[cid] = now
            _pisc_alerta_pessoa(cam, agua, frame_bgr, W, H, tk, gone, now)
        if gone <= PISCINA_SUBMERSO_SEC + 120:
            keep.append(tk)
    _pisc_ptracks[cid] = keep + novos


GUARDA_CONFIRM_SEC = float(os.getenv("GUARDA_CONFIRM_SEC", "2").replace(",", "."))  # presenca confirmada por Xs -> alerta
GUARDA_COOLDOWN    = int(os.getenv("GUARDA_COOLDOWN", "60"))
_GUARDA_ANIMAIS = {"dog", "cat", "bird", "horse", "sheep", "cow", "bear", "cachorro", "gato", "animal", "pet"}
_guarda_state = {}   # cid -> {since, last, alerted}
_guarda_dbg = {}     # cid -> ts do ultimo debug


def _guarda_alerta(cam, agua, frame_bgr, W, H, p, tipo_intruso, now):
    import numpy as _np
    img_b64 = None
    try:
        an = frame_bgr.copy()
        cv2.polylines(an, [_np.array([(int(px * W), int(py * H)) for px, py in agua], dtype=_np.int32)], True, (255, 0, 0), 2)
        bx = float(p.get("x", 0)); by = float(p.get("y", 0)); bw = float(p.get("width", 40)); bh = float(p.get("height", 40))
        x1 = max(0, int(bx - bw / 2)); y1 = max(0, int(by - bh / 2)); x2 = min(W, int(bx + bw / 2)); y2 = min(H, int(by + bh / 2))
        cv2.rectangle(an, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.putText(an, "GUARDA-PISCINA", (max(0, x1 - 8), max(26, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        ok, buf = cv2.imencode(".jpg", an)
        if ok:
            img_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    desc = "GUARDA-PISCINA: %s entrou na piscina (que deveria estar vazia)" % tipo_intruso
    print("[guarda] %s: %s na piscina (armado)" % (cam.get("nome", ""), tipo_intruso))
    envia_alerta(cam, "guarda_piscina", 0.9, desc, img_b64, verificado=True)


def _guarda_check(cam, predictions, frame_bgr, now):
    """Guarda-piscina (piscina que deveria estar VAZIA, armavel pelo cliente): quando ARMADO,
    pessoa OU animal na zona de agua (confirmado GUARDA_CONFIRM_SEC) -> alerta imediato marcando o intruso."""
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "guarda_piscina", now):
        return
    if not (cam.get("config_analitico") or {}).get("guarda_armado"):
        return
    agua = None
    for z in (cam.get("config_analitico") or {}).get("zonas_intrusao", []) or []:
        if z.get("tipo") == "agua" and len(z.get("pontos") or []) >= 3:
            agua = z["pontos"]; break
    if not agua:
        return
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    cid = cam.get("id")
    alvo, tipo_intruso = None, None
    inwater = [(fx, fy, p) for (fx, fy, p) in _person_pts(preds, W, H) if _pt_in_poly(fx, fy, agua)]
    if inwater:
        alvo = inwater[0][2]; tipo_intruso = "pessoa"
    else:
        for p in preds:
            cls = str(p.get("class", "")).lower()
            if CLASS_MAP.get(cls) == "animal" or cls in _GUARDA_ANIMAIS:
                fx = float(p.get("x", 0)) / (W or 1); fy = (float(p.get("y", 0)) + float(p.get("height", 0)) / 2.0) / (H or 1)
                if _pt_in_poly(fx, fy, agua):
                    alvo = p; tipo_intruso = "animal"; break
    st = _guarda_state.get(cid) or {"since": 0.0, "last": 0.0, "alerted": 0.0}
    if alvo:
        if not st["since"] or (now - st.get("last", 0)) > 5:   # nova presenca (1a vez ou apos gap > 5s)
            st["since"] = now
        st["last"] = now
        if (now - st["since"] >= GUARDA_CONFIRM_SEC) and (now - st.get("alerted", 0) >= GUARDA_COOLDOWN):
            st["alerted"] = now
            _guarda_alerta(cam, agua, frame_bgr, W, H, alvo, tipo_intruso, now)
    else:
        if st.get("last") and (now - st["last"]) > 5:   # sumiu de vez -> zera a presenca
            st["since"] = 0.0
    _guarda_state[cid] = st
    if PISCINA_DEBUG and now - _guarda_dbg.get(cid, 0) >= 2:
        _guarda_dbg[cid] = now
        print("[guarda-dbg] %s | alvo=%s | presente_ha=%.0fs" % (cam.get("nome", ""), tipo_intruso or "-", (now - st["since"]) if (alvo and st.get("since")) else 0))


# ---------- DETECTOR DE SUSPEITOS - FASE 1 (Camada 1): merodeio / permanencia por pessoa ----------
SUSP_DWELL_SEC = float(os.getenv("SUSP_DWELL_SEC", "60").replace(",", "."))   # permanencia continua -> alerta
SUSP_MIN_HITS  = int(os.getenv("SUSP_MIN_HITS", "6"))                          # deteccoes minimas (nao e fantasma)
SUSP_COOLDOWN  = int(os.getenv("SUSP_COOLDOWN", "120"))                        # 1 alerta por camera a cada Xs
SUSP_TRACK_TOL = float(os.getenv("SUSP_TRACK_TOL", "0.12").replace(",", "."))  # dist (frac) p/ casar pessoa->track
SUSP_GAP_SEC   = float(os.getenv("SUSP_GAP_SEC", "5").replace(",", "."))       # some por > Xs -> track encerrado
_susp_tracks = {}   # cid -> [ {id,cx,cy,first,last,hits,alerted,crop} ]
_susp_tid = {}      # cid -> contador de ids
_susp_ultimo = {}   # cid -> ts do ultimo alerta
_susp_dbg = {}      # cid -> ts do ultimo debug


SUSP_GEMINI_ON = os.getenv("SUSP_GEMINI_ON", "1") not in ("0", "false", "False", "")
SUSP_RONDA_SEC    = float(os.getenv("SUSP_RONDA_SEC", "25").replace(",", "."))     # presenca minima p/ avaliar ronda
SUSP_RONDA_PATH   = float(os.getenv("SUSP_RONDA_PATH", "1.2").replace(",", "."))   # trajeto acumulado (fracao do frame) = andou muito
SUSP_RONDA_SPREAD = float(os.getenv("SUSP_RONDA_SPREAD", "0.55").replace(",", ".")) # diagonal max da area visitada = confinamento
SUSP_RONDA_RATIO  = float(os.getenv("SUSP_RONDA_RATIO", "2.5").replace(",", "."))  # trajeto/espalhamento alto = vai-e-volta (nao atravessa)


def gemini_suspeito(crop_jpg, nome, dwell_secs=0):
    """Fase 2 do detector de suspeitos. Retorna (veredito, motivo):
    veredito True=suspeito, False=normal, None=Gemini indisponivel/erro (=> fallback Camada 1).
    Anti-vies: julga SO a acao (ignora idade/genero/cor). Temperatura 0."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return None, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return None, ""
    prompt = ('Camera de seguranca "' + str(nome) + '". A pessoa no CIRCULO VERMELHO esta ha cerca de '
              + str(int(dwell_secs)) + ' segundos parada ou rondando nesta area. Analise SOMENTE o COMPORTAMENTO/ACAO '
              '(IGNORE idade, genero, cor da pele e roupa como fator; foque so na acao). A pessoa demonstra comportamento '
              'suspeito: merodeio (rondar/vigiar o local, ir e voltar, observar entradas/vitrine), ocultacao de objeto '
              '(guardar algo no corpo/bolsa de forma furtiva), tentativa de arrombamento/pulo de muro, ou permanencia sem '
              'proposito claro em local/horario incomum? NAO alarme para: pessoa trabalhando, esperando (fila/ponto de onibus), '
              'conversando, mexendo no celular, cliente escolhendo produto normalmente, ou funcionario. Na duvida responda '
              'suspeito=false. Responda SO JSON: {"suspeito": true/false, "categoria": "merodeio|ocultacao|arrombamento|permanencia|normal", "descricao": "1 frase do que a pessoa faz"}')
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
        cat = str(d.get("categoria", "") or ""); dsc = str(d.get("descricao", "") or "")
        motivo = ((cat + ": " + dsc).strip(": ").strip()) if (cat or dsc) else "confirmado"
        return bool(d.get("suspeito")), motivo
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return None, ""


def _susp_alerta(cam, area, frame_bgr, W, H, tk, dwell, now, modo="parado"):
    import numpy as _np
    px, py = int(tk["cx"] * W), int(tk["cy"] * H)
    _rond = (modo == "rondando")
    _lbl = ("RONDANDO ~%ds" % int(dwell)) if _rond else ("PERMANENCIA ~%ds" % int(dwell))
    _txt = ("pessoa RONDANDO a area ha ~%ds (indo e voltando, sem sair do local)" % int(dwell)) if _rond else ("pessoa parada/permanencia ha ~%ds na area vigiada" % int(dwell))
    # --- Fase 2: confirma o COMPORTAMENTO com Gemini (anti-vies; so acao) ---
    verif, extra = True, ""
    if SUSP_GEMINI_ON:
        try:
            g = frame_bgr.copy()
            if area:
                cv2.polylines(g, [_np.array([(int(qx * W), int(qy * H)) for qx, qy in area], dtype=_np.int32)], True, (0, 165, 255), 2)
            cv2.circle(g, (px, py), 30, (0, 0, 255), 4)
            okg, bg = cv2.imencode(".jpg", g)
            if okg:
                sus, motivo = gemini_suspeito(bg.tobytes(), cam.get("nome", ""), dwell)
                if sus is True:
                    extra = " [IA-visao: " + (motivo or "confirmado") + "]"
                elif sus is False:
                    verif = False
                    extra = " [IA-visao: sem indicio claro" + ((" - " + motivo) if motivo else "") + " - em revisao]"
                else:
                    extra = " [IA-visao indisponivel]"
        except Exception as _e:
            print("[suspeito] gemini:", _e)
    # --- imagem do alerta ---
    img_b64 = None
    try:
        an = frame_bgr.copy()
        if area:
            cv2.polylines(an, [_np.array([(int(qx * W), int(qy * H)) for qx, qy in area], dtype=_np.int32)], True, (0, 165, 255), 2)
        cv2.circle(an, (px, py), 30, (0, 0, 255), 4)
        cv2.putText(an, _lbl, (max(0, px - 96), max(26, py - 42)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        crop = tk.get("crop")
        if crop is not None and getattr(crop, "size", 0) > 0:
            ih = 130; iw = max(46, min(int(crop.shape[1] * ih / max(1, crop.shape[0])), 190))
            th = cv2.resize(crop, (iw, ih))
            an[10:10 + ih, 10:10 + iw] = th
            cv2.rectangle(an, (10, 10), (10 + iw, 10 + ih), (0, 0, 255), 3)
            cv2.putText(an, "pessoa", (10, 10 + ih + 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        ok, buf = cv2.imencode(".jpg", an)
        if ok:
            img_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception as _e:
        print("[suspeito] desenho:", _e)
    desc = "COMPORTAMENTO SUSPEITO: %s (assistivo - revisar)%s" % (_txt, extra)
    print("[suspeito] %s: %s (track %s) ~%ds verif=%s" % (cam.get("nome", ""), modo, tk.get("id"), int(dwell), verif))
    envia_alerta(cam, "suspeito", 0.85, desc, img_b64, verificado=verif)


SUSP_OCULT_SEC = float(os.getenv("SUSP_OCULT_SEC", "12").replace(",", "."))     # presenca minima p/ checar ocultacao
SUSP_OCULT_COOLDOWN = int(os.getenv("SUSP_OCULT_COOLDOWN", "25"))               # 1 chamada Gemini-furto por camera a cada Xs
_furto_ultimo = {}   # cid -> ts da ultima chamada gemini_furto


def gemini_furto(crop_jpg, nome):
    """Fase 3: True se a pessoa (RECORTE) esconde/guarda produto de forma furtiva.
    Tri-estado (True/False/None=indisponivel). Anti-vies: so a acao das maos/objeto."""
    if not USE_GEMINI or not GEMINI_KEY or not crop_jpg:
        return None, ""
    global _gem_fails, _gem_open_until, _gem_down
    if _gem_open_until and time.time() < _gem_open_until:
        return None, ""
    prompt = ('Camera de seguranca de loja/varejo "' + str(nome) + '". A imagem e o RECORTE de UMA pessoa. '
              'Ela esta ESCONDENDO/GUARDANDO um produto ou objeto de forma FURTIVA - enfiando no bolso, na cintura, '
              'por dentro da roupa, dentro da mochila/bolsa, ou sob uma peca de roupa? Foque SO na ACAO das maos/objeto '
              '(IGNORE idade, genero, cor da pele e roupa como fator). NAO alarme para: segurar/olhar um produto normal, '
              'mexer no celular, guardar a propria carteira/chave/celular, ou por compras numa sacola de compras. '
              'Na duvida responda ocultacao=false. Responda SO JSON: {"ocultacao": true/false, "descricao": "1 frase"}')
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
        return bool(d.get("ocultacao")), str(d.get("descricao", "") or "")
    except Exception as e:
        _gem_fails += 1
        if ("429" in str(e)) or _gem_fails >= GEMINI_CB_FAILS:
            _gem_open_until = time.time() + GEMINI_CB_COOLDOWN; _gem_down = True
        return None, ""


def _furto_alerta(cam, area, frame_bgr, W, H, tk, motivo, now):
    import numpy as _np
    px, py = int(tk["cx"] * W), int(tk["cy"] * H)
    img_b64 = None
    try:
        an = frame_bgr.copy()
        if area:
            cv2.polylines(an, [_np.array([(int(qx * W), int(qy * H)) for qx, qy in area], dtype=_np.int32)], True, (0, 165, 255), 2)
        cv2.circle(an, (px, py), 30, (0, 0, 255), 4)
        cv2.putText(an, "OCULTACAO?", (max(0, px - 72), max(26, py - 42)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        crop = tk.get("crop")
        if crop is not None and getattr(crop, "size", 0) > 0:
            ih = 150; iw = max(46, min(int(crop.shape[1] * ih / max(1, crop.shape[0])), 210))
            th = cv2.resize(crop, (iw, ih))
            an[10:10 + ih, 10:10 + iw] = th
            cv2.rectangle(an, (10, 10), (10 + iw, 10 + ih), (0, 0, 255), 3)
            cv2.putText(an, "pessoa", (10, 10 + ih + 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        ok, buf = cv2.imencode(".jpg", an)
        if ok:
            img_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception as _e:
        print("[furto] desenho:", _e)
    desc = "POSSIVEL FURTO/OCULTACAO: pessoa aparenta esconder produto/objeto no corpo/bolsa (assistivo - revisar) [IA-visao: %s]" % (motivo or "confirmado")
    print("[furto] %s: ocultacao (track %s)" % (cam.get("nome", ""), tk.get("id")))
    envia_alerta(cam, "suspeito", 0.85, desc, img_b64, verificado=True)


def _susp_check(cam, predictions, frame_bgr, now):
    """Fase 1 (Camada 1) do detector de suspeitos: rastreia cada pessoa na area vigiada
    (zona tipo 'vigilancia' desenhada, ou o frame inteiro) e, se uma pessoa permanece
    de forma continua (tolerante a gap) por >= SUSP_DWELL_SEC, dispara alerta ASSISTIVO
    marcando a pessoa. Respeita o horario do analitico. Sem vies: so geometria + tempo."""
    susp_on = _tipo_ativo_na_cam(cam, "suspeito", now)
    furto_on = _tipo_ativo_na_cam(cam, "furto", now)
    if frame_bgr is None or not (susp_on or furto_on):
        return
    area = None
    for z in (cam.get("config_analitico") or {}).get("zonas_intrusao", []) or []:
        if z.get("tipo") == "vigilancia" and len(z.get("pontos") or []) >= 3:
            area = z["pontos"]; break
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    cid = cam.get("id")
    persons = [(fx, fy, p) for (fx, fy, p) in _person_pts(preds, W, H) if (area is None or _pt_in_poly(fx, fy, area))]
    tracks = _susp_tracks.get(cid) or []
    used = [False] * len(tracks)
    novos = []
    for (fx, fy, p) in persons:
        best, bestd = -1, SUSP_TRACK_TOL
        for i, tk in enumerate(tracks):
            if used[i]:
                continue
            d = ((fx - tk["cx"]) ** 2 + (fy - tk["cy"]) ** 2) ** 0.5
            if d <= bestd:
                bestd, best = d, i
        crop = _crop_person(frame_bgr, p, W, H)
        if best >= 0:
            tk = tracks[best]; used[best] = True
            tk["path"] = tk.get("path", 0.0) + ((fx - tk["cx"]) ** 2 + (fy - tk["cy"]) ** 2) ** 0.5
            tk["cx"] = fx; tk["cy"] = fy; tk["last"] = now; tk["hits"] += 1
            tk["minx"] = min(tk.get("minx", fx), fx); tk["maxx"] = max(tk.get("maxx", fx), fx)
            tk["miny"] = min(tk.get("miny", fy), fy); tk["maxy"] = max(tk.get("maxy", fy), fy)
            if crop is not None:
                tk["crop"] = crop
            dwell = now - tk["first"]
            _spread = ((tk["maxx"] - tk["minx"]) ** 2 + (tk["maxy"] - tk["miny"]) ** 2) ** 0.5
            if (susp_on and not tk["alerted"] and tk["hits"] >= SUSP_MIN_HITS and dwell >= SUSP_DWELL_SEC
                    and (now - _susp_ultimo.get(cid, 0) >= SUSP_COOLDOWN)):
                tk["alerted"] = True; _susp_ultimo[cid] = now
                _susp_alerta(cam, area, frame_bgr, W, H, tk, dwell, now, "parado")
            elif (susp_on and not tk.get("ronda_alerted") and tk["hits"] >= SUSP_MIN_HITS and dwell >= SUSP_RONDA_SEC
                    and tk["path"] >= SUSP_RONDA_PATH and _spread <= SUSP_RONDA_SPREAD
                    and tk["path"] >= SUSP_RONDA_RATIO * max(_spread, 0.05)
                    and (now - _susp_ultimo.get(cid, 0) >= SUSP_COOLDOWN)):
                tk["ronda_alerted"] = True; _susp_ultimo[cid] = now
                _susp_alerta(cam, area, frame_bgr, W, H, tk, dwell, now, "rondando")
        else:
            _susp_tid[cid] = _susp_tid.get(cid, 0) + 1
            novos.append({"id": _susp_tid[cid], "cx": fx, "cy": fy, "first": now, "last": now,
                          "hits": 1, "alerted": False, "ronda_alerted": False, "crop": crop,
                          "path": 0.0, "minx": fx, "maxx": fx, "miny": fy, "maxy": fy})
    keep = [tk for i, tk in enumerate(tracks) if used[i] or (now - tk["last"] <= SUSP_GAP_SEC)]
    _susp_tracks[cid] = keep + novos
    # --- Fase 3: ocultacao / furto (varejo) - Gemini no recorte da pessoa, custo limitado por camera ---
    if furto_on and (now - _furto_ultimo.get(cid, 0) >= SUSP_OCULT_COOLDOWN):
        cand = None
        for tk in _susp_tracks[cid]:
            if tk.get("furto_alerted") or (now - tk["last"]) > SUSP_GAP_SEC:
                continue
            if (now - tk["first"]) < SUSP_OCULT_SEC or tk.get("crop") is None:
                continue
            if cand is None or tk["first"] < cand["first"]:
                cand = tk
        if cand is not None:
            _furto_ultimo[cid] = now
            try:
                okf, bf = cv2.imencode(".jpg", cand["crop"])
                if okf:
                    oc, motivo = gemini_furto(bf.tobytes(), cam.get("nome", ""))
                    if oc is True:
                        cand["furto_alerted"] = True
                        _furto_alerta(cam, area, frame_bgr, W, H, cand, motivo, now)
            except Exception as _e:
                print("[furto] erro:", _e)
    if PISCINA_DEBUG and now - _susp_dbg.get(cid, 0) >= 2:
        _susp_dbg[cid] = now
        _td = " ".join("t%s(h=%d dwell=%.0f path=%.2f spr=%.2f)" % (x["id"], x["hits"], now - x["first"], x.get("path", 0), (((x.get("maxx", 0) - x.get("minx", 0)) ** 2 + (x.get("maxy", 0) - x.get("miny", 0)) ** 2) ** 0.5)) for x in _susp_tracks[cid][:6])
        print("[suspeito-dbg] %s | area=%s | pessoas=%d | tracks=%d %s" % (cam.get("nome", ""), "zona" if area else "frame", len(persons), len(_susp_tracks[cid]), _td))


# ---------- PESSOA CAIDA / QUEDA (auxilio): pessoa deitada e imovel ~15-20s -> Gemini confirma ----------
_queda = {}          # cid -> {"since":ts,"centroid":(fx,fy),"last_check":ts}
_queda_ultimo = {}   # cid -> ts do ultimo alerta
_queda_dbg = {}
QUEDA_DEBUG = os.getenv("QUEDA_DEBUG", "0") not in ("0", "false", "False", "")
QUEDA_RATIO     = float(os.getenv("QUEDA_RATIO", "1.15").replace(",", "."))    # bbox mais LARGO que alto (w/h) = deitado
QUEDA_STILL     = int(os.getenv("QUEDA_STILL", "18"))                          # imovel+deitado por Xs antes de confirmar (conservador)
QUEDA_CHECK_SEC = int(os.getenv("QUEDA_CHECK_SEC", "15"))                      # intervalo min entre chamadas ao Gemini/cam
QUEDA_COOLDOWN  = int(os.getenv("QUEDA_COOLDOWN", "180"))                      # cooldown do alerta/cam
QUEDA_MOVE_TOL  = float(os.getenv("QUEDA_MOVE_TOL", "0.06").replace(",", "."))  # movimento (frac) que reinicia o cronometro
QUEDA_MIN_H     = float(os.getenv("QUEDA_MIN_H", "0.12").replace(",", "."))     # altura min do bbox (frac) p/ ignorar ruido distante


def _queda_check(cam, predictions, frame_bgr, now):
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "queda", now):
        return
    preds = predictions.get("predictions", []) if isinstance(predictions, dict) else []
    H, W = frame_bgr.shape[:2]
    lying = []
    for (fx, fy, p) in _person_pts(preds, W, H):
        w = float(p.get("width", 0)); h = float(p.get("height", 0))
        if h <= 0 or (h / (H or 1)) < QUEDA_MIN_H:
            continue
        if (w / h) >= QUEDA_RATIO:   # deitado: largura >= altura*ratio (pessoa em pe tem h >> w)
            lying.append((fx, fy, p))
    cid = cam.get("id"); st = _queda.get(cid) or {}
    if QUEDA_DEBUG and (now - _queda_dbg.get(cid, 0) >= 3):
        _queda_dbg[cid] = now
        _mr = 0.0
        for (_a, _b, _pp) in _person_pts(preds, W, H):
            _hh = float(_pp.get("height", 0)) or 1
            _mr = max(_mr, float(_pp.get("width", 0)) / _hh)
        print("[queda-dbg] %s: pessoas=%d deitadas=%d maxratio=%.2f (deitado>=%.2f) still=%ds" % (
            cam.get("nome", ""), len(_person_pts(preds, W, H)), len(lying), _mr, QUEDA_RATIO,
            int(now - (st.get("since") or now)) if st.get("since") else 0))
    GAP = float(os.getenv("QUEDA_GAP", "5").replace(",", "."))        # tolera flicker: gap sem "deitado" ate Xs NAO zera o cronometro
    JUMP = float(os.getenv("QUEDA_JUMP", "0.25").replace(",", "."))    # centroide pulou muito = outra pessoa/local -> reinicia episodio
    MINH = int(os.getenv("QUEDA_MIN_HITS", "4"))                       # min de frames "deitado" no episodio antes de confirmar
    p = None
    if lying:
        fx, fy, p = max(lying, key=lambda t: float(t[2].get("width", 0)) * float(t[2].get("height", 0)))
        cx, cy = fx, fy
        if st.get("since") and (now - float(st.get("last_lying", 0))) <= GAP:
            oc = st.get("centroid") or (cx, cy)
            jump = ((cx - oc[0]) ** 2 + (cy - oc[1]) ** 2) ** 0.5
            if jump <= JUMP:
                since = st["since"]; hits = int(st.get("hits", 0)) + 1
            else:
                since = now; hits = 1
        else:
            since = now; hits = 1
        _queda[cid] = {"since": since, "centroid": (cx, cy), "last_lying": now, "hits": hits, "last_check": st.get("last_check", 0)}
    else:
        if st.get("since") and (now - float(st.get("last_lying", 0))) <= GAP:
            _queda[cid] = st                        # gap curto de "deitado": mantem o episodio vivo (nao zera)
        else:
            _queda[cid] = {"since": 0, "centroid": None, "last_lying": 0, "hits": 0, "last_check": st.get("last_check", 0)}
        return                                      # sem "deitado" neste frame -> espera um frame com deitado p/ confirmar
    st2 = _queda[cid]
    still = now - float(st2["since"])
    if still < QUEDA_STILL or int(st2.get("hits", 0)) < MINH:   # conservador: imovel/deitado ~18s + N deteccoes
        return
    if now - float(st2.get("last_check", 0)) < QUEDA_CHECK_SEC:
        return
    _queda[cid]["last_check"] = now
    crop = _crop_person(frame_bgr, p, W, H)
    if crop is None:
        return
    try:
        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            return
        caida, desc = gemini_queda(buf.tobytes(), cam.get("nome", ""), int(still))
    except Exception as e:
        print("[queda] erro:", e); return
    if QUEDA_DEBUG:
        print("[queda-dbg] %s: GEMINI caida=%s (still=%ds) - %s" % (cam.get("nome", ""), caida, int(still), desc))
    if not caida or (now - _queda_ultimo.get(cid, 0) < QUEDA_COOLDOWN):
        return
    _queda_ultimo[cid] = now
    img_b64 = None
    try:
        x = float(p.get("x", 0)); y = float(p.get("y", 0)); w = float(p.get("width", 0)); h = float(p.get("height", 0))
        an = frame_bgr.copy()
        cv2.rectangle(an, (int(x - w / 2), int(y - h / 2)), (int(x + w / 2), int(y + h / 2)), (0, 0, 255), 3)
        cv2.putText(an, "PESSOA CAIDA?", (int(max(0, x - w / 2)), int(max(24, y - h / 2 - 8))),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    print("[queda] %s: pessoa caida (imovel %ds) - %s" % (cam.get("nome", ""), int(still), desc))
    envia_alerta(cam, "queda", 0.9, "PESSOA CAIDA (auxilio): " + (desc or "pessoa no chao imovel, possivel queda"), img_b64, verificado=True)


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
    if PISCINA_SUBMERSO_ON:
        try:
            _pisc_submerso(cam, cid, inwater, agua, frame_bgr, W, H, now)
        except Exception as _e:
            print("[piscina-sub] erro:", _e)
        try:
            _pisc_person_check(cam, cid, inwater, agua, frame_bgr, W, H, now)
        except Exception as _e:
            print("[piscina-pessoa] erro:", _e)
    if PISCINA_DEBUG and now - _pisc_dbg_last.get(cid, 0) >= 2:
        _pisc_dbg_last[cid] = now
        _p = _pisc_pres.get(cid)
        _pd = ("hits=%d gone=%.0f alert=%s" % (_p["hits_int"], now - _p["last_any"], _p["alerted"])) if _p else "-"
        _tk = _pisc_ptracks.get(cid) or []
        _td = " ".join("t%s(int=%s h=%d gone=%.0f)" % (x["id"], x["interior"], x["hits"], now - x["last"]) for x in _tk[:6])
        print("[piscina-dbg] %s | inwater=%d | pres[%s] | tracks=%d %s" % (cam.get("nome", ""), len(inwater), _pd, len(_tk), _td))
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
    _crowd = len(inwater) >= PISCINA_CROWD_N
    _chk = PISCINA_CHECK_CROWD if _crowd else PISCINA_CHECK_SEC
    if not ((now - last_check >= _chk) or (still >= PISCINA_STILL and now - last_check >= 8)):
        return
    _pisc[cid]["last_check"] = now
    x1, y1, x2, y2 = _bbox_poly(agua, W, H)
    if x2 - x1 < 24 or y2 - y1 < 24:
        return
    try:
        ok, buf = cv2.imencode(".jpg", frame_bgr[y1:y2, x1:x2])
        if not ok:
            return
        perigo, desc = gemini_piscina(buf.tobytes(), cam.get("nome", ""), still, crowd=_crowd)
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


# ================= CONTROLE DE ACESSO FACIAL (YuNet + SFace, on-prem) =================
_FACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "face")
FACIAL_MIN_PX    = int(os.getenv("FACIAL_MIN_PX", "80"))          # rosto menor que isso: NAO decide (evita chute)
FACIAL_DET_SCORE = float(os.getenv("FACIAL_DET_SCORE", "0.5").replace(",", "."))
FACIAL_THR       = float(os.getenv("FACIAL_COSINE_THR", "0.363").replace(",", "."))
FACIAL_CHECK_SEC = float(os.getenv("FACIAL_CHECK_SEC", "1.0").replace(",", "."))   # ~1 verificacao/seg
FACIAL_CONFIRM   = int(os.getenv("FACIAL_CONFIRM", "3"))          # nº de verificacoes p/ confirmar (nunca 1 quadro)
FACIAL_COOLDOWN  = int(os.getenv("FACIAL_COOLDOWN", "60"))        # 1 evento por "slot" a cada Xs
FACIAL_GAL_TTL   = int(os.getenv("FACIAL_GAL_TTL", "120"))        # recarrega a galeria da camera a cada Xs
_face_det = None
_face_rec = None
_face_gal = {}     # cid -> {"t": ts, "g": {nome: [emb np(1,128), ...]}}
_face_state = {}   # cid -> {"last": ts, "seen": {slot: n}, "alerted": {slot: ts}}


def _face_models():
    global _face_det, _face_rec
    if _face_det is None:
        y = os.path.join(_FACE_DIR, "yunet.onnx"); s = os.path.join(_FACE_DIR, "sface.onnx")
        _face_det = cv2.FaceDetectorYN.create(y, "", (320, 320), score_threshold=FACIAL_DET_SCORE)
        _face_rec = cv2.FaceRecognizerSF.create(s, "")
    return _face_det, _face_rec


def _face_gallery(cid):
    import numpy as _np
    ent = _face_gal.get(cid)
    if ent and time.time() - ent["t"] < FACIAL_GAL_TTL:
        return ent["g"]
    g = {}
    p = os.path.join(_FACE_DIR, "galleries", str(cid) + ".json")
    try:
        if os.path.exists(p):
            raw = json.load(open(p))
            for nome, embs in raw.items():
                g[nome] = [_np.array(e, dtype=_np.float32).reshape(1, -1) for e in embs]
    except Exception as e:
        print("[facial] galeria erro:", e)
    _face_gal[cid] = {"t": time.time(), "g": g}
    return g


def _facial_alerta(cam, frame_bgr, face, tipo, desc, verificado):
    img_b64 = None
    try:
        an = frame_bgr.copy()
        x, y, w, h = int(face[0]), int(face[1]), int(face[2]), int(face[3])
        cor = (0, 0, 255) if verificado else (0, 200, 0)
        cv2.rectangle(an, (x, y), (x + w, y + h), cor, 3)
        okA, bufA = cv2.imencode(".jpg", an)
        if okA:
            img_b64 = base64.b64encode(bufA.tobytes()).decode()
    except Exception:
        pass
    envia_alerta(cam, tipo, 0.9, desc, img_b64, verificado=verificado)


def _facial_check(cam, frame_bgr, now):
    """Controle de acesso facial: detecta TODOS os rostos >= FACIAL_MIN_PX, compara com a galeria
    da camera; confirma em varios quadros; reconhecido -> registra; desconhecido -> alerta plantao."""
    if frame_bgr is None or not _tipo_ativo_na_cam(cam, "facial", now):
        return
    cid = cam.get("id")
    st = _face_state.get(cid) or {"last": 0.0, "seen": {}, "alerted": {}}
    if now - st["last"] < FACIAL_CHECK_SEC:
        return
    st["last"] = now; _face_state[cid] = st
    det, rec = _face_models()
    gal = _face_gallery(cid)
    H, W = frame_bgr.shape[:2]
    try:
        det.setInputSize((W, H))
        _, faces = det.detect(frame_bgr)
    except Exception as e:
        print("[facial] detect erro:", e); return
    if faces is None:
        faces = []
    achou = {}   # slot -> face representativa (avalia TODOS os rostos do quadro)
    for f in faces:
        if float(f[2]) < FACIAL_MIN_PX:   # rosto pequeno demais -> nao decide
            continue
        try:
            feat = rec.feature(rec.alignCrop(frame_bgr, f))
        except Exception:
            continue
        best_nome, best = None, 0.0
        for nome, embs in gal.items():
            for e in embs:
                sc = rec.match(feat, e, cv2.FaceRecognizerSF_FR_COSINE)
                if sc > best:
                    best = sc; best_nome = nome
        slot = ("known:" + best_nome) if best >= FACIAL_THR else "unknown"
        achou[slot] = f
    seen = st["seen"]
    for slot, f in achou.items():
        seen[slot] = seen.get(slot, 0) + 1
        if seen[slot] >= FACIAL_CONFIRM and (now - st["alerted"].get(slot, 0) >= FACIAL_COOLDOWN):
            st["alerted"][slot] = now
            if slot.startswith("known:"):
                nome = slot.split(":", 1)[1]
                print("[facial] %s: ACESSO reconhecido -> %s" % (cam.get("nome", ""), nome))
                _facial_alerta(cam, frame_bgr, f, "acesso_facial", "Acesso reconhecido: " + nome, verificado=False)
            else:
                print("[facial] %s: pessoa DESCONHECIDA na portaria" % (cam.get("nome", "")))
                _facial_alerta(cam, frame_bgr, f, "facial_desconhecido", "Pessoa NAO reconhecida (controle de acesso)", verificado=True)
    for k in list(seen.keys()):   # decai slots ausentes neste ciclo
        if k not in achou:
            seen[k] = seen[k] - 1
            if seen[k] <= 0:
                del seen[k]
    st["seen"] = seen; _face_state[cid] = st


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
    # PESSOA CAIDA / QUEDA (auxilio): pessoa deitada imovel ~15-20s + Gemini confirma
    try:
        _queda_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[queda] erro:", e)
    # PISCINA/AFOGAMENTO (auxilio): pessoa imovel na agua + Gemini na zona
    try:
        _piscina_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[piscina] erro:", e)
    # CONTROLE DE ACESSO FACIAL: reconhece cadastrados / alerta desconhecido (portaria)
    try:
        _facial_check(cam, video_frame.image, time.time())
    except Exception as e:
        print("[facial] erro:", e)
    # GUARDA-PISCINA: piscina que deveria estar vazia + armada -> pessoa/animal na agua
    try:
        _guarda_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[guarda] erro:", e)
    # DETECTOR DE SUSPEITOS (Fase 1): merodeio/permanencia de pessoa na area vigiada
    try:
        _susp_check(cam, predictions, video_frame.image, time.time())
    except Exception as e:
        print("[suspeito] erro:", e)

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
        # EPI por setor: so alerta o item OBRIGATORIO naquela camera (lista epi_itens)
        if tipo == "epi" and not _epi_item_obrigatorio(cam, cls):
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
