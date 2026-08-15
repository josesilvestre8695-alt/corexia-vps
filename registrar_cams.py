"""
Cadastra as cameras da lista (id|url) no Corexia como entidades Camera, marcadas
decode_engine=nvdec. Resolve embed->m3u8 e guarda AMBOS (rtsp_url resolvido + embed_url
original, pro runner re-resolver se o servidor liveNN rotacionar). Idempotente por analitico_id.
Uso: ./venv/bin/python registrar_cams.py [cameras.txt]
"""
import os, sys, json, sqlite3, secrets, re, datetime
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "corexia.db")
LIST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "cameras.txt")
REF  = os.getenv("STREAM_REFERER", "https://analitico.grupocorexia.com.br/")
_EMBED_RE = re.compile(r"let live = '([^']+)'")

def resolve_embed(url):
    try:
        html = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0", "Referer": REF}).text
        m = _EMBED_RE.search(html)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"  [resolve] {str(e)[:70]}", flush=True)
    return ""

def slug_de(url, is_embed, aid):
    try:
        base = url.split("?", 1)[0].rstrip("/")
        if is_embed:
            return base.split("/")[-2] or f"cam-{aid}"        # .../<slug>/18712
        return base.split("/")[-1].replace(".m3u8", "") or f"cam-{aid}"
    except Exception:
        return f"cam-{aid}"

def now_iso():
    return datetime.datetime.utcnow().isoformat()

c = sqlite3.connect(DB, timeout=20)
c.row_factory = sqlite3.Row
existing = set()
for r in c.execute("SELECT data FROM entities WHERE entity='Camera'"):
    aid = json.loads(r["data"]).get("analitico_id")
    if aid:
        existing.add(str(aid))

novas = puladas = sem_stream = 0
for ln in open(LIST, encoding="utf-8"):
    ln = ln.strip()
    if not ln or "|" not in ln:
        continue
    aid, url = [x.strip() for x in ln.split("|", 1)]
    if aid in existing:
        puladas += 1; continue
    is_embed = "/camera/embed/" in url
    rtsp = resolve_embed(url) if is_embed else url
    if not rtsp or ".m3u8" not in rtsp:
        sem_stream += 1     # nao resolveu agora; guarda embed_url pro runner resolver depois
        rtsp = ""
    data = {
        "nome": slug_de(url, is_embed, aid),
        "analitico_id": aid,
        "rtsp_url": rtsp,
        "embed_url": url if is_embed else "",
        "decode_engine": "nvdec",
        "ia_placa": False,
        "status": "ativo",
    }
    eid = secrets.token_hex(12); ts = now_iso()
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("Camera", eid, json.dumps(data), ts, ts))
    existing.add(aid); novas += 1
    print(f"  + {data['nome']} ({aid}) {'m3u8' if rtsp else 'SO-EMBED'}", flush=True)

c.commit()
tot = c.execute("SELECT COUNT(*) FROM entities WHERE entity='Camera'").fetchone()[0]
c.close()
print(f"\n=== NOVAS={novas} PULADAS={puladas} SEM_M3U8_AGORA={sem_stream} | total Cameras no DB={tot} ===", flush=True)
