"""
Corexia Vision AI — detector NVDEC (decode na GPU + inferencia na GPU).

Runner do caminho HIBRIDO: decodifica cada camera com ffmpeg-NVDEC (nvdec_source), roda os
modelos roboflow (arma/faca/fogo/placa) e REUSA 100% a logica de deteccao/alerta do
detector_saas (thresholds, anti-flicker, memoria de rejeicao, Gemini, movimento, webhook).

- 1 processo = 1 WORKER pinado numa GPU (WORKER_GPU), cuidando de uma FATIA de cameras.
- Nao toca no detector_saas em produção: so IMPORTA as funcoes dele.
- Cameras: de um arquivo id|url (teste) OU do backend filtrando decode_engine=nvdec.

Env principais:
  WORKER_GPU=0|1         GPU deste worker (decode+inferencia)
  CAMS_FILE=cameras.txt  arquivo id|url (modo teste); se ausente, busca do backend
  NVDEC_MAX_DIM=960      maior dimensao do frame decodificado (preserva aspecto)
  NVDEC_FPS=4            fps de amostragem por camera
  WORKER_ID / WORKER_COUNT  sharding (divide a lista entre N workers)
"""
import os, sys, time, json, glob, subprocess, re

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from dotenv import load_dotenv
load_dotenv(os.path.join(_HERE, ".env"))

# ---- GPU: re-exec com ambiente CUDA setado ANTES de importar cv2/inference/core ----
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
    _env["CUDA_VISIBLE_DEVICES"] = os.getenv("WORKER_GPU", "0")   # worker pinado numa placa
    os.execve(sys.executable, [sys.executable] + sys.argv, _env)

import requests, cv2, base64
from inference import get_model
import detector_saas as core           # reusa TODA a logica (import nao roda o loop dele)
from nvdec_source import NvdecSource

# o _process do core faz movimento internamente so se IS_PARENT; aqui a gente chama
# _motion_check UMA vez por frame (fora do _process) e desliga o motion interno pra nao duplicar.
core.IS_PARENT = False
core._pipe_started = 0        # desliga o warmup GLOBAL do _process; warmup e POR CAMERA aqui

# DRY-RUN (teste/canario): imprime o alerta em vez de POSTar no webhook — nao polui producao.
if os.getenv("NVDEC_DRYRUN") == "1":
    def _dry_alerta(cam, tipo, conf, desc, imagem_b64=None):
        print(f"[DRYRUN ALERTA] {cam.get('nome')} | {tipo} {int(conf*100)}% | {desc}", flush=True)
    core.envia_alerta = _dry_alerta
    print("[nvdec] DRY-RUN: alertas serao IMPRESSOS, nao enviados", flush=True)

