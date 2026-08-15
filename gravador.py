"""
Corexia Gravador — grava as cameras em segmentos MP4 nesta maquina (por enquanto;
depois migra pra maquina de armazenamento com varios HDs).

- Puxa TODAS as cameras do backend (/listarCamerasGravacao, secret).
- Fonte: rtsp_url/stream_url direto; se so tem embed do YouTube, resolve o HLS
  real com yt-dlp (re-resolve quando o ffmpeg cair — URLs do YT expiram).
- ffmpeg -c copy em segmentos de GRAVACAO_SEGMENTO_SEG (default 300s = 5min),
  arquivos: gravacoes/<camera_id>/YYYY-MM-DD_HH-MM-SS.mp4
- Retencao: apaga > GRAVACAO_RETENCAO_HORAS (default 48h) e mantem o total
  abaixo de GRAVACAO_MAX_GB (default 15GB) apagando os mais antigos.

E' iniciado como processo filho do server.py (GRAVADOR_ATIVO=true).
"""
import os, re, sys, time, subprocess, signal
import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

BACKEND   = os.getenv("BACKEND_URL", "http://localhost:8000")
SECRET    = os.getenv("WEBHOOK_SECRET", "corexia-webhook-2024")
STREAM_REFERER = os.getenv("STREAM_REFERER", "")   # hotlink protection (ex.: analitico)
GRAV_DIR  = os.path.join(HERE, "gravacoes")
LIVE_DIR  = os.path.join(HERE, "gravacoes_live")   # HLS ao vivo (janela curta) p/ a IA + viewing
GRAVAR_MP4 = os.getenv("GRAVACAO_MP4", "true").lower() == "true"  # OFF ate ligar maquina de HDs
SEG_SEG   = int(os.getenv("GRAVACAO_SEGMENTO_SEG", "300"))
RET_HORAS = float(os.getenv("GRAVACAO_RETENCAO_HORAS", "48"))
MAX_GB    = float(os.getenv("GRAVACAO_MAX_GB", "15"))
SYNC_SEG  = int(os.getenv("GRAVACAO_SYNC_SEG", "60"))
YTDLP     = os.path.join(HERE, "venv", "bin", "yt-dlp")
if not os.path.exists(YTDLP):
    YTDLP = "yt-dlp"

os.makedirs(GRAV_DIR, exist_ok=True)
os.makedirs(LIVE_DIR, exist_ok=True)

procs = {}      # camera_id -> subprocess.Popen (ffmpeg)
yt_cache = {}   # camera_id -> (url_resolvida, timestamp)
YT_RE = re.compile(r"(?:embed/|v=|youtu\.be/)([A-Za-z0-9_-]{6,})")


def log(*a):
    print("[gravador]", *a, flush=True)


def fetch_cameras():
    r = requests.post(f"{BACKEND}/listarCamerasGravacao", json={"secret": SECRET}, timeout=20)
    return r.json().get("cameras", [])


def resolve_fonte(cam):
    """URL gravavel da camera (direta ou resolvida do YouTube)."""
    url = (cam.get("rtsp_url") or cam.get("stream_url") or "").strip()
    if url:
        return url
    emb = cam.get("embed_url") or ""
    m = YT_RE.search(emb)
    if not (m and ("youtube" in emb or "youtu.be" in emb)):
        return None
    cid = cam["id"]
    cached = yt_cache.get(cid)
    if cached and time.time() - cached[1] < 3 * 3600:
        return cached[0]
    try:
        out = subprocess.run(
            [YTDLP, "-g", "-f", "best[height<=720]/best",
             f"https://www.youtube.com/watch?v={m.group(1)}"],
            capture_output=True, text=True, timeout=90)
        u = (out.stdout or "").strip().splitlines()
        if u:
            yt_cache[cid] = (u[0], time.time())
            log(f"youtube resolvido: {cam.get('nome','')} ")
            return u[0]
        log(f"yt-dlp sem saida p/ {cam.get('nome','')}: {(out.stderr or '')[:120]}")
    except Exception as e:
        log("yt-dlp erro:", str(e)[:120])
    return None


def start_ffmpeg(cam, src):
    live = os.path.join(LIVE_DIR, cam["id"])
    os.makedirs(live, exist_ok=True)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if src.startswith("rtsp"):
        args += ["-rtsp_transport", "tcp"]
    if src.startswith("http"):
        if STREAM_REFERER:
            args += ["-referer", STREAM_REFERER]   # hotlink protection do analitico
        # HLS: comeca no ULTIMO segmento (default -3 = ~9s extras de atraso no ao vivo)
        args += ["-live_start_index", "-1"]
    args += ["-i", src]
    if GRAVAR_MP4:
        # saida 1: gravacao em segmentos MP4 (so quando GRAVACAO_MP4=true)
        d = os.path.join(GRAV_DIR, cam["id"]); os.makedirs(d, exist_ok=True)
        args += ["-c", "copy", "-f", "segment", "-segment_time", str(SEG_SEG),
                 "-reset_timestamps", "1", "-strftime", "1",
                 "-segment_format_options", "movflags=+faststart",
                 os.path.join(d, "%Y-%m-%d_%H-%M-%S.mp4")]
    # saida SEMPRE: HLS ao vivo local (viewing + IA) — janela curta, nao acumula disco
    args += ["-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
             "-hls_flags", "delete_segments+omit_endlist",
             os.path.join(live, "index.m3u8")]
    logf = open(os.path.join(live, "ffmpeg.log"), "ab")
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=logf)
    log(f"{'gravando+' if GRAVAR_MP4 else ''}live {cam.get('nome','')} (pid {p.pid})")
    return p


def limpeza():
    """Retencao: por idade e por espaco total."""
    agora = time.time()
    arquivos = []
    for cid in os.listdir(GRAV_DIR):
        d = os.path.join(GRAV_DIR, cid)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".mp4"):
                continue
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if agora - st.st_mtime > RET_HORAS * 3600:
                os.remove(p)
            else:
                arquivos.append((st.st_mtime, st.st_size, p))
    total = sum(s for _, s, _ in arquivos)
    if total > MAX_GB * 1024 ** 3:
        arquivos.sort()  # mais antigo primeiro
        for _, s, p in arquivos:
            try:
                os.remove(p); total -= s
            except OSError:
                pass
            if total <= MAX_GB * 1024 ** 3 * 0.9:
                break


def parar_tudo(*_):
    for p in procs.values():
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, parar_tudo)
signal.signal(signal.SIGINT, parar_tudo)

log(f"iniciado | segmento={SEG_SEG}s retencao={RET_HORAS}h max={MAX_GB}GB")
while True:
    try:
        cams = fetch_cameras()
    except Exception as e:
        log("erro ao buscar cameras:", str(e)[:100])
        time.sleep(30)
        continue

    ids_atuais = {c["id"] for c in cams}
    # para gravacoes de cameras removidas
    for cid in list(procs):
        if cid not in ids_atuais:
            try:
                procs[cid].terminate()
            except Exception:
                pass
            procs.pop(cid, None)
            log("camera removida, gravacao parada:", cid)

    # inicia/reinicia ffmpeg de cada camera
    for cam in cams:
        cid = cam["id"]
        p = procs.get(cid)
        if p and p.poll() is None:
            continue          # ja gravando
        if p:                 # morreu (stream caiu / URL do YT expirou)
            yt_cache.pop(cid, None)
        src = resolve_fonte(cam)
        if src:
            procs[cid] = start_ffmpeg(cam, src)

    try:
        limpeza()
    except Exception as e:
        log("limpeza erro:", str(e)[:100])

    time.sleep(SYNC_SEG)
