"""
LP Corexia + analytics: rotas /lp, /lp/admin e /api/lp/*.

Banco proprio (lp_analytics.db), isolado do corexia.db do sistema.
Incluido pelo server.py (app.include_router) junto com o mount /lp-assets.
Senha do painel: env LP_ADMIN_PASSWORD (default corexia123).
"""
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

HERE = os.path.dirname(os.path.abspath(__file__))
LP_DIR = os.path.join(HERE, "lp")
DB_PATH = os.path.join(HERE, "lp_analytics.db")
ADMIN_PASSWORD = os.getenv("LP_ADMIN_PASSWORD", "corexia123")
# fuso do negocio: o servidor roda em UTC, entao "hoje" precisa de ancora explicita
LP_TZ = ZoneInfo(os.getenv("LP_TZ", "America/Recife"))

EVENT_TYPES = {"pageview", "cta_click", "form_open", "step_reached", "form_complete"}

router = APIRouter()

_db_lock = threading.Lock()
_tokens = set()
_geo_cache = {}


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _init_db():
    with _connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                type TEXT NOT NULL,
                label TEXT,
                session_id TEXT,
                ua TEXT,
                ip TEXT,
                city TEXT,
                region TEXT,
                lat REAL,
                lng REAL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS leads(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                nome TEXT,
                provedor TEXT,
                whatsapp TEXT,
                cidade TEXT,
                momento TEXT,
                session_id TEXT,
                origem TEXT)"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_lp_events_ts ON events(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_lp_events_type ON events(type)")


_init_db()


def _now_ms():
    return int(time.time() * 1000)


def _start_of_today_ms():
    hoje = datetime.now(LP_TZ).date()
    return int(datetime.combine(hoje, dtime.min, tzinfo=LP_TZ).timestamp() * 1000)


def _client_ip(request):
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "") or ""


def _is_private(ip):
    if not ip:
        return True
    ip = ip.replace("::ffff:", "")
    if ip in ("::1", "localhost"):
        return True
    if ip.startswith(("127.", "10.", "192.168.", "fe80", "fc", "fd")):
        return True
    if ip.startswith("172."):
        try:
            octeto = int(ip.split(".")[1])
            return 16 <= octeto <= 31
        except (ValueError, IndexError):
            return False
    return False


def _lookup_ip(ip):
    # ip vazio: a ip-api.com resolve o IP publico de quem chamou (a rede do servidor)
    url = "http://ip-api.com/json/%s?fields=status,city,regionName,lat,lon" % ip
    with urllib.request.urlopen(url, timeout=6) as resp:
        j = json.load(resp)
    if j.get("status") != "success":
        return None
    return {
        "city": j.get("city"),
        "region": j.get("regionName"),
        "lat": j.get("lat"),
        "lng": j.get("lon"),
    }


def _resolve_geo(ip, session_id):
    """Localizacao REAL do visitante ou nenhuma.

    IP privado (rede local/VPN) nao tem geolocalizacao publica. Nesse caso
    gravamos NULL: usar a geo do proprio servidor plotaria o visitante na
    cidade da Xeon, inventando a origem dele.
    """
    key = session_id or ip
    if key in _geo_cache:
        return _geo_cache[key]
    clean = (ip or "").replace("::ffff:", "")
    if _is_private(clean):
        return None
    try:
        geo = _lookup_ip(clean)
    except Exception:
        geo = None
    if geo:
        if len(_geo_cache) > 10000:
            _geo_cache.clear()
        _geo_cache[key] = geo
    return geo


# ---------------- Paginas ----------------

@router.get("/lp", include_in_schema=False)
def lp_page():
    return FileResponse(
        os.path.join(LP_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/lp/admin", include_in_schema=False)
def lp_admin_page():
    return FileResponse(
        os.path.join(LP_DIR, "admin.html"),
        headers={"Cache-Control": "no-cache"},
    )


# ---------------- Tracking ----------------

@router.post("/api/lp/track")
def lp_track(payload: dict, request: Request):
    etype = (payload or {}).get("type")
    if etype not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail="tipo invalido")
    session_id = payload.get("session_id")
    ip = _client_ip(request).replace("::ffff:", "")
    geo = _resolve_geo(ip, session_id)
    ua = (request.headers.get("user-agent") or "")[:300]
    with _db_lock, _connect() as con:
        con.execute(
            "INSERT INTO events (ts,type,label,session_id,ua,ip,city,region,lat,lng)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _now_ms(), etype, payload.get("label"), session_id, ua, ip,
                geo["city"] if geo else None,
                geo["region"] if geo else None,
                geo["lat"] if geo else None,
                geo["lng"] if geo else None,
            ),
        )
    return Response(status_code=204)