API          = core.API
REFERER      = os.getenv("STREAM_REFERER", "")
WORKER_GPU   = os.getenv("WORKER_GPU", "0")
NVDEC_MAXDIM = int(os.getenv("NVDEC_MAX_DIM", "960"))
NVDEC_FPS    = float(os.getenv("NVDEC_FPS", "4").replace(",", "."))
WARMUP_SEG   = core.WARMUP_SEG
STALE_SEG    = float(os.getenv("NVDEC_STALE_SEG", "30"))   # frame mais velho que isso = pula
REOPEN_STALE = int(os.getenv("NVDEC_REOPEN_STALE", "120")) # fonte sem frame por tanto tempo = reabre (re-resolve)
WORKER_ID    = int(os.getenv("WORKER_ID", "0"))
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "1"))
MAX_CAMS     = int(os.getenv("NVDEC_MAX_CAMS", "0"))   # >0 limita nº de câmeras/worker (ramp)
# HIBRIDO: cada ffmpeg-NVDEC gasta ~262MB de contexto CUDA, entao a GPU satura ~30 cams/placa.
# As primeiras NVDEC_BUDGET/worker usam NVDEC (GPU); o resto usa decode na CPU (0 VRAM).
NVDEC_BUDGET = int(os.getenv("NVDEC_BUDGET", "28"))
# MOVIMENTO: alerta de movimento e OPT-IN por camera (cam.ia_movimento) — default off, senao
# camera de rua vira enxurrada. GATING: so roda o YOLO pesado quando ha movimento (+ piso de
# scan a cada FLOOR_SEC mesmo parado, rede de seguranca p/ arma em cena estatica).
MOTION_MODE = os.getenv("NVDEC_MOTION", "optin")           # optin | all | off (alertas)
MOTION_GATE = os.getenv("NVDEC_MOTION_GATE", "1") != "0"   # YOLO so com movimento (+piso)
FLOOR_SEC   = float(os.getenv("NVDEC_FLOOR_SEC", "3"))      # roda YOLO a cada X s mesmo parado
GATE_FRAC   = float(os.getenv("NVDEC_GATE_FRAC", "0.006"))  # movimento minimo p/ disparar YOLO
MOTION_COOLDOWN_NV = int(os.getenv("NVDEC_MOTION_COOLDOWN", "600"))  # 1 alerta movimento/cam/Xs
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))
FFPROBE      = os.getenv("FFPROBE_BIN", "/usr/bin/ffprobe")


class Frame:
    """objeto minimo que o core._process espera (.source_id + .image)."""
    __slots__ = ("source_id", "image")
    def __init__(self, sid, img):
        self.source_id = sid
        self.image = img


def ffprobe_wh(url):
    """resolucao do stream (w,h) ou None se offline."""
    try:
        cmd = [FFPROBE, "-v", "error"]
        if REFERER:
            cmd += ["-referer", REFERER]
        cmd += ["-select_streams", "v:0", "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x", url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
        # ffprobe pode devolver mais de uma linha (ex.: 2 streams) — pega a 1a "LARGxALT" valida
        line = next((l.strip() for l in out.splitlines() if "x" in l.lower()), "")
        w, h = line.lower().split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def target_size(w, h):
    """downscale preservando aspecto, maior lado = NVDEC_MAXDIM, dims pares (exig. do NV12)."""
    if max(w, h) <= NVDEC_MAXDIM:
        tw, th = w, h
    elif w >= h:
        tw = NVDEC_MAXDIM; th = round(h * NVDEC_MAXDIM / w)
    else:
        th = NVDEC_MAXDIM; tw = round(w * NVDEC_MAXDIM / h)
    return (tw // 2) * 2, (th // 2) * 2


_EMBED_RE = re.compile(r"let live = '([^']+)'")

def resolve_embed(embed_url):
    """re-resolve o embed do analitico pro .m3u8 ATUAL (o servidor liveNN pode rotacionar).
    Retorna '' se falhar (o chamador cai no stream_url guardado)."""
    try:
        html = requests.get(embed_url, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0", "Referer": REFERER}).text
        m = _EMBED_RE.search(html)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[nvdec:resolve] {str(e)[:70]}", flush=True)
    return ""


# ---------------- modelos (carrega uma vez, na GPU do worker) ----------------
def load_models():
    ids = []
    for mid in [core.MODEL_TO_RUN, core.MODEL_ID_FIRE, core.MODEL_ID_KNIFE, core.MODEL_ID_PLATE]:
        if mid and mid not in ids:
            ids.append(mid)
    models = []
    for mid in ids:
        try:
            m = get_model(mid, api_key=API)
            models.append((mid, m))
            print(f"[nvdec] modelo carregado: {mid}", flush=True)
        except Exception as e:
            print(f"[nvdec] FALHA ao carregar {mid}: {str(e)[:150]}", flush=True)
    return models


# --- carga ON-DEMAND: modelo so e baixado/instanciado quando alguma camera precisa dele ---
MODEL_CACHE = {}   # model_id -> modelo (ou None se falhou ao carregar)
def get_model_cached(mid):
    if mid in MODEL_CACHE:
        return MODEL_CACHE[mid]
    try:
        m = get_model(mid, api_key=API)
        print(f"[nvdec] modelo carregado on-demand: {mid}", flush=True)
    except Exception as e:
        print(f"[nvdec] FALHA ao carregar {mid}: {str(e)[:150]}", flush=True)
        m = None
    MODEL_CACHE[mid] = m
    return m


def preds_to_dict(resp):
    """converte a resposta do roboflow get_model().infer() no dict {'predictions':[...]}
    que o core._process/sv.Detections.from_inference esperam."""
    try:
        return resp.dict(by_alias=True, exclude_none=True)
    except AttributeError:
        return resp if isinstance(resp, dict) else {"predictions": []}


def motion_frac(rt, frame, now):
    """frame-diff barato -> fracao da cena que mudou (0..1). Usado pro GATING do YOLO e,
    se a camera for opt-in (ia_movimento), dispara o alerta de movimento (cooldown longo)."""
    try:
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (core.MOTION_W, core.MOTION_H))
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
    except Exception:
        return 0.0
    prev = rt.prev_gray
    rt.prev_gray = gray
    if prev is None:
        return 0.0
    diff = cv2.absdiff(prev, gray)
    _, th = cv2.threshold(diff, core.MOTION_DELTA, 255, cv2.THRESH_BINARY)
    frac = float((th > 0).sum()) / th.size
    # ALERTA de movimento OPT-IN: so camera marcada ia_movimento (ou modo 'all'), cooldown longo
    alerta_on = (MOTION_MODE == "all") or (MOTION_MODE == "optin" and rt.cam.get("ia_movimento"))
    if alerta_on and frac >= core.MOTION_FRAC and (now - rt.last_mov_alert) >= MOTION_COOLDOWN_NV:
        rt.last_mov_alert = now
        jpg = None
        try:
            ok, buf = cv2.imencode(".jpg", frame)
            if ok:
                jpg = base64.b64encode(buf.tobytes()).decode()
        except Exception:
            pass
        core.envia_alerta(rt.cam, "movimento", min(0.99, 0.5 + frac),
                          f"Movimento detectado ({int(frac*100)}% da cena)", jpg)
    return frac


# ---------------- fonte de cameras ----------------
def fetch_cameras():
    """do backend (decode_engine=nvdec) OU de um arquivo id|url (teste)."""
    cams_file = os.getenv("CAMS_FILE", "")
    if cams_file and os.path.exists(cams_file):
        cams = []
        for ln in open(cams_file, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "|" not in ln:
                continue
            cid, url = ln.split("|", 1)
            cams.append({"id": cid.strip(), "nome": f"cam-{cid.strip()}",
                         "stream_url": url.strip(), "cliente_nome": "", "cliente_telefone": "",
                         "cliente_id": "", "ia_placa": False})
        return cams
    # backend: pede so as cameras deste engine
    r = requests.post(core.CAMERAS_URL,
                      json={"secret": core.WEBHOOK_SECRET, "validar": False, "decode_engine": "nvdec"},
                      timeout=45)
    cams = [c for c in r.json().get("cameras", []) if c.get("stream_url")]
    return cams


def shard(cams):
    """fatia estavel entre workers (por hash do id) — cada camera cai em 1 worker so."""
    if WORKER_COUNT <= 1:
        return cams
    return [c for c in cams if (hash(str(c["id"])) % WORKER_COUNT) == WORKER_ID]


# ---------------- runtime por camera ----------------
class CamRuntime:
    def __init__(self, cam, idx):
        self.cam = cam
        self.idx = idx
        self.src = None
        self.born = 0.0          # quando a fonte comecou a entregar frames (warmup)
        self.last_reopen = 0.0
        self.use_nvdec = True    # NVDEC (GPU) ou decode CPU — decidido no sync pelo budget
        self.prev_gray = None    # frame cinza anterior (frame-diff de movimento)
        self.last_yolo = 0.0     # ultimo YOLO (p/ o piso de scan do gating)
        self.last_mov_alert = 0.0

    def _resolve_url(self):
        # camera de embed -> re-resolve pro m3u8 ATUAL (liveNN rotaciona); senao usa o guardado
        embed = (self.cam.get("embed_url") or "")
        if "/camera/embed/" in embed:
            fresh = resolve_embed(embed)
            if fresh:
                return fresh
        return (self.cam.get("stream_url") or "").strip()

    def open(self):
        url = self._resolve_url()
        if not url:
            print(f"[nvdec] {self.cam['nome']} sem URL — pula", flush=True)
            return False
        wh = ffprobe_wh(url)
        if not wh:
            print(f"[nvdec] {self.cam['nome']} OFFLINE (ffprobe) — pula por ora", flush=True)
            return False
        tw, th = target_size(*wh)
        self.src = NvdecSource(url, referer=REFERER, gpu=WORKER_GPU, out_w=tw, out_h=th,
                               fps=NVDEC_FPS, name=self.cam["nome"], nvdec=self.use_nvdec).start()
        modo = f"NVDEC/GPU{WORKER_GPU}" if self.use_nvdec else "CPU"
        print(f"[nvdec] {self.cam['nome']} {wh[0]}x{wh[1]} -> decode {tw}x{th} [{modo}]", flush=True)
        return True

    def close(self):
        if self.src:
            self.src.stop()
            self.src = None


def main():
    print(f"=== Corexia NVDEC worker {WORKER_ID}/{WORKER_COUNT} | GPU {WORKER_GPU} | "
          f"maxdim={NVDEC_MAXDIM} fps={NVDEC_FPS} ===", flush=True)
    # modelos carregam ON-DEMAND (get_model_cached), so quando uma camera habilita o analitico
    # na tela "Analiticos por Camera". Camera sem config nao carrega nem roda modelo nenhum.
    print("[nvdec] modelos ON-DEMAND por camera (config Analiticos por Camera)", flush=True)

    runtimes = {}   # camera_id -> CamRuntime
    last_fetch = 0.0
    last_cfg = 0.0
    CONFIG_REFRESH = int(os.getenv("CONFIG_REFRESH", "30"))
    period = 1.0 / max(NVDEC_FPS, 0.5)

    while True:
        # (re)sincroniza a lista de cameras. Quando ainda nao ha camera aberta, re-tenta
        # a cada RETRY_VAZIO s (nao a cada iteracao) pra nao spammar ffprobe em stream offline.
        RETRY_VAZIO = int(os.getenv("NVDEC_RETRY_VAZIO", "20"))
        if time.time() - last_fetch >= (SYNC_INTERVAL if runtimes else RETRY_VAZIO):
            try:
                cams = shard(fetch_cameras())
                if MAX_CAMS > 0:
                    cams = cams[:MAX_CAMS]     # ramp: limita nº de câmeras por worker
            except Exception as e:
                print(f"[nvdec] erro fetch cameras: {e}", flush=True)
                cams = []
            last_fetch = time.time(); last_cfg = time.time()
            wanted = {str(c["id"]): c for c in cams}
            # remove as que sairam
            for cid in list(runtimes):
                if cid not in wanted:
                    runtimes[cid].close(); del runtimes[cid]
                    print(f"[nvdec] camera {cid} removida", flush=True)
            # adiciona as novas (hibrido: primeiras NVDEC_BUDGET usam NVDEC/GPU, resto CPU)
            for cid, c in wanted.items():
                if cid not in runtimes:
                    nvdec_now = sum(1 for r in runtimes.values() if r.use_nvdec and r.src)
                    rt = CamRuntime(c, len(runtimes) + 1)
                    rt.use_nvdec = nvdec_now < NVDEC_BUDGET
                    if rt.open():
                        runtimes[cid] = rt
                else:
                    # camera ja aberta: refaz os metadados (config_analitico/ia_placa/cliente)
                    # SEM reabrir o stream -> edicao na tela "Analiticos por Camera" vale no proximo sync
                    runtimes[cid].cam = c
            # reindexa e publica no core (source_id -> cam)
            core.cam_by_idx = {}
            for i, (cid, rt) in enumerate(runtimes.items()):
                rt.idx = i
                core.cam_by_idx[i] = rt.cam

        # refresh LEVE da config (sem ffprobe): edicao de analiticos por camera vale em ~30s
        if runtimes and (time.time() - last_cfg) >= CONFIG_REFRESH:
            try:
                _fresh = {str(c["id"]): c for c in shard(fetch_cameras())}
                for _cid, _rt in runtimes.items():
                    if _cid in _fresh:
                        _rt.cam = _fresh[_cid]
                core.cam_by_idx = {i: _rt.cam for i, _rt in enumerate(runtimes.values())}
            except Exception as _e:
                print(f"[nvdec] cfg refresh erro: {_e}", flush=True)
            last_cfg = time.time()

        if not runtimes:
            time.sleep(2); continue

        # 1 volta de inferencia por todas as cameras deste worker
        t_cycle = time.time()
        for cid, rt in list(runtimes.items()):
            if rt.src is None:
                continue
            frame, ts = rt.src.read()
            now = time.time()
            if frame is None or (now - ts) > STALE_SEG:
                continue
            if rt.born == 0.0:
                rt.born = now
            if now - rt.born < WARMUP_SEG:      # warmup por camera: descarta frames de reconexao
                continue
            fobj = Frame(rt.idx, frame)
            # MOVIMENTO (frame-diff barato): alerta opt-in + fracao da cena p/ o gating
            mov = motion_frac(rt, frame, now)
            # GATING: so roda o YOLO pesado se houve MOVIMENTO, ou passou o piso de scan
            # (piso = roda mesmo parado a cada FLOOR_SEC — rede de seguranca p/ arma em cena estatica).
            # Isso corta a maioria das inferencias (cena parada) -> libera CPU/GPU p/ escalar.
            if MOTION_GATE and mov < GATE_FRAC and (now - rt.last_yolo) < FLOOR_SEC:
                continue
            rt.last_yolo = now
            # MODELOS ON-DEMAND: so os que ESTA camera habilitou na tela (config + horario).
            # Camera sem config -> conjunto vazio -> nao roda inferencia nenhuma.
            needed = core.models_for_cam(rt.cam, now)
            for mid in needed:
                # placa so em camera opt-in (cam.ia_placa)
                if mid == core.MODEL_ID_PLATE and not rt.cam.get("ia_placa"):
                    continue
                model = get_model_cached(mid)
                if model is None:
                    continue
                try:
                    resp = model.infer(frame)
                    resp = resp[0] if isinstance(resp, list) else resp
                    core._process(preds_to_dict(resp), fobj)
                except Exception as e:
                    print(f"[nvdec:infer:{mid}] {rt.cam['nome']}: {str(e)[:120]}", flush=True)

        # REABERTURA de camera travada: fonte sem frame ha muito tempo -> reabre (re-resolve
        # o embed pro m3u8 fresco). Evita camera presa numa URL de servidor que rotacionou.
        now2 = time.time()
        for cid, rt in list(runtimes.items()):
            if rt.src and rt.src.age() > REOPEN_STALE and (now2 - rt.last_reopen) > REOPEN_STALE:
                print(f"[nvdec] {rt.cam['nome']} travada ha {int(rt.src.age())}s -> reabrindo", flush=True)
                rt.close(); rt.born = 0.0; rt.last_reopen = now2
                rt.open()

        # cadencia: nao passa de NVDEC_FPS por camera
        dt = time.time() - t_cycle
        if dt < period:
            time.sleep(period - dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[nvdec] encerrando...", flush=True)