@router.post("/api/lp/lead")
def lp_lead(payload: dict):
    nome = (payload or {}).get("nome")
    whatsapp = payload.get("whatsapp")
    if not nome or not whatsapp:
        raise HTTPException(status_code=400, detail="nome e whatsapp sao obrigatorios")
    with _db_lock, _connect() as con:
        con.execute(
            "INSERT INTO leads (ts,nome,provedor,whatsapp,cidade,momento,session_id,origem)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                _now_ms(),
                str(nome)[:100],
                str(payload.get("provedor") or "")[:120],
                str(whatsapp)[:20],
                str(payload.get("cidade") or "")[:100],
                str(payload.get("momento") or "")[:60],
                payload.get("session_id"),
                str(payload.get("origem") or "")[:40],
            ),
        )
    return JSONResponse({"ok": True}, status_code=201)


# ---------------- Painel /lp/admin ----------------

@router.post("/api/lp/login")
def lp_login(payload: dict):
    if (payload or {}).get("password") != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="senha incorreta")
    token = secrets.token_hex(24)
    _tokens.add(token)
    return {"token": token}


def _require_auth(authorization):
    token = (authorization or "").replace("Bearer ", "", 1)
    if token not in _tokens:
        raise HTTPException(status_code=401, detail="nao autorizado")


@router.get("/api/lp/stats")
def lp_stats(authorization: str = Header(default="")):
    _require_auth(authorization)
    now = _now_ms()
    today_start = _start_of_today_ms()
    h24 = now - 24 * 3600 * 1000
    m5 = now - 5 * 60 * 1000

    with _connect() as con:
        def q1(sql, *params):
            return con.execute(sql, params).fetchone()[0]

        kpis = {
            "visitorsToday": q1(
                "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='pageview' AND ts>=?",
                today_start,
            ),
            "activeNow": q1(
                "SELECT COUNT(DISTINCT session_id) FROM events WHERE ts>=?", m5
            ),
            "ctaClicks": q1(
                "SELECT COUNT(*) FROM events WHERE type='cta_click' AND ts>=?", today_start
            ),
            "formsStarted": q1(
                "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='form_open' AND ts>=?",
                today_start,
            ),
            "formsCompleted": q1(
                "SELECT COUNT(DISTINCT session_id) FROM events WHERE type='form_complete' AND ts>=?",
                today_start,
            ),
        }
        points = [dict(r) for r in con.execute(
            "SELECT session_id, city, region, lat, lng, MAX(ts) AS last_seen FROM events"
            " WHERE lat IS NOT NULL AND ts>=? GROUP BY session_id"
            " ORDER BY last_seen DESC LIMIT 300",
            (h24,),
        )]
        # sessoes das ultimas 24h sem localizacao real (IP privado/VPN)
        sessions24h = q1(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE ts>=?", h24
        )
        sessions_geo = q1(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE lat IS NOT NULL AND ts>=?", h24
        )
        no_geo = max(0, sessions24h - sessions_geo)

        feed = [dict(r) for r in con.execute(
            "SELECT id, ts, type, label, city, region FROM events ORDER BY id DESC LIMIT 30"
        )]
        by_hour = {}
        for r in con.execute(
            "SELECT (ts/3600000) AS h, COUNT(DISTINCT session_id) AS c FROM events"
            " WHERE type='pageview' AND ts>=? GROUP BY h",
            (h24,),
        ):
            by_hour[int(r["h"])] = r["c"]
        now_h = now // 3600000
        hourly = [
            {"ts": (now_h - i) * 3600000, "count": by_hour.get(now_h - i, 0)}
            for i in range(23, -1, -1)
        ]
        funnel = {
            "opened": q1("SELECT COUNT(DISTINCT session_id) FROM events WHERE type='form_open'"),
            "steps": [
                q1("SELECT COUNT(DISTINCT session_id) FROM events WHERE type='step_reached' AND label=?", str(n))
                for n in range(1, 6)
            ],
            "completed": q1("SELECT COUNT(DISTINCT session_id) FROM events WHERE type='form_complete'"),
        }
        leads = [dict(r) for r in con.execute(
            "SELECT id, ts, nome, provedor, whatsapp, cidade, momento FROM leads ORDER BY ts DESC"
        )]

    return {
        "now": now, "kpis": kpis, "points": points, "feed": feed,
        "hourly": hourly, "funnel": funnel, "leads": leads,
        "sessions24h": sessions24h, "noGeo": no_geo,
    }
