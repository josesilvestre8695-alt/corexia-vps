"""
Corexia Backend — 100% proprio, MULTI-TENANT, roda na Xeon (SEM Base44).

Papeis:
  admin    -> Corexia. Ve TUDO. Cadastra cameras e libera pra provedores.
  provedor -> ISP/revenda. Ve SO o que tem provedor_id dele (cameras liberadas,
              clientes dele, alertas dele). Cria clientes finais e atribui cameras.
  cliente  -> cliente final (morador/lojista). Ve SO o que tem cliente_id dele
              (cameras atribuidas, alertas, gravacoes). Usa o portal/PWA.

- POST /webhookAlertas    : alerta do detector -> SQLite + foto + WhatsApp + entidade Alerta
- POST /listarCamerasIA   : cameras com stream pro detector (secret)
- POST /listarCamerasGravacao : todas as cameras pro gravador (secret)
- /api/auth/*             : login/me/logout com senha hash (pbkdf2) + sessao em SQLite
- /api/users              : gestao de logins (escopado por papel)
- /api/entities/{name}    : CRUD generico ESCOPADO por papel
- /api/gravacoes*         : lista/serve gravacoes (escopado)
- GET /                   : SPA (painel React)

Rodar:  ./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
"""
import os, sys, json, base64, sqlite3, asyncio, secrets, hashlib, hmac, re, time, threading, subprocess
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

def _env_bool(k, d="0"):
    """Lê boolean do .env de forma tolerante (1/true/yes/on), evitando a pegadinha
    de '==\"1\"' que ignora 'true'. Usado nos flags de gate."""
    return os.getenv(k, d).strip().lower() in ("1", "true", "yes", "on")
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET or WEBHOOK_SECRET == "corexia-webhook-2024":
    raise RuntimeError("WEBHOOK_SECRET forte e OBRIGATORIO no .env (nao use o default publico). "
                       "Gere: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"")

def _secret_ok(b):
    return hmac.compare_digest(str(b.get("secret") or ""), WEBHOOK_SECRET)
# Provedor de WhatsApp: "evolution" (gratis, self-hosted) ou "zapi" (pago)
WHATSAPP_PROVIDER  = os.getenv("WHATSAPP_PROVIDER", "evolution").lower()
EVOLUTION_URL      = os.getenv("EVOLUTION_URL", "http://localhost:8080")
EVOLUTION_APIKEY   = os.getenv("EVOLUTION_APIKEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "corexia")
ZAPI_INSTANCE  = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN     = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT    = os.getenv("ZAPI_CLIENT_TOKEN", "")

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "corexia.db")
IMG_DIR = os.path.join(HERE, "alertas_img")
GRAV_DIR = os.getenv("GRAV_DIR") or os.path.join(HERE, "gravacoes")
LIVE_DIR = os.path.join(HERE, "gravacoes_live")   # HLS ao vivo (restream do gravador) p/ a IA
UPLOAD_DIR = os.path.join(HERE, "uploads")        # imagens enviadas (ex: foto da camera)
CAMERAS_FILE = os.path.join(HERE, "cameras_saas.json")
GRAVADOR_ATIVO = os.getenv("GRAVADOR_ATIVO", "true").lower() == "true"
# Referer enviado ao validar/consumir streams http(s) com protecao de hotlink (ex.: analitico)
STREAM_REFERER = os.getenv("STREAM_REFERER", "")
# Web Push (notificacao no PC + app do cliente)
VAPID_PUBLIC  = os.getenv("VAPID_PUBLIC", "")
VAPID_PRIV    = os.path.join(HERE, "vapid_private.pem")
VAPID_SUB     = os.getenv("VAPID_SUB", "mailto:contato@grupocorexia.com.br")
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(GRAV_DIR, exist_ok=True)
os.makedirs(LIVE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- resolucao YouTube -> HLS (pra IA analisar cameras de teste do YouTube) ---
YT_RE = re.compile(r"(?:embed/|v=|youtu\.be/)([A-Za-z0-9_-]{6,})")
YTDLP = os.path.join(HERE, "venv", "bin", "yt-dlp")
if not os.path.exists(YTDLP):
    YTDLP = "yt-dlp"
_yt_urls = {}   # cam_id -> (hls_url, ts)

TIPO_EMOJI = {"fogo":"🔥","arma_fogo":"🔫","arma_branca":"🔪","placa":"🚗",
              "movimento":"📡","intruso":"🚨","aglomeracao":"👥","outro":"🔔"}
TIPO_LABEL = {"fogo":"FOGO / FUMACA DETECTADO","arma_fogo":"ARMA DE FOGO DETECTADA",
              "arma_branca":"ARMA BRANCA DETECTADA","placa":"PLACA DETECTADA",
              "movimento":"MOVIMENTO DETECTADO","intruso":"INTRUSO DETECTADO",
              "aglomeracao":"AGLOMERACAO DETECTADA","outro":"ALERTA DE SEGURANCA"}


def db():
    c = sqlite3.connect(DB, timeout=15); c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")   # espera o lock em vez de estourar 'database is locked'
    return c

def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def init_db():
    c = db()
    c.execute("PRAGMA journal_mode=WAL")   # concorrencia: leitores nao bloqueiam o escritor
    c.execute("""CREATE TABLE IF NOT EXISTS alertas (
        id INTEGER PRIMARY KEY AUTOINCREMENT, camera_nome TEXT, camera_id TEXT,
        cliente_nome TEXT, cliente_telefone TEXT, tipo TEXT, descricao TEXT,
        confianca INTEGER, imagem TEXT, whatsapp INTEGER DEFAULT 0, criado TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS entities (
        entity TEXT, id TEXT, data TEXT, created_date TEXT, updated_date TEXT,
        PRIMARY KEY (entity, id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT, full_name TEXT,
        role TEXT, provedor_id TEXT, cliente_id TEXT, status TEXT DEFAULT 'ativo',
        created TEXT)""")
    for _col, _typ in (("menu_perms", "TEXT"), ("equipe", "INTEGER DEFAULT 0")):
        try:
            c.execute("ALTER TABLE users ADD COLUMN %s %s" % (_col, _typ))
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT, created TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS push_subs (
        endpoint TEXT PRIMARY KEY, user_id TEXT, sub TEXT, created TEXT)""")
    c.commit(); c.close()

init_db()


# ==================== AUTH (senha hash + sessao persistente) ====================
def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}${h}"

def _check_pw(pw, stored):
    try:
        salt, h = stored.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False

def _seed_admin():
    c = db()
    n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if n == 0:
        email = (os.getenv("ADMIN_EMAIL") or "admin@corexia.com").lower()
        pw = os.getenv("ADMIN_PASSWORD")
        if not pw:
            pw = secrets.token_urlsafe(12)
            print(f"[auth] ADMIN_PASSWORD nao setado — senha admin temporaria (TROQUE): {pw}")
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  ("admin", email, _hash_pw(pw), "Admin Corexia", "admin", "", "", "ativo", _now_iso()))
        c.commit()
        print(f"[auth] admin criado: {email}")
    c.close()

_seed_admin()

def _demo_ativo(uid):
    """AcessoDemo ATIVO e nao vencido do usuario (ou None)."""
    if not uid:
        return None
    c = db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='AcessoDemo' "
                     "AND json_extract(data,'$.user_id')=?", (uid,)).fetchall()
    c.close()
    hoje = datetime.now().strftime("%Y-%m-%d")
    for _r in rows:
        d = json.loads(_r["data"])
        if d.get("status") == "ativo" and str(d.get("expira") or "9999-12-31") >= hoje:
            return {"id": _r["id"], "cameras": d.get("cameras") or [], "expira": d.get("expira")}
    return None


def _expira_demos():
    """Marca demos vencidas como 'expirado' e derruba as sessoes (perde acesso na hora)."""
    c = db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='AcessoDemo'").fetchall()
    hoje = datetime.now().strftime("%Y-%m-%d"); n = 0
    for _r in rows:
        d = json.loads(_r["data"])
        if d.get("status") == "ativo" and str(d.get("expira") or "") and str(d.get("expira")) < hoje:
            d["status"] = "expirado"; d["expirado_em"] = _now_iso()
            c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='AcessoDemo' AND id=?",
                      (json.dumps(d), _now_iso(), _r["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (d.get("user_id"),))
            n += 1
    c.commit(); c.close()
    return n


def _user_public(r):
    out = {"id": r["id"], "email": r["email"], "full_name": r["full_name"],
           "role": r["role"], "provedor_id": r["provedor_id"] or "",
           "cliente_id": r["cliente_id"] or "", "status": r["status"]}
    try:
        _dm = _demo_ativo(r["id"])
    except Exception:
        _dm = None
    if _dm:
        out["user_type"] = "demonstrator"
        out["demo_cameras"] = _dm["cameras"]
        out["demo_expira"] = _dm["expira"]
        return out
    try:
        _su = _subuser_by_uid(r["id"])
    except Exception:
        _su = None
    if _su:
        out["user_type"] = "subuser"
        out["subuser_id"] = _su.get("id")
        out["allowed_cameras"] = _su.get("allowed_cameras") or []
        out["allowed_gravacoes"] = _su.get("allowed_gravacoes") or []
        out["allowed_mosaicos"] = _su.get("allowed_mosaicos") or []
        out["subuser_alertas"] = bool(_su.get("receber_alertas_whatsapp"))
        if _su.get("client_id"):
            out["cliente_id"] = _su["client_id"]
        _mst = _get_entity("Cliente", _su.get("client_id", "")) or {}
        out["sub_blocked"] = (_su.get("status") != "ativo") or (_mst.get("status") == "bloqueado")
    return out

SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))   # sessao expira apos N dias
LOGIN_MAX    = int(os.getenv("LOGIN_MAX", "8"))        # tentativas de login por IP...
LOGIN_WINDOW = 300                                     # ...em 5 min -> 429 (anti brute-force)
_login_fails = {}    # ip -> (count, inicio_janela_ts)

# MEDIA TOKEN assinado (mt1) — token CURTO so pra ?t= de midia, em vez de expor o
# token de sessao de 30d em query string (que vaza em log/link compartilhado)
MEDIA_TOKEN_TTL = int(os.getenv("MEDIA_TOKEN_TTL", str(6*3600)))   # 6h (era 24h): reduz janela de reuso

def _media_secret():
    """Segredo DEDICADO do media token — NUNCA o WEBHOOK_SECRET (senão qualquer nó que
    conhece o segredo do webhook forja token de usuario). Usa MEDIA_TOKEN_SECRET do .env;
    se ausente, gera e PERSISTE em .media_secret (estavel entre restarts, ao contrario de
    um token_hex volatil que invalidaria os mt1 a cada reboot)."""
    v = os.getenv("MEDIA_TOKEN_SECRET")
    if v:
        return v
    path = os.path.join(HERE, ".media_secret")
    try:
        if os.path.exists(path):
            return open(path).read().strip()
        s = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(s)
        try: os.chmod(path, 0o600)
        except OSError: pass
        return s
    except Exception:
        return secrets.token_hex(32)

_MEDIA_KEY = _media_secret().encode()

def _media_sign(uid, exp):
    return hmac.new(_MEDIA_KEY, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

def _media_user(tok):
    """Valida token mt1.<uid>.<exp>.<sig> e devolve o user. So chamado no ramo ?t=."""
    try:
        prefixo, uid, exp_s, sig = tok.split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    if exp <= time.time() or not hmac.compare_digest(sig, _media_sign(uid, exp)):
        return None
    c = db(); r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); c.close()
    if not r or r["status"] != "ativo":
        return None
    return _user_public(r)

def current_user(req: Request, allow_query_token=False):
    tok = (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    if not tok and allow_query_token:
        # ?t= SO em rotas de midia (<video>) — limita o alcance de um link/log vazado
        tok = req.query_params.get("t", "")
        # mt1 SO vale aqui (veio do ?t=) — no header Authorization NAO autentica
        if tok.startswith("mt1."):
            return _media_user(tok)
    if not tok:
        return None
    c = db()
    r = c.execute("SELECT u.*, s.created AS _s FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
    c.close()
    if not r or r["status"] != "ativo":
        return None
    try:   # sessao expira apos SESSION_DAYS dias (token vazado nao vale pra sempre)
        if (datetime.now() - datetime.strptime(r["_s"], "%Y-%m-%dT%H:%M:%S")).days >= SESSION_DAYS:
            return None
    except Exception:
        pass
    return _user_public(r)

def _unauth():
    return JSONResponse({"error": "nao autenticado"}, status_code=401)

def _forbidden(msg="sem permissao"):
    return JSONResponse({"error": msg}, status_code=403)


app = FastAPI(title="Corexia Backend")

# Unificacao de frontend (2026-08): as telas legadas server-rendered (/comercial/*) foram
# substituidas pelas paginas React ja publicadas. Redirecionamos as antigas para as novas.
# Fallback: qualquer URL antiga com ?legacy=1 ainda renderiza a versao legada (nada se perde).
_REDIR_NOVO = {
    # 2026-08-14: desvio /comercial/* -> SPA DESLIGADO. O admin usa os paineis
    # server-rendered que montamos (Opcao A). Mantido so o rumo p/ o proprio /comercial.
    "/config-analiticos": "/comercial/analiticos",
}
@app.middleware("http")
async def _redir_legado_comercial(request: Request, call_next):
    if request.method == "GET" and not request.query_params.get("legacy"):
        _dest = _REDIR_NOVO.get(request.url.path)
        if _dest:
            return RedirectResponse(_dest, status_code=302)
    return await call_next(request)


@app.post("/api/auth/login")
async def api_login(req: Request):
    ip = req.client.host if req.client else "?"
    now = time.time()
    cnt, ts = _login_fails.get(ip, (0, now))
    if now - ts > LOGIN_WINDOW:
        cnt, ts = 0, now
    if cnt >= LOGIN_MAX:
        return JSONResponse({"error": "muitas tentativas — espere alguns minutos"}, status_code=429)
    b = await req.json()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    c = db()
    r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not r or not _check_pw(pw, r["password_hash"]):
        c.close()
        _login_fails[ip] = (cnt + 1, ts)     # conta a falha (anti brute-force)
        return JSONResponse({"error": "email ou senha invalidos"}, status_code=401)
    if r["status"] != "ativo":
        c.close()
        return JSONResponse({"error": "usuario bloqueado"}, status_code=403)
    _login_fails.pop(ip, None)                # sucesso zera o contador do IP
    t = secrets.token_hex(24)
    c.execute("INSERT INTO sessions (token,user_id,created) VALUES (?,?,?)", (t, r["id"], _now_iso()))
    corte = (datetime.now() - timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    c.execute("DELETE FROM sessions WHERE created < ?", (corte,))   # higiene: limpa sessoes expiradas
    c.commit(); c.close()
    return {"token": t, "user": _user_public(r)}

@app.get("/api/auth/me")
async def api_me(req: Request):
    u = current_user(req)
    return u if u else _unauth()

@app.post("/api/auth/logout")
async def api_logout(req: Request):
    tok = (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    c = db(); c.execute("DELETE FROM sessions WHERE token=?", (tok,)); c.commit(); c.close()
    return {"success": True}


@app.post("/api/auth/change-password")
async def api_change_password(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    b = await req.json()
    atual = b.get("senha_atual") or b.get("current") or ""
    nova = b.get("nova_senha") or b.get("password") or ""
    if len(nova) < 6:
        return JSONResponse({"error": "a nova senha precisa de ao menos 6 caracteres"}, status_code=400)
    uid = u.get("id")
    c = db()
    if not uid:
        tok = (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
        srow = c.execute("SELECT user_id FROM sessions WHERE token=?", (tok,)).fetchone()
        uid = srow["user_id"] if srow else None
    if not uid:
        c.close(); return _unauth()
    r = c.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    if not r or not _check_pw(atual, r["password_hash"]):
        c.close(); return JSONResponse({"error": "senha atual incorreta"}, status_code=403)
    c.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(nova), uid))
    c.commit(); c.close()
    return {"success": True}


@app.get("/trocar-senha")
def trocar_senha_page():
    html = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Trocar senha</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0a0a0a;color:#fff;font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
.card{background:#161616;border:1px solid #262626;border-radius:14px;padding:24px;width:100%;max-width:360px}
h1{font-size:18px;margin:0 0 8px}p.sub{color:#888;font-size:13px;margin:0 0 8px}
label{display:block;font-size:13px;color:#aaa;margin:12px 0 4px}
input{width:100%;padding:10px;border-radius:8px;border:1px solid #333;background:#0e0e0e;color:#fff;font-size:15px}
button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:24px;background:#f97316;color:#111;font-weight:700;font-size:15px;cursor:pointer}
.msg{margin-top:12px;font-size:13px;min-height:18px}.ok{color:#22c55e}.err{color:#ef4444}
a.bk{color:#f97316;text-decoration:none;font-size:13px;display:inline-block;margin-top:16px}</style></head>
<body><div class='card'><h1>Trocar senha</h1><p class='sub'>Defina uma nova senha para a sua conta.</p>
<label>Senha atual</label><input id='a' type='password' autocomplete='current-password'>
<label>Nova senha (min 6)</label><input id='b' type='password' autocomplete='new-password'>
<label>Repita a nova senha</label><input id='c' type='password' autocomplete='new-password'>
<button id='go'>Salvar nova senha</button><div class='msg' id='m'></div>
<a class='bk' href='/'>&larr; Voltar ao painel</a></div>
<script>
var t=localStorage.getItem('corexia_token');var m=document.getElementById('m');
if(!t){m.className='msg err';m.textContent='Faca login primeiro.';}
document.getElementById('go').onclick=function(){
 var a=document.getElementById('a').value,b=document.getElementById('b').value,c=document.getElementById('c').value;
 m.className='msg';m.textContent='';
 if(b.length<6){m.className='msg err';m.textContent='A nova senha precisa de ao menos 6 caracteres.';return;}
 if(b!==c){m.className='msg err';m.textContent='As senhas nao conferem.';return;}
 fetch('/api/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+t},body:JSON.stringify({senha_atual:a,nova_senha:b})})
 .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
 .then(function(x){if(x.ok){m.className='msg ok';m.textContent='Senha alterada com sucesso!';document.getElementById('a').value='';document.getElementById('b').value='';document.getElementById('c').value='';}
  else{m.className='msg err';m.textContent=(x.j&&x.j.error)||'Erro ao trocar senha.';}})
 .catch(function(){m.className='msg err';m.textContent='Erro de conexao.';});
};
</script></body></html>"""
    return HTMLResponse(html)

@app.get("/api/auth/media-token")
async def api_media_token(req: Request):
    # troca o Bearer de sessao por um token curto assinado, so pra ?t= de midia
    u = current_user(req)   # SO Bearer de sessao (sem allow_query_token)
    if not u:
        return _unauth()
    exp = int(time.time()) + MEDIA_TOKEN_TTL
    return {"token": f"mt1.{u['id']}.{exp}.{_media_sign(u['id'], exp)}", "expires": exp}


# ==================== PROVEDOR/REVENDA TESTER (admin) ====================
def _plano_cloud():
    c = db()
    out = ("", "Painel Cloud")
    for r in c.execute("SELECT id, data FROM entities WHERE entity='Plano'").fetchall():
        d = json.loads(r["data"])
        if d.get("gravacao") == "cloud":
            out = (r["id"], d.get("nome", "Painel Cloud")); break
    c.close()
    return out


@app.post("/api/tester/criar")
async def tester_criar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    b = await req.json()
    nome = (b.get("nome") or "").strip()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    try:
        dias = int(b.get("dias") or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias not in (7, 14):
        dias = 7
    if not nome or not email or len(pw) < 4:
        return JSONResponse({"error": "nome, email e senha (min 4) obrigatorios"}, status_code=400)
    plano_id, plano_nome = _plano_cloud()
    trial_ate = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")
    pid = secrets.token_hex(12); now = _now_iso()
    prov = {"nome": nome, "email": email, "telefone": (b.get("telefone") or ""),
            "plano_id": plano_id, "plano_nome": plano_nome, "gravacao": "cloud",
            "status": "ativo", "tester": True, "trial_ate": trial_ate, "limite_cameras": 3,
            "criado_em": now}
    c = db()
    uid = secrets.token_hex(8)
    try:
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, email, _hash_pw(pw), nome, "provedor", pid, "", "ativo", now))
    except sqlite3.IntegrityError:
        c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("Provedor", pid, json.dumps(prov), now, now))
    c.commit(); c.close()
    return {"success": True, "provedor_id": pid, "trial_ate": trial_ate, "dias": dias, "limite_cameras": 3}


@app.get("/api/tester/listar")
async def tester_listar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    c = db()
    provs = c.execute("SELECT id, data FROM entities WHERE entity='Provedor'").fetchall()
    cams = c.execute("SELECT data FROM entities WHERE entity='Camera'").fetchall()
    c.close()
    ncam = {}
    for cc in cams:
        pv = json.loads(cc["data"]).get("provedor_id")
        if pv:
            ncam[pv] = ncam.get(pv, 0) + 1
    hoje = datetime.now().strftime("%Y-%m-%d"); out = []
    for r in provs:
        d = json.loads(r["data"])
        if not d.get("tester"):
            continue
        venc = str(d.get("trial_ate") or "") < hoje
        st = d.get("status", "ativo")
        st_disp = "bloqueado" if st == "bloqueado" else ("trial vencido" if venc else "trial ativo")
        out.append({"id": r["id"], "nome": d.get("nome"), "email": d.get("email"),
                    "trial_ate": d.get("trial_ate"), "status": st, "status_disp": st_disp,
                    "n_cameras": ncam.get(r["id"], 0), "limite": d.get("limite_cameras", 3)})
    out.sort(key=lambda x: x.get("trial_ate") or "", reverse=True)
    return out


@app.post("/api/tester/{pid}/reativar")
async def tester_reativar(pid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    o = _get_entity("Provedor", pid)
    if not o:
        return JSONResponse({"error": "nao encontrado"}, status_code=404)
    o["tester"] = False; o["status"] = "ativo"; o["reativado_em"] = _now_iso()
    c = db()
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Provedor' AND id=?",
              (json.dumps(o), _now_iso(), pid))
    c.execute("UPDATE users SET status='ativo' WHERE provedor_id=?", (pid,))
    c.commit(); c.close()
    return {"success": True}


@app.delete("/api/tester/{pid}")
async def tester_excluir(pid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    o = _get_entity("Provedor", pid)
    if not o or not o.get("tester"):
        return JSONResponse({"error": "provedor tester nao encontrado"}, status_code=404)
    c = db()
    for cc in c.execute("SELECT id FROM entities WHERE entity='Camera' "
                        "AND json_extract(data,'$.provedor_id')=?", (pid,)).fetchall():
        c.execute("DELETE FROM entities WHERE entity='Camera' AND id=?", (cc["id"],))
    for uu in c.execute("SELECT id FROM users WHERE provedor_id=?", (pid,)).fetchall():
        c.execute("DELETE FROM sessions WHERE user_id=?", (uu["id"],))
    c.execute("DELETE FROM users WHERE provedor_id=?", (pid,))
    c.execute("DELETE FROM entities WHERE entity='Provedor' AND id=?", (pid,))
    c.commit(); c.close()
    return {"success": True}


# ==================== USUARIO DEMONSTRADOR (admin) ====================
@app.get("/api/prov/demo/listar")
async def prov_demo_listar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    pid = u["provedor_id"]
    c = db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='AcessoDemo'").fetchall()
    cams = {}
    for r in c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall():
        try:
            dd = json.loads(r["data"]); cams[r["id"]] = dd.get("nome") or dd.get("name") or ""
        except Exception:
            cams[r["id"]] = ""
    c.close()
    hoje = datetime.now().strftime("%Y-%m-%d")
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") != pid:
            continue
        st = d.get("status") or "ativo"
        if st == "ativo" and str(d.get("expira") or "") and str(d.get("expira")) < hoje:
            st = "expirado"
        ids = d.get("cameras") or []
        out.append({"id": r["id"], "user_nome": d.get("user_nome", ""), "user_email": d.get("user_email", ""),
                    "cameras": ids, "camera_nomes": [cams.get(x, "?") for x in ids],
                    "expira": d.get("expira"), "status": st, "criado": d.get("criado")})
    out.sort(key=lambda x: x.get("criado") or "", reverse=True)
    return {"acessos": out}


@app.post("/api/prov/demo/criar")
async def prov_demo_criar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    pid = u["provedor_id"]
    b = await req.json()
    cams = [str(x) for x in (b.get("cameras") or [])][:4]
    if not cams:
        return JSONResponse({"error": "escolha de 1 a 4 cameras"}, status_code=400)
    try:
        dias = int(b.get("dias") or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias <= 0:
        return JSONResponse({"error": "defina a duracao (dias)"}, status_code=400)
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    nome = (b.get("full_name") or b.get("nome") or email).strip()
    if not email or len(pw) < 4:
        return JSONResponse({"error": "informe email e senha (min 4)"}, status_code=400)
    c = db()
    meus = set()
    for r in c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall():
        try:
            if json.loads(r["data"]).get("provedor_id") == pid:
                meus.add(r["id"])
        except Exception:
            pass
    cams = [x for x in cams if x in meus]
    if not cams:
        c.close(); return JSONResponse({"error": "nenhuma camera valida do seu provedor"}, status_code=400)
    uid = secrets.token_hex(8)
    try:
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, email, _hash_pw(pw), nome, "cliente", pid, "", "ativo", _now_iso()))
    except sqlite3.IntegrityError:
        c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    expira = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")
    eid = secrets.token_hex(12); now = _now_iso()
    data = {"user_id": uid, "cameras": cams, "expira": expira, "status": "ativo", "criado": now,
            "provedor_id": pid, "user_nome": nome, "user_email": email,
            "criado_por": (u.get("full_name") or u.get("email") or "Provedor")}
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("AcessoDemo", eid, json.dumps(data), now, now))
    c.commit(); c.close()
    return {"success": True, "id": eid, "expira": expira, "cameras": len(cams)}


@app.post("/api/prov/demo/{eid}/revogar")
async def prov_demo_revogar(eid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    pid = u["provedor_id"]
    c = db()
    r = c.execute("SELECT data FROM entities WHERE entity='AcessoDemo' AND id=?", (eid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    d = json.loads(r["data"])
    if d.get("provedor_id") != pid:
        c.close(); return _forbidden()
    d["status"] = "revogado"; d["revogado_em"] = _now_iso()
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='AcessoDemo' AND id=?", (json.dumps(d), _now_iso(), eid))
    c.execute("DELETE FROM sessions WHERE user_id=?", (d.get("user_id"),))
    c.commit(); c.close()
    return {"success": True}


def _prov_owner(uid):
    """True se o user e o DONO do painel do provedor (equipe=0/NULL)."""
    c = db()
    r = c.execute("SELECT equipe FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return bool(r) and not (r["equipe"] or 0)


@app.get("/api/prov/equipe/listar")
async def prov_equipe_listar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    pid = u["provedor_id"]
    c = db()
    rows = c.execute("SELECT id, email, full_name, status, menu_perms, equipe FROM users "
                     "WHERE role='provedor' AND provedor_id=?", (pid,)).fetchall()
    c.close()
    out = []
    for r in rows:
        eq = r["equipe"] or 0
        try:
            perms = json.loads(r["menu_perms"]) if r["menu_perms"] else []
        except Exception:
            perms = []
        out.append({"id": r["id"], "email": r["email"], "full_name": r["full_name"],
                    "status": r["status"], "menu_perms": perms, "owner": (not eq)})
    out.sort(key=lambda x: (0 if x["owner"] else 1, (x["full_name"] or x["email"] or "").lower()))
    return {"usuarios": out}


@app.post("/api/prov/equipe/criar")
async def prov_equipe_criar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    if not _prov_owner(u["id"]):
        return JSONResponse({"error": "apenas o dono da conta gerencia usuarios"}, status_code=403)
    pid = u["provedor_id"]
    b = await req.json()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    nome = (b.get("full_name") or b.get("nome") or "").strip()
    perms = b.get("menu_perms")
    if not isinstance(perms, list):
        perms = [""]
    perms = [str(x) for x in perms if x != "gestao-usuarios"]
    if not nome or not email or len(pw) < 4:
        return JSONResponse({"error": "informe nome, email e senha (min 4)"}, status_code=400)
    c = db(); uid = secrets.token_hex(8)
    try:
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created,menu_perms,equipe) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, email, _hash_pw(pw), nome, "provedor", pid, "", "ativo", _now_iso(), json.dumps(perms), 1))
    except sqlite3.IntegrityError:
        c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    c.commit(); c.close()
    return {"success": True, "id": uid}


@app.post("/api/prov/equipe/{uid}/perms")
async def prov_equipe_perms(uid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    if not _prov_owner(u["id"]):
        return JSONResponse({"error": "apenas o dono da conta gerencia usuarios"}, status_code=403)
    b = await req.json()
    perms = b.get("menu_perms")
    if not isinstance(perms, list):
        return JSONResponse({"error": "menu_perms invalido"}, status_code=400)
    perms = [str(x) for x in perms if x != "gestao-usuarios"]
    c = db()
    r = c.execute("SELECT provedor_id, equipe FROM users WHERE id=?", (uid,)).fetchone()
    if not r or r["provedor_id"] != u["provedor_id"] or not (r["equipe"] or 0):
        c.close(); return _forbidden()
    c.execute("UPDATE users SET menu_perms=? WHERE id=?", (json.dumps(perms), uid))
    c.commit(); c.close()
    return {"success": True}


@app.post("/api/prov/equipe/{uid}/status")
async def prov_equipe_status(uid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    if not _prov_owner(u["id"]):
        return JSONResponse({"error": "apenas o dono da conta gerencia usuarios"}, status_code=403)
    b = await req.json()
    novo = "bloqueado" if b.get("bloquear") else "ativo"
    c = db()
    r = c.execute("SELECT provedor_id, equipe FROM users WHERE id=?", (uid,)).fetchone()
    if not r or r["provedor_id"] != u["provedor_id"] or not (r["equipe"] or 0):
        c.close(); return _forbidden()
    c.execute("UPDATE users SET status=? WHERE id=?", (novo, uid))
    if novo == "bloqueado":
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.commit(); c.close()
    return {"success": True, "status": novo}


@app.delete("/api/prov/equipe/{uid}")
async def prov_equipe_del(uid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "provedor" and u.get("provedor_id")):
        return _forbidden()
    if not _prov_owner(u["id"]):
        return JSONResponse({"error": "apenas o dono da conta gerencia usuarios"}, status_code=403)
    c = db()
    r = c.execute("SELECT provedor_id, equipe FROM users WHERE id=?", (uid,)).fetchone()
    if not r or r["provedor_id"] != u["provedor_id"] or not (r["equipe"] or 0):
        c.close(); return _forbidden()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.commit(); c.close()
    return {"success": True}


@app.post("/api/demo/criar")
async def demo_criar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    b = await req.json()
    cams = [str(x) for x in (b.get("cameras") or [])][:4]
    if not cams:
        return JSONResponse({"error": "escolha de 1 a 4 cameras"}, status_code=400)
    try:
        dias = int(b.get("dias") or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias <= 0:
        return JSONResponse({"error": "defina a duracao (dias)"}, status_code=400)
    uid = (b.get("user_id") or "").strip()
    c = db()
    if not uid:
        email = (b.get("email") or "").strip().lower()
        pw = b.get("password") or ""
        nome = (b.get("full_name") or email).strip()
        if not email or len(pw) < 4:
            c.close(); return JSONResponse({"error": "email e senha (min 4) p/ criar login novo"}, status_code=400)
        uid = secrets.token_hex(8)
        try:
            c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (uid, email, _hash_pw(pw), nome, "cliente", "", "", "ativo", _now_iso()))
        except sqlite3.IntegrityError:
            c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    else:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            c.close(); return JSONResponse({"error": "usuario nao encontrado"}, status_code=404)
    for _r in c.execute("SELECT id FROM entities WHERE entity='AcessoDemo' "
                        "AND json_extract(data,'$.user_id')=?", (uid,)).fetchall():
        c.execute("DELETE FROM entities WHERE entity='AcessoDemo' AND id=?", (_r["id"],))
    expira = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d")
    eid = secrets.token_hex(12); now = _now_iso()
    data = {"user_id": uid, "cameras": cams, "expira": expira, "status": "ativo", "criado": now}
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("AcessoDemo", eid, json.dumps(data), now, now))
    c.commit(); c.close()
    return {"success": True, "id": eid, "user_id": uid, "expira": expira, "cameras": len(cams)}


@app.get("/api/demo/listar")
async def demo_listar(req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    c = db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='AcessoDemo'").fetchall()
    usr = {x["id"]: x for x in c.execute("SELECT id, email, full_name FROM users").fetchall()}
    c.close()
    hoje = datetime.now().strftime("%Y-%m-%d"); out = []
    for r in rows:
        d = json.loads(r["data"]); uu = usr.get(d.get("user_id"))
        st = d.get("status")
        if st == "ativo" and str(d.get("expira") or "") < hoje:
            st = "expirado"
        out.append({"id": r["id"], "user_id": d.get("user_id"),
                    "email": (uu["email"] if uu else ""), "nome": (uu["full_name"] if uu else ""),
                    "n_cameras": len(d.get("cameras") or []), "cameras": d.get("cameras") or [],
                    "expira": d.get("expira"), "status": st})
    out.sort(key=lambda x: x.get("expira") or "", reverse=True)
    return out


@app.post("/api/demo/{eid}/revogar")
async def demo_revogar(eid: str, req: Request):
    u = current_user(req)
    if not (u and u["role"] == "admin"):
        return _forbidden()
    c = db()
    r = c.execute("SELECT data FROM entities WHERE entity='AcessoDemo' AND id=?", (eid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    d = json.loads(r["data"]); d["status"] = "revogado"; d["revogado_em"] = _now_iso()
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='AcessoDemo' AND id=?",
              (json.dumps(d), _now_iso(), eid))
    c.execute("DELETE FROM sessions WHERE user_id=?", (d.get("user_id"),))
    c.commit(); c.close()
    return {"success": True}


# ==================== GESTAO DE LOGINS (/api/users) ====================
def _cliente_do_provedor(cliente_id, provedor_id):
    """cliente_id pertence a um Cliente cujo provedor_id == provedor_id?"""
    c = db()
    r = c.execute("SELECT data FROM entities WHERE entity='Cliente' AND id=?", (cliente_id,)).fetchone()
    c.close()
    if not r:
        return False
    return json.loads(r["data"]).get("provedor_id") == provedor_id

@app.get("/api/users")
async def users_list(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db()
    if u["role"] == "admin":
        rows = c.execute("SELECT * FROM users ORDER BY created DESC").fetchall()
    elif u["role"] == "provedor":
        rows = c.execute("SELECT * FROM users WHERE provedor_id=? AND role='cliente' ORDER BY created DESC",
                         (u["provedor_id"],)).fetchall()
    else:
        rows = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchall()
    c.close()
    return [dict(_user_public(r), created=r["created"]) for r in rows]

@app.post("/api/users")
async def users_create(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    b = await req.json()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    role = b.get("role") or "cliente"
    if not email or len(pw) < 4:
        return JSONResponse({"error": "email e senha (min 4) obrigatorios"}, status_code=400)
    provedor_id = b.get("provedor_id") or ""
    cliente_id = b.get("cliente_id") or ""
    if u["role"] == "provedor":
        # provedor SO cria login de cliente final, SEMPRE amarrado a ele
        if role != "cliente":
            return _forbidden("provedor so cria login de cliente")
        provedor_id = u["provedor_id"]
        if not cliente_id or not _cliente_do_provedor(cliente_id, provedor_id):
            return _forbidden("cliente_id invalido ou de outro provedor")
    elif u["role"] != "admin":
        return _forbidden()
    if u["role"] == "admin" and role in ("provedor", "cliente"):
        return _forbidden("Provedor nasce de proposta+pagamento e cliente e criado pelo provedor. "
                          "Aqui o admin so cria outro admin (use Usuario Demonstrador ou Provedor Tester).")
    if role not in ("admin", "provedor", "cliente"):
        return JSONResponse({"error": "role invalida"}, status_code=400)
    if role == "cliente" and not cliente_id:
        return JSONResponse({"error": "cliente_id obrigatorio p/ login de cliente"}, status_code=400)
    if role == "provedor" and not provedor_id:
        return JSONResponse({"error": "provedor_id obrigatorio p/ login de provedor"}, status_code=400)
    uid = secrets.token_hex(8)
    c = db()
    try:
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, email, _hash_pw(pw), b.get("full_name") or email, role,
                   provedor_id, cliente_id, "ativo", _now_iso()))
        c.commit()
    except sqlite3.IntegrityError:
        c.close()
        return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); c.close()
    return _user_public(r)

@app.put("/api/users/{uid}")
async def users_update(uid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    # permissao: admin tudo; provedor so os clientes dele; usuario a si mesmo (senha/nome)
    if u["role"] != "admin" and u["id"] != uid:
        if not (u["role"] == "provedor" and r["provedor_id"] == u["provedor_id"] and r["role"] == "cliente"):
            c.close(); return _forbidden()
    b = await req.json()
    sets, vals = [], []
    if b.get("password"):
        sets.append("password_hash=?"); vals.append(_hash_pw(b["password"]))
    if b.get("full_name"):
        sets.append("full_name=?"); vals.append(b["full_name"])
    if b.get("status") in ("ativo", "bloqueado") and u["id"] != uid:
        sets.append("status=?"); vals.append(b["status"])
    if sets:
        vals.append(uid)
        c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
        if b.get("status") == "bloqueado":
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.commit()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(); c.close()
    return _user_public(r)

@app.delete("/api/users/{uid}")
async def users_delete(uid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    if uid == u["id"]:
        return _forbidden("nao pode excluir a si mesmo")
    c = db()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    if u["role"] != "admin":
        if not (u["role"] == "provedor" and r["provedor_id"] == u["provedor_id"] and r["role"] == "cliente"):
            c.close(); return _forbidden()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.commit(); c.close()
    return {"success": True}


# ==================== ENTIDADES GENERICAS (ESCOPADAS POR PAPEL) ====================
def _row_to_obj(r):
    o = json.loads(r["data"]); o["id"] = r["id"]
    o["created_date"] = r["created_date"]; o["updated_date"] = r["updated_date"]
    return o

# Config da plataforma (precos/catalogos): todos os papeis LEEM, so a Corexia (admin) escreve.
ENT_CONFIG = {"Plano", "CatalogoIA", "CatalogoGravacao"}
# Segredos (Asaas/Z-API do provedor): NUNCA via CRUD generico (nem admin) — so os endpoints /api/comercial.
ENT_SECRETAS = {"ProvedorCred"}

def _scope_ok(name, obj, u):
    """Este usuario pode VER este registro?"""
    if name in ENT_SECRETAS:        # segredos invisiveis no CRUD generico
        return False
    if u["role"] == "admin":
        return True
    if u.get("user_type") == "demonstrator":     # demo: ve SO as cameras escolhidas na demonstracao
        return name == "Camera" and obj.get("id") in (u.get("demo_cameras") or [])
    if u.get("user_type") == "subuser":          # sub-usuario: espelho parcial do cliente master
        if u.get("sub_blocked"):
            return False
        if name == "Camera":
            return obj.get("id") in (u.get("allowed_cameras") or [])
        if name == "Mosaico":
            return obj.get("id") in (u.get("allowed_mosaicos") or [])
        if name == "Alerta":
            return obj.get("camera_id") in (u.get("allowed_cameras") or [])
        if name in ENT_CONFIG:
            return True
        return False
    if name in ENT_CONFIG:          # planos/catalogos = leitura publica (autenticada)
        return True
    if u["role"] == "provedor":
        pid = u["provedor_id"]
        if not pid:
            return False                        # dono vazio NUNCA casa (fecha o fail-open)
        if name == "Provedor":
            return obj.get("id") == pid
        return obj.get("provedor_id") == pid
    if u["role"] == "cliente":
        cid = u["cliente_id"]
        if not cid:
            return False
        if name == "Cliente":
            return obj.get("id") == cid
        if name == "Provedor":
            return False
        return obj.get("cliente_id") == cid
    return False

# o que cada papel pode CRIAR
PROVEDOR_NAO_CRIA = {"Camera", "Provedor"}          # camera e provedor = so a Corexia
CLIENTE_CRIA = {"PreferenciaAlerta"}                 # cliente final quase nao cria nada

@app.get("/api/entities/{name}")
async def ent_list(name: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # respeita sort/limit do front (ex.: ?sort=-created_date&limit=10) — antes ignorava e
    # devolvia a tabela inteira em cada polling do painel
    sort = req.query_params.get("sort", "-created_date")
    order = "ASC" if not sort.startswith("-") else "DESC"
    try:
        limit = max(0, int(req.query_params.get("limit", "0")))
    except ValueError:
        limit = 0
    c = db(); rows = c.execute(f"SELECT * FROM entities WHERE entity=? ORDER BY created_date {order}", (name,)).fetchall(); c.close()
    out = [o for r in rows for o in [_row_to_obj(r)] if _scope_ok(name, o, u)]
    if name == "Alerta":
        out = [_obj_ts_local(o) for o in out]
    return out[:limit] if limit else out

@app.post("/api/entities/{name}/filter")
async def ent_filter(name: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    q = (await req.json()).get("query", {}) or {}
    c = db(); rows = c.execute("SELECT * FROM entities WHERE entity=? ORDER BY created_date DESC", (name,)).fetchall(); c.close()
    res = [o for r in rows for o in [_row_to_obj(r)]
           if _scope_ok(name, o, u) and all(o.get(k) == v for k, v in q.items())]
    if name == "Alerta":
        res = [_obj_ts_local(o) for o in res]
    return res

_EMBED_LIVE_RE = re.compile(r"let live = '([^']+)'")

def _resolver_link_camera(url):
    """O cliente cola o link que tiver. Sanitiza (corta lixo de iframe colado junto) e,
    se for link de EMBED do analitico (pagina de player, que a IA nao le), resolve
    automaticamente pro stream .m3u8 real que esta dentro da pagina."""
    u = (url or "").strip().strip('"').strip("'")
    for sep in ('"', "'", "<", " "):   # ex.: ...autoplay=true" frameborder="0"></iframe>
        if sep in u:
            u = u.split(sep, 1)[0]
    if "/camera/embed/" in u:
        try:
            html = requests.get(u, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
            m = _EMBED_LIVE_RE.search(html)
            if m:
                print(f"[camera] embed resolvido -> {m.group(1)[:90]}")
                return m.group(1)
        except Exception as e:
            print("[camera] erro ao resolver embed:", str(e)[:80])
    return u

def _normaliza_camera(data):
    """Aplica o resolvedor nos campos de stream da Camera (create e update)."""
    if data.get("rtsp_url"):
        data["rtsp_url"] = _resolver_link_camera(data["rtsp_url"])
    # colou o embed do analitico no campo de embed e deixou o stream vazio? resolve sozinho
    if not data.get("rtsp_url") and "/camera/embed/" in (data.get("embed_url") or ""):
        data["rtsp_url"] = _resolver_link_camera(data["embed_url"])
    return data


@app.post("/api/entities/{name}")
async def ent_create(name: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # chamado tem regras proprias (autor, resposta, push) — nao-admin nao burla pelo CRUD generico
    if name in ("Chamado", "SubUser") and u["role"] != "admin":
        return _forbidden("chamados so pelo fluxo /api/chamados")
    if name in ENT_CONFIG and u["role"] != "admin":
        return _forbidden("config da plataforma so a Corexia gerencia")
    data = await req.json(); data.pop("id", None)
    if u["role"] == "provedor":
        if name in PROVEDOR_NAO_CRIA:
            return _forbidden(f"provedor nao pode criar {name}")
        cid = data.get("cliente_id")
        if cid and name != "Comissao" and not _cliente_do_provedor(cid, u["provedor_id"]):
            return _forbidden("cliente_id de outro provedor")
        data["provedor_id"] = u["provedor_id"]          # carimba SEMPRE o dono
        prov = _get_entity("Provedor", u["provedor_id"])
        data["provedor_nome"] = (prov or {}).get("nome", data.get("provedor_nome", ""))
    elif u["role"] == "cliente":
        if name not in CLIENTE_CRIA:
            return _forbidden(f"cliente nao pode criar {name}")
        data["cliente_id"] = u["cliente_id"]
    if name == "Camera":
        data = _normaliza_camera(data)
    eid = secrets.token_hex(12); now = _now_iso()
    c = db(); c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                        (name, eid, json.dumps(data), now, now)); c.commit(); c.close()
    o = dict(data); o.update(id=eid, created_date=now, updated_date=now); return o

@app.get("/api/entities/{name}/{eid}")
async def ent_get(name: str, eid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db(); r = c.execute("SELECT * FROM entities WHERE entity=? AND id=?", (name, eid)).fetchone(); c.close()
    if not r:
        return JSONResponse({"error": "nao encontrado"}, status_code=404)
    o = _row_to_obj(r)
    return o if _scope_ok(name, o, u) else _forbidden()

@app.put("/api/entities/{name}/{eid}")
async def ent_update(name: str, eid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # chamado tem regras proprias — nao-admin nao burla pelo CRUD generico
    if name in ("Chamado", "SubUser") and u["role"] != "admin":
        return _forbidden("chamados so pelo fluxo /api/chamados")
    if name in ENT_CONFIG and u["role"] != "admin":
        return _forbidden("config da plataforma so a Corexia gerencia")
    data = await req.json(); data.pop("id", None)
    c = db(); r = c.execute("SELECT * FROM entities WHERE entity=? AND id=?", (name, eid)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    cur = _row_to_obj(r)
    if not _scope_ok(name, cur, u):
        c.close(); return _forbidden()
    if u["role"] != "admin" and name == "Provedor":
        c.close(); return _forbidden("so a Corexia gerencia provedores")
    # nao-admin NUNCA muda o dono (evita roubar registro de outro tenant)
    if u["role"] != "admin":
        data.pop("provedor_id", None); data.pop("provedor_nome", None)
    if u["role"] == "provedor":
        # provedor so pode apontar cliente_id pra cliente DELE (em QUALQUER entidade)
        cid = data.get("cliente_id")
        if cid and name != "Comissao" and not _cliente_do_provedor(cid, u["provedor_id"]):
            c.close(); return _forbidden("cliente de outro provedor")
    if u["role"] == "cliente":
        # cliente final so mexe em: status do proprio alerta e preferencias dele
        if name == "Alerta":
            data = {k: v for k, v in data.items() if k == "status"}
        elif name == "PreferenciaAlerta":
            data.pop("cliente_id", None)
        else:
            c.close(); return _forbidden(f"cliente nao pode editar {name}")
    if u["role"] == "provedor" and name == "Camera":
        # provedor SO atribui/desatribui camera a cliente e contrata add-ons (nao edita o resto)
        data = {k: v for k, v in data.items()
                if k in ("cliente_id", "cliente_nome", "cliente_telefone", "ia_contrato", "grav_contrato")}
    if name == "Camera":
        data = _normaliza_camera(data)   # sanitiza/resolve link colado (embed -> m3u8)
        # Contratos de add-on: NUNCA confia nas datas/preco vindos do cliente — o servidor
        # carimba valor_dia (tabela), inicio/fim (relogio do servidor) e valida dias.
        cur_cam = json.loads(r["data"])
        for campo in ("ia_contrato", "grav_contrato"):
            if campo in data:
                san = _sanitiza_contrato(data[campo], campo, cur_cam.get(campo))
                if san is None:
                    data.pop(campo, None)
                else:
                    data[campo] = san
    cur_data = json.loads(r["data"]); cur_data.update(data); now = _now_iso()
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity=? AND id=?",
              (json.dumps(cur_data), now, name, eid))
    c.commit(); c.close()
    # cascata: status do Cliente -> bloqueia/libera os LOGINS dele (gate real de acesso e users.status)
    if name == "Cliente" and "status" in data:
        bloq = cur_data.get("status") in ("suspenso", "inativo")
        cc = db()
        cc.execute("UPDATE users SET status=? WHERE cliente_id=? AND role='cliente'",
                   ("bloqueado" if bloq else "ativo", eid))
        if bloq:
            cc.execute("DELETE FROM sessions WHERE user_id IN "
                       "(SELECT id FROM users WHERE cliente_id=? AND role='cliente')", (eid,))
        cc.commit(); cc.close()
    o = dict(cur_data); o.update(id=eid, updated_date=now); return o

def _cascade_delete(c, name, eid):
    """Excluir Cliente/Provedor remove os DEPENDENTES, senao ficam orfaos ATIVOS
    (usuarios que ainda logam, cameras ainda gravadas/analisadas, alertas, preferencias)."""
    if name == "Cliente":
        for (uid,) in c.execute("SELECT id FROM users WHERE cliente_id=?", (eid,)).fetchall():
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE cliente_id=?", (eid,))
        for ent in ("Camera", "Alerta", "PreferenciaAlerta"):
            c.execute("DELETE FROM entities WHERE entity=? AND json_extract(data,'$.cliente_id')=?", (ent, eid))
    elif name == "Provedor":
        for (cid,) in c.execute("SELECT id FROM entities WHERE entity='Cliente' AND json_extract(data,'$.provedor_id')=?", (eid,)).fetchall():
            _cascade_delete(c, "Cliente", cid)
            c.execute("DELETE FROM entities WHERE entity='Cliente' AND id=?", (cid,))
        for (uid,) in c.execute("SELECT id FROM users WHERE provedor_id=?", (eid,)).fetchall():
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE provedor_id=?", (eid,))


@app.delete("/api/entities/{name}/{eid}")
async def ent_delete(name: str, eid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # chamado tem regras proprias — nao-admin nao burla pelo CRUD generico
    if name in ("Chamado", "SubUser") and u["role"] != "admin":
        return _forbidden("chamados so pelo fluxo /api/chamados")
    c = db(); r = c.execute("SELECT * FROM entities WHERE entity=? AND id=?", (name, eid)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    o = _row_to_obj(r)
    if not _scope_ok(name, o, u):
        c.close(); return _forbidden()
    if name in ENT_CONFIG and u["role"] != "admin":
        c.close(); return _forbidden("config da plataforma so a Corexia gerencia")
    if u["role"] == "cliente":
        c.close(); return _forbidden("cliente nao exclui registros")
    if u["role"] == "provedor" and name in ("Camera", "Provedor"):
        c.close(); return _forbidden("so a Corexia gerencia cameras")
    _cascade_delete(c, name, eid)   # remove dependentes (Cliente/Provedor) antes
    c.execute("DELETE FROM entities WHERE entity=? AND id=?", (name, eid))
    c.commit(); c.close()
    return {"success": True}

def _get_entity(name, eid):
    if not eid:
        return None
    c = db(); r = c.execute("SELECT * FROM entities WHERE entity=? AND id=?", (name, eid)).fetchone(); c.close()
    return _row_to_obj(r) if r else None


# ==================== WEB PUSH (PC + app do cliente) ====================
def _push_users_do_alerta(cliente_id, provedor_id):
    """Quem recebe o push: donos do cliente da camera, do provedor, e admins."""
    c = db(); ids = set()
    for r in c.execute("SELECT id, role, cliente_id, provedor_id FROM users WHERE status='ativo'"):
        if (r["role"] == "admin"
                or (cliente_id and r["cliente_id"] == cliente_id)
                or (provedor_id and r["provedor_id"] == provedor_id)):
            ids.add(r["id"])
    c.close(); return list(ids)

def _send_push(user_ids, title, body, url="/", tag="corexia-alerta"):
    if not (VAPID_PUBLIC and os.path.exists(VAPID_PRIV)) or not user_ids:
        return 0
    try:
        from pywebpush import webpush
    except Exception:
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    c = db()
    qs = ",".join("?" * len(user_ids))
    subs = c.execute(f"SELECT endpoint, sub FROM push_subs WHERE user_id IN ({qs})", tuple(user_ids)).fetchall()
    c.close()
    enviados, mortos = 0, []
    for row in subs:
        try:
            webpush(subscription_info=json.loads(row["sub"]), data=payload,
                    vapid_private_key=VAPID_PRIV, vapid_claims={"sub": VAPID_SUB}, timeout=10)
            enviados += 1
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (404, 410):
                mortos.append(row["endpoint"])
    if mortos:
        c = db(); c.executemany("DELETE FROM push_subs WHERE endpoint=?", [(e,) for e in mortos]); c.commit(); c.close()
    return enviados

@app.get("/api/push/config")
async def push_config():
    return {"vapid_public": VAPID_PUBLIC, "enabled": bool(VAPID_PUBLIC and os.path.exists(VAPID_PRIV))}

@app.post("/api/push/subscribe")
async def push_subscribe(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    sub = await req.json()
    ep = (sub or {}).get("endpoint")
    if not ep:
        return JSONResponse({"error": "sem endpoint"}, status_code=400)
    c = db(); c.execute("INSERT OR REPLACE INTO push_subs (endpoint,user_id,sub,created) VALUES (?,?,?,?)",
                        (ep, u["id"], json.dumps(sub), _now_iso())); c.commit(); c.close()
    return {"success": True}

@app.post("/api/push/unsubscribe")
async def push_unsubscribe(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    ep = (await req.json()).get("endpoint", "")
    c = db(); c.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,)); c.commit(); c.close()
    return {"success": True}


# ==================== WHATSAPP ====================
def _numero(numero):
    n = "".join(ch for ch in numero if ch.isdigit())
    return n if n.startswith("55") else "55" + n

def _evolution(numero, caption, img_b64=None):
    if not EVOLUTION_APIKEY:
        print("[evolution] EVOLUTION_APIKEY nao configurada — pulei WhatsApp"); return False
    base = EVOLUTION_URL.rstrip("/")
    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_APIKEY}
    try:
        if img_b64:
            url = f"{base}/message/sendMedia/{EVOLUTION_INSTANCE}"
            body = {"number": numero, "mediatype": "image", "media": img_b64,
                    "caption": caption, "fileName": "alerta.jpg"}
        else:
            url = f"{base}/message/sendText/{EVOLUTION_INSTANCE}"
            body = {"number": numero, "text": caption}
        r = requests.post(url, json=body, headers=headers, timeout=25)
        print("[evolution]", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("[evolution] erro:", e); return False

def _zapi(numero, caption, img_b64=None, inst=None, tok=None, cli=None):
    inst = inst or ZAPI_INSTANCE; tok = tok or ZAPI_TOKEN; cli = cli if cli is not None else ZAPI_CLIENT
    if not (inst and tok):
        print("[zapi] credenciais nao configuradas — pulei WhatsApp"); return False
    headers = {"Content-Type": "application/json", "Client-Token": cli or ""}
    try:
        if img_b64:
            url = f"https://api.z-api.io/instances/{inst}/token/{tok}/send-image"
            body = {"phone": numero, "image": f"data:image/jpeg;base64,{img_b64}", "caption": caption}
        else:
            url = f"https://api.z-api.io/instances/{inst}/token/{tok}/send-text"
            body = {"phone": numero, "message": caption}
        r = requests.post(url, json=body, headers=headers, timeout=25)
        print("[zapi]", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("[zapi] erro:", e); return False

def envia_whatsapp(numero, caption, img_b64=None, provedor_id=None):
    numero = _numero(numero)
    if WHATSAPP_PROVIDER == "zapi":
        _inst = _tok = _cli = None
        if provedor_id:
            try:
                import comercial as _com
                _inst, _tok, _cli = _com._zapi_do_provedor(provedor_id)
            except Exception as _e:
                print("[zapi] _zapi_do_provedor falhou:", _e)
        return _zapi(numero, caption, img_b64, _inst, _tok, _cli)
    return _evolution(numero, caption, img_b64)


def _pref_notifica(cliente_id, tipo):
    """Respeita PreferenciaAlerta do cliente (tipos/horario/dias/notificar_whatsapp).
    Sem preferencia (ou desativada) = notifica (comportamento padrao)."""
    if not cliente_id:
        return True
    try:
        c = db()
        row = c.execute("SELECT data FROM entities WHERE entity='PreferenciaAlerta' "
                        "AND json_extract(data,'$.cliente_id')=? LIMIT 1", (cliente_id,)).fetchone()
        c.close()
    except Exception:
        return True
    if not row:
        return True
    d = json.loads(row["data"])
    if not d.get("ativo", True):
        return True
    if not d.get("notificar_whatsapp", True):
        return False
    tipos = d.get("tipos_permitidos") or []
    if tipos and tipo not in tipos:
        return False
    now = datetime.now()
    hi = (d.get("hora_inicio") or d.get("horario_inicio") or "").strip(); hf = (d.get("hora_fim") or d.get("horario_fim") or "").strip()
    if hi and hf:
        hm = now.strftime("%H:%M")
        dentro = (hi <= hm <= hf) if hi <= hf else (hm >= hi or hm <= hf)
        if not dentro:
            return False
    dias = d.get("dias_semana") or []
    if dias:
        wd = (now.weekday() + 1) % 7   # 0=Dom .. 6=Sab
        if wd not in dias:
            return False
    return True


def _plantao_numeros(provedor_id):
    """Numeros de plantao ATIVOS do provedor (recebem todos os alertas do provedor)."""
    if not provedor_id:
        return []
    try:
        c = db()
        rows = c.execute("SELECT data FROM entities WHERE entity='NumeroPlantao' "
                         "AND json_extract(data,'$.provedor_id')=?", (provedor_id,)).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("ativo", True) and d.get("telefone"):
            out.append(d["telefone"])
    return out


# ==================== WEBHOOK DO DETECTOR ====================
@app.post("/webhookAlertas")
async def webhook(req: Request):
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    tipo = b.get("tipo", "outro")
    img_b64 = b.get("imagem_base64")
    verificado = bool(b.get("verificado", True))   # item 1: False = quarentena (grava, sem WhatsApp/push)
    criado = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # enriquece com os dados REAIS da camera (dono provedor + cliente atribuido)
    # dono SEMPRE da camera cadastrada — nunca do corpo (impede forjar alerta a numero/cliente arbitrario)
    cam = _get_entity("Camera", b.get("camera_id", "")) or {}
    cliente_id = cam.get("cliente_id", "")
    cliente_nome = cam.get("cliente_nome", "")
    tel = (cam.get("cliente_telefone") or "").strip()
    if not tel and cliente_id:
        _cli_ent = _get_entity("Cliente", cliente_id) or {}
        tel = (_cli_ent.get("telefone") or _cli_ent.get("celular") or _cli_ent.get("whatsapp") or "").strip()
    provedor_id = cam.get("provedor_id", "")
    provedor_nome = cam.get("provedor_nome", "")

    c = db()
    cur = c.execute(
        "INSERT INTO alertas (camera_nome,camera_id,cliente_nome,cliente_telefone,tipo,descricao,confianca,imagem,whatsapp,criado) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (b.get("camera_nome", ""), b.get("camera_id", ""), cliente_nome,
         tel, tipo, b.get("descricao", ""),
         int(b.get("confianca", 0) or 0), "", 0, criado))
    aid = cur.lastrowid

    img_name = ""
    if img_b64:
        try:
            img_name = f"{aid}-{secrets.token_hex(8)}.jpg"   # nome nao-adivinavel
            with open(os.path.join(IMG_DIR, img_name), "wb") as f:
                f.write(base64.b64decode(img_b64))
            c.execute("UPDATE alertas SET imagem=? WHERE id=?", (img_name, aid))
        except Exception as e:
            print("[img] erro:", e); img_name = ""
    c.commit(); c.close()

    enviado = False
    # so notifica alerta VERIFICADO pela IA (quarentena grava mas nao dispara)
    # camera em migracao (ainda no analitico) grava o alerta mas NAO notifica ainda
    if verificado and cam.get("migracao_status") != "pendente_analitico":
        emoji = TIPO_EMOJI.get(tipo, "🔔"); label = TIPO_LABEL.get(tipo, "ALERTA DE SEGURANCA")
        agora = datetime.now().strftime("%d/%m/%Y %H:%M")
        _camlink = (cam.get('embed_url', '') or '')
        if _camlink and not _camlink.startswith('http'):
            _camlink = 'https://www.grupocorexia.com.br' + (_camlink if _camlink.startswith('/') else '/' + _camlink)
        caption = (f"{emoji} *COREXIA SEGURANCA - {label}*\n\n"
                   f"👤 *Cliente:* {cliente_nome or 'N/A'}\n"
                   f"📷 *Camera:* {b.get('camera_nome', '')}\n"
                   f"🎯 *Confianca da IA:* {int(b.get('confianca', 0) or 0)}%\n"
                   f"🕐 *Horario:* {agora}\n"
                   + (f"📝 *Descricao:* {b.get('descricao', '')}\n" if b.get('descricao') else "")
                   + (f"\n📹 *Ver a camera ao vivo:*\n{_camlink}\n" if _camlink else "")
                   + "\n_Sistema Corexia de vigilancia._")
        # 1) CLIENTE final: so se a camera tem cliente c/ telefone E a PreferenciaAlerta permite
        if tel and cliente_id and _pref_notifica(cliente_id, tipo):
            enviado = envia_whatsapp(tel, caption, img_b64, provedor_id)
            if enviado:
                c = db(); c.execute("UPDATE alertas SET whatsapp=1 WHERE id=?", (aid,)); c.commit(); c.close()
        # 2) PLANTAO do provedor: recebe TODOS os alertas (sem filtro de preferencia)
        try:
            for _num in _plantao_numeros(provedor_id):
                envia_whatsapp(_num, "*[PLANTAO]* " + caption, img_b64, provedor_id)
        except Exception as _e:
            print("[plantao] erro:", _e)
        # 3) SUB-USUARIOS do cliente: so os que optaram (receber_alertas_whatsapp) e com a camera liberada
        try:
            _camid = b.get("camera_id", "")
            for _su in _subusers_do_cliente(cliente_id):
                _stel = (_su.get("telefone") or "").strip()
                if (_su.get("status") == "ativo" and _su.get("receber_alertas_whatsapp")
                        and _stel and _camid in (_su.get("allowed_cameras") or [])):
                    envia_whatsapp(_stel, caption, img_b64, provedor_id)
        except Exception as _e:
            print("[subuser-alerta] erro:", _e)

    # cria a ENTIDADE Alerta (pro painel/portal), ja com o dono certo
    try:
        ent = {
            "camera_id": b.get("camera_id", ""), "camera_nome": b.get("camera_nome", ""),
            "cliente_id": cliente_id, "cliente_nome": cliente_nome,
            "cliente_telefone": tel, "provedor_id": provedor_id, "provedor_nome": provedor_nome,
            "tipo": tipo, "descricao": b.get("descricao", ""),
            "confianca": int(b.get("confianca", 0) or 0),
            "imagem_url": f"/img/{img_name}" if img_name else "",
            "status": "novo" if verificado else "nao_verificado",
            "verificado": verificado,
            "whatsapp_enviado": enviado, "fonte": "detector_ia",
        }
        eid = secrets.token_hex(12); now = _now_iso()
        cc = db(); cc.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                              ("Alerta", eid, json.dumps(ent), now, now)); cc.commit(); cc.close()
    except Exception as e:
        print("[alerta-entity] erro:", e)

    # NOTIFICACAO PUSH (PC + app) — so p/ alerta VERIFICADO (quarentena nao empurra push)
    try:
        if verificado:
            emoji = TIPO_EMOJI.get(tipo, "🔔"); lbl = TIPO_LABEL.get(tipo, "ALERTA DE SEGURANCA")
            alvo = "/alertas"   # admin/provedor caem na central; cliente e redirecionado pro portal no SW
            n_push = _send_push(_push_users_do_alerta(cliente_id, provedor_id),
                                f"{emoji} {lbl}",
                                f"{b.get('camera_nome','')} · {int(b.get('confianca', 0) or 0)}% de confianca",
                                url=alvo, tag=f"cam-{b.get('camera_id','')}")
            if n_push:
                print(f"[push] enviado a {n_push} dispositivo(s)")
    except Exception as e:
        print("[push] erro:", e)

    print(f"[ALERTA] #{aid} {tipo} | {b.get('camera_nome','')} | whatsapp={enviado}")
    return {"success": True, "alerta_id": aid, "whatsapp_enviado": enviado}


# ==================== CAMERAS PRO DETECTOR / GRAVADOR (secret) ====================
def _valida_stream(url):
    try:
        h = {"User-Agent": "Mozilla/5.0"}
        # streams com protecao de hotlink (ex.: analitico) so respondem com Referer
        if STREAM_REFERER:
            h["Referer"] = STREAM_REFERER
        r = requests.get(url, timeout=6, stream=True, headers=h)
        st = r.status_code; r.close()
        return st == 200, st
    except Exception:
        return False, 0

def _todas_cameras():
    c = db(); rows = c.execute("SELECT * FROM entities WHERE entity=?", ("Camera",)).fetchall(); c.close()
    out = []
    for r in rows:
        o = json.loads(r["data"]); o["id"] = r["id"]
        out.append(o)
    return out

def _yt_stream(cam_id):
    e = _yt_urls.get(cam_id)
    return e[0] if (e and time.time() - e[1] < 6 * 3600) else ""

def _live_hls_path(cam_id):
    """HLS local restreamado pelo gravador; o detector (mesma maquina) le direto o arquivo."""
    p = os.path.join(LIVE_DIR, cam_id, "index.m3u8")
    if os.path.exists(p) and (time.time() - os.path.getmtime(p) < 60):
        return p
    return ""

def _bg_resolve_youtube():
    """Resolve os embeds do YouTube -> HLS pra IA analisar. Re-resolve a cada 3h (expira)."""
    while True:
        try:
            for o in _todas_cameras():
                if (o.get("rtsp_url") or o.get("stream_url") or "").strip():
                    continue
                emb = o.get("embed_url", "") or ""
                m = YT_RE.search(emb)
                if not (m and ("youtube" in emb or "youtu.be" in emb)):
                    continue
                cur = _yt_urls.get(o["id"])
                if cur and time.time() - cur[1] < 3 * 3600:
                    continue
                try:
                    out = subprocess.run(
                        [YTDLP, "-g", "-f", "best[height<=720]/best",
                         f"https://www.youtube.com/watch?v={m.group(1)}"],
                        capture_output=True, text=True, timeout=90)
                    u = (out.stdout or "").strip().splitlines()
                    if u:
                        _yt_urls[o["id"]] = (u[0], time.time())
                        print(f"[yt] HLS resolvido p/ IA: {o.get('nome','')}", flush=True)
                    else:
                        print(f"[yt] sem HLS p/ {o.get('nome','')}: {(out.stderr or '')[:120]}")
                except Exception as e:
                    print("[yt] erro:", str(e)[:100])
        except Exception as e:
            print("[yt-bg] erro:", str(e)[:100])
        time.sleep(600)

# Gates de monetizacao (ia_contrato / grav_contrato na Camera).
# ESTRITO = so roda com contrato ATIVO e dentro da janela (inicio <= hoje <= fim).
# CONSERVADOR = tudo roda, exceto contrato cancelado/vencido (grandfather).
# Regra do dono (2026-07-24): GRAVACAO e estrita (so grava quem contratou, contando da
# data da contratacao); IA segue conservadora ate a virada comercial (mude no .env).
GATE_GRAV_ESTRITO = _env_bool("GATE_GRAV_ESTRITO", "1")
GATE_IA_ESTRITO   = _env_bool("GATE_IA_ESTRITO", "0")

def _contrato_ativo(cam, campo):
    ct = cam.get(campo)
    if not isinstance(ct, dict) or ct.get("status") != "ativo":
        return False
    hoje = datetime.now().strftime("%Y-%m-%d")
    ini = str(ct.get("inicio") or "")
    fim = str(ct.get("fim") or "")
    return (not ini or ini <= hoje) and (not fim or hoje <= fim)

def _contrato_bloqueia(cam, campo):
    ct = cam.get(campo)
    if not isinstance(ct, dict) or not ct:
        return False
    if ct.get("status") == "inativo":
        return True
    # vencido = bloqueia INDEPENDENTE do status (contrato malformado sem status tambem):
    fim = str(ct.get("fim") or "")
    if fim and fim < datetime.now().strftime("%Y-%m-%d"):
        return True
    return False

def _gate_permite(cam, campo, estrito):
    if estrito:
        return _contrato_ativo(cam, campo)
    return not _contrato_bloqueia(cam, campo)


# Precos por dia carimbados no SERVIDOR (nao confia no valor_dia do cliente) e dias validos.
PRECO_DIA = {"ia_contrato": float(os.getenv("PRECO_IA_DIA", "1.9")),
             "grav_contrato": float(os.getenv("PRECO_GRAV_DIA", "0.9"))}
DIAS_VALIDOS = {7, 15, 30, 60, 90}

def _sanitiza_contrato(raw, campo, atual):
    """Reconstroi o contrato SO com valores confiaveis do servidor: valor_dia da tabela,
    inicio/fim carimbados no relogio do servidor (mesmo que o gate le), dias validado.
    Renovar contrato ainda ativo ESTENDE do fim atual (nao perde dias pagos)."""
    if not isinstance(raw, dict):
        return None
    if raw.get("status") == "inativo":                 # cancelamento: preserva o resto
        base = dict(atual) if isinstance(atual, dict) else {}
        base["status"] = "inativo"
        return base
    try:
        dias = int(raw.get("dias") or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias not in DIAS_VALIDOS:
        dias = 30
    hoje = datetime.now()
    hoje_str = hoje.strftime("%Y-%m-%d")
    at = atual if isinstance(atual, dict) else {}
    fim_atual = str(at.get("fim") or "")
    if at.get("status") == "ativo" and fim_atual and fim_atual >= hoje_str:
        try: base_fim = datetime.strptime(fim_atual, "%Y-%m-%d")
        except ValueError: base_fim = hoje
        inicio = str(at.get("inicio") or hoje_str)      # renovacao preserva a origem
    else:
        base_fim = hoje
        inicio = hoje_str                                # contrato novo/expirado: comeca hoje
    fim = (base_fim + timedelta(days=dias)).strftime("%Y-%m-%d")
    return {"status": "ativo", "valor_dia": PRECO_DIA.get(campo, 0.0),
            "dias": dias, "inicio": inicio, "fim": fim}


@app.post("/listarCamerasIA")
async def listar(req: Request):
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # ROTEAMENTO por engine de decode: detector antigo (vigia0) POSTa sem decode_engine e
    # recebe SO as cpu (nao tenta as roteadas pro NVDEC); detector novo (vigia_nvdec) POSTa
    # decode_engine=nvdec e recebe SO essas. Zero double-open entre os dois detectores.
    eng_req = b.get("decode_engine")
    # config de analiticos por camera (tela "Analiticos por Camera") -> o detector filtra por isso
    cfg_by_cam = {}
    try:
        _c = db()
        for _r in _c.execute("SELECT data FROM entities WHERE entity=?", ("ConfigAnalitico",)).fetchall():
            _d = json.loads(_r["data"])
            if _d.get("camera_id"):
                cfg_by_cam[_d["camera_id"]] = {"ativo": _d.get("ativo", True),
                    "horarios": _d.get("horarios", []), "analiticos_padrao": _d.get("analiticos_padrao", []),
                    "zonas_intrusao": _d.get("zonas_intrusao", [])}
        _c.close()
    except Exception as _e:
        print("[listarCamerasIA] cfg analitico:", _e)
    # so cameras PUBLICANDO agora (mediamtx_ready) — evita o detector gastar tempo em ffprobe de offline
    _pub = None
    try:
        _rd = json.load(open(os.path.join(HERE, "mediamtx_ready.json")))
        if (time.time() - float(_rd.get("ts", 0))) < 360:
            _pub = set(str(x) for x in _rd.get("ready", []))
    except Exception:
        _pub = None
    cams = []
    for o in _todas_cameras():
        url = (o.get("rtsp_url") or o.get("stream_url") or "").strip()
        if url.startswith("rtmp://127.0.0.1:1935/cam/"):  # NVDEC decodifica RTSP melhor que RTMP (detector le RTSP local; camera segue RTMP)
            # topo-3nós: a IA está em outra máquina; entrega o RTSP no host da VM de storage (default = loopback p/ setup antigo).
            url = "rtsp://%s:8554/cam/%s" % (os.getenv("IA_RTSP_HOST", "127.0.0.1"), url.split("/cam/", 1)[1])
        # cameras so-embed (YouTube) NAO vao pra IA — ficam so p/ ver ao vivo + gravar.
        # A IA roda so em cameras com stream direto (RTMP/RTSP/HLS) = as reais.
        if not url:
            continue
        if not _gate_permite(o, "ia_contrato", GATE_IA_ESTRITO):   # gate do add-on IA
            continue
        o_eng = (o.get("decode_engine") or "cpu")
        if eng_req:
            if o_eng != eng_req:
                continue
        elif o_eng == "nvdec":          # detector antigo nao pega as do nvdec
            continue
        if _pub and o.get("stream_key") not in _pub:
            continue    # so quem esta publicando agora vai pro detector
        cams.append({"id": o["id"], "nome": o.get("nome", ""), "cliente_id": o.get("cliente_id", ""),
                     "cliente_nome": o.get("cliente_nome", ""), "cliente_telefone": o.get("cliente_telefone", ""),
                     "provedor_id": o.get("provedor_id", ""), "analitico_id": o.get("analitico_id", ""),
                     "ia_placa": bool(o.get("ia_placa")),   # placa opt-in (camera de entrada/portao)
                     "config_analitico": cfg_by_cam.get(o["id"]),   # gating por camera+horario (tela Analiticos)
                     "decode_engine": o_eng, "embed_url": o.get("embed_url", ""),
                     "stream_url": url})
    if b.get("validar", True) and cams:
        loop = asyncio.get_event_loop()
        # so checa por HTTP as URLs http(s); rtsp:// e caminho local (restream) = assume valido
        http_cams = [c for c in cams if str(c["stream_url"]).startswith("http")]
        results = await asyncio.gather(*[loop.run_in_executor(None, _valida_stream, c["stream_url"]) for c in http_cams])
        for c, (ok, st) in zip(http_cams, results):
            c["stream_valido"], c["stream_status"] = ok, st
        for c in cams:
            if not str(c["stream_url"]).startswith("http"):
                c["stream_valido"], c["stream_status"] = True, 0
    else:
        for c in cams:
            c["stream_valido"] = True
    validas = [c for c in cams if c.get("stream_valido")]
    return {"success": True, "total": len(cams), "online_validas": len(validas), "cameras": cams}

@app.post("/listarCamerasGravacao")
async def listar_gravacao(req: Request):
    """Todas as cameras (incl. YouTube embed) pro gravador."""
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _grav_host = os.getenv("GRAV_STREAM_HOST", "10.93.0.126")
    _ret_def = float(os.getenv("GRAVACAO_RETENCAO_DIAS_DEFAULT", "30"))
    cams = []
    for o in _todas_cameras():
        _dias = int(o.get("dias_gravacao", 0) or 0)
        # opt-in de gravacao: contrato grav ativo (legado) OU dias_gravacao>0 (Cloud do portal)
        if not (_gate_permite(o, "grav_contrato", GATE_GRAV_ESTRITO) or _dias > 0):
            continue
        _rtsp = (o.get("rtsp_url", "") or "")
        # camera MediaMTX: 127.0.0.1 nao e alcancavel da storage -> usa a LAN da Xeon
        if "127.0.0.1:1935/cam/" in _rtsp:
            _rtsp = _rtsp.replace("127.0.0.1", _grav_host)
        cams.append({"id": o["id"], "nome": o.get("nome", ""),
                     "rtsp_url": _rtsp, "stream_url": o.get("stream_url", ""),
                     "embed_url": o.get("embed_url", ""), "status": o.get("status", ""),
                     "retencao_dias": (_dias if _dias > 0 else _ret_def),
                     "cliente_id": o.get("cliente_id", ""), "cliente_nome": o.get("cliente_nome", ""),
                     "provedor_id": o.get("provedor_id", ""), "provedor_nome": o.get("provedor_nome", "")})
    return {"success": True, "cameras": cams}


@app.post("/api/gravacoes/cam-map")
async def cam_map_endpoint(req: Request):
    """Mapa stream_key -> {cliente, camera} pro organizador de gravacao do storage (protegido por secret)."""
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    out = {}
    for o in _todas_cameras():
        k = o.get("stream_key")
        if not k:
            continue
        try:
            _grava = bool(_gate_permite(o, "grav_contrato", GATE_GRAV_ESTRITO) or int(o.get("dias_gravacao", 0) or 0) > 0)
        except Exception:
            _grava = int(o.get("dias_gravacao", 0) or 0) > 0
        out[str(k)] = {"cliente": o.get("cliente_nome") or "", "camera": o.get("nome") or "", "grava": _grava, "dias": int(o.get("dias_gravacao", 0) or 0)}
    return out


@app.post("/api/mediamtx-ready")
async def mediamtx_ready_endpoint(req: Request):
    """Recebe do storage (122) os stream_keys publicando agora (secret); grava arquivo p/ o alerta offline."""
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ready = b.get("ready") or []
    data = {"ready": [str(x) for x in ready], "ts": time.time()}
    p = os.path.join(HERE, "mediamtx_ready.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)
    return {"success": True, "count": len(data["ready"])}


# ==================== GRAVACOES (escopadas) ====================
_ARQ_RE = re.compile(r"^[\w.-]+\.mp4$")

def _cameras_visiveis(u):
    return [o for o in _todas_cameras() if _scope_ok("Camera", o, u)]


def _subusers_do_cliente(cliente_id):
    if not cliente_id:
        return []
    c = db()
    rows = c.execute("SELECT data FROM entities WHERE entity='SubUser' AND json_extract(data,'$.client_id')=?", (cliente_id,)).fetchall()
    c.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data"]))
        except Exception:
            pass
    return out


def _subuser_by_uid(uid):
    c = db()
    r = c.execute("SELECT id, data FROM entities WHERE entity='SubUser' AND json_extract(data,'$.auth_user_id')=?", (uid,)).fetchone()
    c.close()
    if not r:
        return None
    d = json.loads(r["data"]); d["id"] = r["id"]; return d


def _gravacoes_visiveis(u):
    """Cameras cujas gravacoes o usuario pode ver. Sub-usuario: so as liberadas p/ gravacao."""
    if u.get("user_type") == "subuser":
        if u.get("sub_blocked"):
            return []
        allow = set(u.get("allowed_gravacoes") or [])
        cid = u.get("cliente_id") or ""
        return [o for o in _todas_cameras() if o.get("id") in allow and o.get("cliente_id") == cid]
    return _cameras_visiveis(u)

@app.get("/healthz")
def healthz():
    """Sonda publica p/ monitor externo (UptimeRobot).
    200 = backend vivo + watchdog da IA atualizando.
    503 = o ia_health.json ficou velho (>10min) => o watchdog interno PAROU (caso que o
    proprio sistema nao consegue avisar, pois o alertador estaria morto junto)."""
    hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ia_health.json")
    try:
        idade = time.time() - os.stat(hp).st_mtime
        if idade > 600:
            return JSONResponse({"ok": False, "motivo": "watchdog IA parado ha %d min" % int(idade / 60)},
                                status_code=503)
    except Exception:
        pass   # sem arquivo ainda nao derruba: backend respondendo ja e sinal util
    return {"ok": True}


_rec_cache = {}

def _rec_safe(sname):
    s = "".join(c for c in str(sname) if c.isalnum() or c in " -_.()").strip()
    return s or "SEM_NOME"

def _rec_week(dt):
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return "%s_a_%s" % (monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))

def _rec_browse(relpath):
    """Lista pasta de gravacao no storage (media edge browse JSON). Cache 30s."""
    now = time.time()
    hit = _rec_cache.get(relpath)
    if hit and now - hit[0] < 30:
        return hit[1]
    res = []
    try:
        r = requests.get(_rec_signed_url(relpath.strip("/") + "/", 300),
                         headers={"Accept": "application/json"}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, list):
                res = j
    except Exception:
        res = []
    _rec_cache[relpath] = (now, res)
    return res


# ==================== BUSCA INTELIGENTE — "Pergunte ao Corexia" (Fase 1: VLM sob demanda) ====================
# Busca por linguagem natural dentro de gravacoes. Amostra quadros do(s) trecho(s) escolhido(s)
# e pergunta ao Gemini (VLM, via REST) se cada quadro corresponde a descricao do operador.
# Roda em thread de fundo (NUNCA no event loop) — segue a regra anti-freeze do backend.
_BUSCA_JOBS = {}
_BUSCA_DIR = "/tmp/corexia_busca"
_BUSCA_LOCK = threading.Lock()
_BUSCA_STEP = {"rapido": 6.0, "normal": 3.0, "minucioso": 1.5}
_BUSCA_MAX_SEGMENTS = 8      # teto: ate 8 trechos (~2h) por busca
_BUSCA_MAX_FRAMES = 900      # teto duro de quadros por job
_BUSCA_CONF_MIN = 55         # so mostra correspondencia com confianca >= isso


def _busca_reap():
    """Remove jobs e frames com mais de 1h (limpeza preguicosa, chamada ao iniciar)."""
    import shutil
    now = time.time()
    try:
        for jid in list(_BUSCA_JOBS.keys()):
            j = _BUSCA_JOBS.get(jid)
            if j and (now - j.get("created", now)) > 3600:
                _BUSCA_JOBS.pop(jid, None)
                shutil.rmtree(os.path.join(_BUSCA_DIR, jid), ignore_errors=True)
    except Exception:
        pass


def _gemini_match(query, jpeg_path):
    """Pergunta ao Gemini se o quadro corresponde a query. Retorna (match, confianca, motivo).
    match=None => sem chave/config (sinaliza erro fatal do job)."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return (None, 0, "sem_chave")
    model = (os.getenv("BUSCA_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()
    try:
        with open(jpeg_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception:
        return (False, 0, "sem_frame")
    prompt = (
        "Voce analisa 1 quadro de camera de seguranca. O operador procura por: \"%s\".\n"
        "Responda SOMENTE em JSON: {\"match\": true|false, \"confianca\": 0-100, \"motivo\": \"ate 6 palavras\"}.\n"
        "match=true APENAS se o que o operador procura aparece de forma clara e identificavel no quadro. "
        "Em duvida, ou se o objeto/pessoa nao estiver visivel, use match=false." % (query[:300])
    )
    body = {
        "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 120, "responseMimeType": "application/json"},
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key)
    try:
        r = requests.post(url, json=body, timeout=40)
        if r.status_code != 200:
            return (False, 0, "api_%d" % r.status_code)
        j = r.json()
        txt = j["candidates"][0]["content"]["parts"][0]["text"]
        d = json.loads(txt)
        return (bool(d.get("match")), int(d.get("confianca") or 0), str(d.get("motivo") or "")[:60])
    except Exception:
        return (False, 0, "erro")


def _busca_instant_query(camera_id, date, query, topk=24):
    """Consulta o indice CLIP na Xeon via tunel reverso (127.0.0.1:9765). None se indisponivel/nao indexado."""
    url = os.getenv("BUSCA_SVC_URL", "http://127.0.0.1:9765")
    sec = os.getenv("BUSCA_SVC_SECRET", "")
    try:
        r = requests.post(url + "/query", timeout=12,
                          headers={"X-Busca-Secret": sec, "Content-Type": "application/json"},
                          json={"text": query, "camera_key": camera_id, "date": date, "topk": topk, "min_score": 0.18})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _busca_worker(job_id, camera_id, cam_nome, folder, date, segs, query, step):
    """Extrai quadros dos segmentos (ffmpeg via URL assinada) e consulta o VLM. Thread de fundo."""
    job = _BUSCA_JOBS.get(job_id)
    if not job:
        return
    jobdir = os.path.join(_BUSCA_DIR, job_id)
    try:
        os.makedirs(jobdir, exist_ok=True)
    except Exception:
        pass
    # === modo INSTANTANEO: tenta o indice CLIP (Xeon via tunel) antes do Nivel B ===
    try:
        _sel = set(a for a, _si in segs)
        _inst = _busca_instant_query(camera_id, date, query, 24)
        if _inst is not None and _inst.get("indexed") and _inst.get("results"):
            _res = [r for r in _inst["results"] if r.get("arquivo") in _sel]
            job["mode"] = "instant"
            job["total"] = len(_res)

            def _thumb(r):
                if job.get("cancel"):
                    return None
                relpath = folder + "/" + r["arquivo"]
                src = _rec_signed_url(relpath, 1800)
                fp = os.path.join(jobdir, "i_%s.jpg" % secrets.token_hex(4))
                try:
                    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                                    "-ss", str(max(0.0, float(r.get("offset", 0)))), "-i", src,
                                    "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", fp],
                                   timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    return None
                with _BUSCA_LOCK:
                    job["processed"] = job.get("processed", 0) + 1
                if not (os.path.exists(fp) and os.path.getsize(fp) > 0):
                    return None
                clip = _clip_signed_url(relpath, max(0.0, float(r.get("offset", 0)) - 4), 12)
                conf = max(0, min(100, int(round(float(r.get("score", 0)) * 100))))
                return {"frame": os.path.basename(fp), "ts": r.get("ts", ""), "conf": conf,
                        "motivo": "", "clip_url": clip, "arquivo": r["arquivo"]}
            try:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    for res in ex.map(_thumb, _res):
                        if res:
                            job["results"].append(res)
            except Exception:
                pass
            job["results"].sort(key=lambda x: x["ts"])
            job["status"] = "done"
            return
    except Exception:
        pass
    # === fallback: Nivel B (VLM/Gemini) ===
    frames = []   # (frame_path, arquivo, offset_s, seg_inicio)
    for si, (arquivo, seg_inicio) in enumerate(segs):
        if job.get("cancel"):
            break
        relpath = folder + "/" + arquivo
        src = _rec_signed_url(relpath, 1800)
        pat = os.path.join(jobdir, "s%02d_%%05d.jpg" % si)
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src,
                 "-vf", "fps=1/%g,scale=640:-2" % step, "-q:v", "5", pat],
                timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            job["msgs"] = (job.get("msgs") or []) + ["falha ao abrir " + arquivo]
            continue
        i = 1
        while True:
            fp = os.path.join(jobdir, "s%02d_%05d.jpg" % (si, i))
            if not os.path.exists(fp):
                break
            frames.append((fp, arquivo, (i - 1) * step, seg_inicio))
            i += 1
            if len(frames) >= _BUSCA_MAX_FRAMES:
                break
        if len(frames) >= _BUSCA_MAX_FRAMES:
            job["msgs"] = (job.get("msgs") or []) + ["limite de %d quadros atingido" % _BUSCA_MAX_FRAMES]
            break
    job["total"] = len(frames)

    def _one(item):
        fp, arquivo, offset, seg_inicio = item
        if job.get("cancel") or job.get("fatal"):
            return None
        match, conf, motivo = _gemini_match(query, fp)
        with _BUSCA_LOCK:
            job["processed"] = job.get("processed", 0) + 1
        if match is None:
            job["fatal"] = "IA de busca nao configurada no servidor (falta a chave Gemini)."
            return None
        if match and conf >= _BUSCA_CONF_MIN:
            relpath = folder + "/" + arquivo
            clip = _clip_signed_url(relpath, max(0.0, offset - 4), 12)
            try:
                bh, bm, bs = [int(x) for x in seg_inicio.split(":")]
                tot = bh * 3600 + bm * 60 + bs + int(offset)
                ts = "%02d:%02d:%02d" % ((tot // 3600) % 24, (tot % 3600) // 60, tot % 60)
            except Exception:
                ts = seg_inicio
            return {"frame": os.path.basename(fp), "ts": ts, "conf": conf,
                    "motivo": motivo, "clip_url": clip, "arquivo": arquivo}
        try:
            os.remove(fp)   # descarta quadro sem correspondencia (economiza disco)
        except Exception:
            pass
        return None

    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for res in ex.map(_one, frames):
                if res:
                    job["results"].append(res)
                if job.get("fatal") or job.get("cancel"):
                    break
    except Exception as e:
        job["msgs"] = (job.get("msgs") or []) + ["erro: " + str(e)[:120]]
    try:
        job["results"].sort(key=lambda x: x["ts"])
    except Exception:
        pass
    job["status"] = "cancelado" if job.get("cancel") else ("erro" if job.get("fatal") else "done")


@app.post("/api/busca/iniciar")
async def busca_iniciar(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    _busca_reap()
    try:
        b = await req.json()
    except Exception:
        return JSONResponse({"error": "json invalido"}, status_code=400)
    camera_id = (b.get("camera_id") or "").strip()
    data = (b.get("data") or "").strip()
    query = (b.get("query") or "").strip()[:300]
    precisao = (b.get("precisao") or "normal").strip()
    arquivos = b.get("arquivos") or []
    if not query:
        return JSONResponse({"error": "descreva o que procurar"}, status_code=400)
    if not (camera_id and data):
        return JSONResponse({"error": "camera/data faltando"}, status_code=400)
    cam = next((c for c in _gravacoes_visiveis(u) if c["id"] == camera_id), None)
    if not cam:
        return _forbidden("camera nao encontrada ou sem acesso")
    if not cam.get("busca_ia") and u.get("role") != "admin":
        return _forbidden("Pergunte ao Corexia nao esta ativado nesta camera")
    try:
        dt = datetime.strptime(data, "%Y-%m-%d")
    except Exception:
        return JSONResponse({"error": "data invalida"}, status_code=400)
    cliente = _rec_safe(cam.get("cliente_nome") or "SEM_CLIENTE")
    camnome = _rec_safe(cam.get("nome") or "")
    folder = "%s/%s/%s" % (cliente, _rec_week(dt), data)
    segs = []
    for a in arquivos:
        a = str(a or "").strip()
        if ("/" in a) or (".." in a) or (not a.endswith(".mp4")) or (not a.startswith(camnome + "_")):
            continue
        seg_inicio = a[len(camnome) + 1:-4].replace("-", ":")
        segs.append((a, seg_inicio))
        if len(segs) >= _BUSCA_MAX_SEGMENTS:
            break
    if not segs:
        return JSONResponse({"error": "selecione ao menos 1 trecho valido"}, status_code=400)
    step = _BUSCA_STEP.get(precisao, 3.0)
    job_id = secrets.token_hex(8)
    _BUSCA_JOBS[job_id] = {"user_id": u.get("id"), "status": "running", "processed": 0, "total": 0,
                           "results": [], "created": time.time(), "query": query,
                           "cam_nome": cam.get("nome", ""), "msgs": [], "cancel": False, "fatal": None}
    threading.Thread(target=_busca_worker,
                     args=(job_id, camera_id, cam.get("nome", ""), folder, data, segs, query, step),
                     daemon=True).start()
    return {"job_id": job_id, "trechos": len(segs)}


@app.get("/api/busca/cameras")
def busca_cameras(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    is_admin = (u.get("role") == "admin")
    out = []
    for cam in _gravacoes_visiveis(u):
        if not (is_admin or cam.get("busca_ia")):
            continue
        cliente = _rec_safe(cam.get("cliente_nome") or "SEM_CLIENTE")
        camnome = _rec_safe(cam.get("nome") or "")
        dias = set()
        for wk in _rec_browse(cliente):
            if not wk.get("is_dir"):
                continue
            wkname = (wk.get("name") or "").strip("/")
            for dy in _rec_browse(cliente + "/" + wkname):
                if not dy.get("is_dir"):
                    continue
                dyname = (dy.get("name") or "").strip("/")
                for f in _rec_browse(cliente + "/" + wkname + "/" + dyname):
                    if (not f.get("is_dir")) and (f.get("name") or "").startswith(camnome + "_"):
                        dias.add(dyname)
                        break
        if dias:
            out.append({"camera_id": cam["id"], "camera_nome": cam.get("nome", ""),
                        "dias": sorted(dias, reverse=True)})
    return out


@app.get("/api/busca/status")
def busca_status(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    jid = req.query_params.get("job", "")
    job = _BUSCA_JOBS.get(jid)
    if not job or job.get("user_id") != u.get("id"):
        return JSONResponse({"error": "job nao encontrado"}, status_code=404)
    return {"status": job.get("status"), "processed": job.get("processed", 0),
            "total": job.get("total", 0), "found": len(job.get("results", [])),
            "results": job.get("results", []), "query": job.get("query", ""), "mode": job.get("mode", "vlm"),
            "erro": job.get("fatal"), "msgs": (job.get("msgs") or [])[-3:]}


@app.post("/api/busca/cancelar")
async def busca_cancelar(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    try:
        b = await req.json()
    except Exception:
        b = {}
    job = _BUSCA_JOBS.get((b.get("job") or "").strip())
    if job and job.get("user_id") == u.get("id"):
        job["cancel"] = True
    return {"ok": True}


@app.get("/api/busca/frame")
def busca_frame(req: Request):
    u = current_user(req, allow_query_token=True)   # <img> usa ?t= (nao manda header Authorization)
    if not u:
        return _unauth()
    jid = req.query_params.get("job", "")
    fn = req.query_params.get("f", "")
    job = _BUSCA_JOBS.get(jid)
    if not job or job.get("user_id") != u.get("id"):
        return _forbidden()
    if not re.match(r"^[A-Za-z0-9_]+\.jpg$", fn):
        return _forbidden("nome invalido")
    p = os.path.join(_BUSCA_DIR, jid, fn)
    if not os.path.exists(p):
        return JSONResponse({"error": "nao encontrado"}, status_code=404)
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/gravacoes/cameras")
def grav_cameras(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    out = []
    for cam in _gravacoes_visiveis(u):
        cliente = _rec_safe(cam.get("cliente_nome") or "SEM_CLIENTE")
        camnome = _rec_safe(cam.get("nome") or "")
        dias = set()
        for wk in _rec_browse(cliente):
            if not wk.get("is_dir"):
                continue
            wkname = (wk.get("name") or "").strip("/")
            for dy in _rec_browse(cliente + "/" + wkname):
                if not dy.get("is_dir"):
                    continue
                dyname = (dy.get("name") or "").strip("/")
                for f in _rec_browse(cliente + "/" + wkname + "/" + dyname):
                    if (not f.get("is_dir")) and (f.get("name") or "").startswith(camnome + "_"):
                        dias.add(dyname)
                        break
        if dias:
            out.append({"camera_id": cam["id"], "camera_nome": cam.get("nome", ""),
                        "dias": sorted(dias, reverse=True)})
    return out

@app.get("/api/gravacoes")
def grav_list(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    camera_id = req.query_params.get("camera_id", "")
    data = req.query_params.get("data", "")          # YYYY-MM-DD
    cam = next((c for c in _gravacoes_visiveis(u) if c["id"] == camera_id), None)
    if not cam:
        return _forbidden("camera nao encontrada ou sem acesso")
    if not data:
        return []
    cliente = _rec_safe(cam.get("cliente_nome") or "SEM_CLIENTE")
    camnome = _rec_safe(cam.get("nome") or "")
    try:
        dt = datetime.strptime(data, "%Y-%m-%d")
    except Exception:
        return []
    folder = "%s/%s/%s" % (cliente, _rec_week(dt), data)
    out = []
    for f in _rec_browse(folder):
        nm = f.get("name") or ""
        if f.get("is_dir") or not nm.endswith(".mp4") or not nm.startswith(camnome + "_"):
            continue
        inicio = nm[len(camnome) + 1:-4].replace("-", ":")
        mb = round((f.get("size", 0) or 0) / 1048576, 1)
        out.append({"arquivo": nm, "data": data, "inicio": inicio, "tamanho_mb": mb,
                    "url": _rec_signed_url(folder + "/" + nm)})
    out.sort(key=lambda x: x["inicio"])
    return out

@app.get("/api/gravacoes/sharelink")
def grav_sharelink(req: Request):
    """Link temporario (default 7 dias) de UMA gravacao, escopado ao cliente (baixar/compartilhar)."""
    u = current_user(req)
    if not u:
        return _unauth()
    relpath = (req.query_params.get("path") or "").strip().lstrip("/")
    if not relpath or ".." in relpath or not relpath.lower().endswith(".mp4"):
        return JSONResponse({"error": "path invalido"}, status_code=400)
    allowed = set(_rec_safe(c.get("cliente_nome") or "SEM_CLIENTE") for c in _gravacoes_visiveis(u))
    if relpath.split("/")[0] not in allowed:
        return _forbidden("sem acesso a esta gravacao")
    try:
        dias = max(1, min(int(req.query_params.get("dias", "7") or 7), 30))
    except Exception:
        dias = 7
    return {"url": _rec_signed_url(relpath, ttl=dias * 86400), "dias": dias}


@app.get("/api/gravacoes/clip")
def grav_clip(req: Request):
    """Recorta um pedaco de UMA gravacao (ffmpeg -c copy, via range) e devolve como download. Escopado ao cliente."""
    u = current_user(req, allow_query_token=True)
    if not u:
        return _unauth()
    relpath = (req.query_params.get("path") or "").strip().lstrip("/")
    if not relpath or ".." in relpath or not relpath.lower().endswith(".mp4"):
        return JSONResponse({"error": "path invalido"}, status_code=400)
    allowed = set(_rec_safe(c.get("cliente_nome") or "SEM_CLIENTE") for c in _gravacoes_visiveis(u))
    if relpath.split("/")[0] not in allowed:
        return _forbidden("sem acesso a esta gravacao")
    try:
        start = max(0.0, float(req.query_params.get("start", "0") or 0))
        dur = float(req.query_params.get("dur", "0") or 0)
    except Exception:
        return JSONResponse({"error": "tempos invalidos"}, status_code=400)
    if dur <= 0 or dur > 300:
        return JSONResponse({"error": "duracao invalida (1s a 5min)"}, status_code=400)
    from starlette.background import BackgroundTask
    src = _rec_signed_url(relpath, 900)
    outdir = "/tmp/corexia_clips"; os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, secrets.token_hex(8) + ".mp4")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(start), "-i", src, "-t", str(dur), "-c", "copy", "-movflags", "+faststart", out], timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return JSONResponse({"error": "falha ao recortar"}, status_code=500)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
        return JSONResponse({"error": "recorte vazio (tente outro intervalo)"}, status_code=500)
    fname = os.path.basename(relpath)[:-4] + "_recorte.mp4"
    return FileResponse(out, media_type="video/mp4", filename=fname, background=BackgroundTask(lambda p=out: (os.path.exists(p) and os.remove(p))))


def _clip_sign(relpath, start_s, dur_s, exp):
    return hmac.new(_MEDIA_KEY, ("clip|%s|%s|%s|%s" % (relpath, start_s, dur_s, exp)).encode("utf-8"), hashlib.sha256).hexdigest()


def _clip_signed_url(relpath, start, dur, ttl=7 * 86400):
    from urllib.parse import quote
    exp = int(time.time()) + int(ttl)
    start_s = str(start); dur_s = str(dur)
    sig = _clip_sign(relpath, start_s, dur_s, exp)
    base = os.getenv("PANEL_BASE", "https://grupocorexia.com.br").rstrip("/")
    return "%s/api/gravacoes/clip-dl?path=%s&start=%s&dur=%s&exp=%s&sig=%s" % (base, quote(relpath), start_s, dur_s, exp, sig)


@app.get("/api/gravacoes/clip-dl")
def grav_clip_dl(req: Request):
    """Baixa o CORTE via link assinado (sem login) — usado nos e-mails. Gera o corte na hora do clique."""
    from starlette.responses import PlainTextResponse
    q = req.query_params
    relpath = (q.get("path") or "").strip().lstrip("/")
    start_s = q.get("start") or "0"; dur_s = q.get("dur") or "0"
    sig = (q.get("sig") or "").split("?")[0]
    try:
        exp = int(q.get("exp") or 0)
    except Exception:
        return PlainTextResponse("Link invalido.", status_code=400)
    if not relpath or ".." in relpath or not relpath.lower().endswith(".mp4"):
        return PlainTextResponse("Link invalido.", status_code=400)
    if exp < int(time.time()):
        return PlainTextResponse("Link expirado. Peca um novo pelo portal.", status_code=410)
    if not hmac.compare_digest(sig, _clip_sign(relpath, start_s, dur_s, exp)):
        return PlainTextResponse("Assinatura invalida.", status_code=403)
    try:
        start = max(0.0, float(start_s)); dur = float(dur_s)
    except Exception:
        return PlainTextResponse("Tempos invalidos.", status_code=400)
    if dur <= 0 or dur > 300:
        return PlainTextResponse("Duracao invalida.", status_code=400)
    from starlette.background import BackgroundTask
    src = _rec_signed_url(relpath, 900)
    outdir = "/tmp/corexia_clips"; os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, secrets.token_hex(8) + ".mp4")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(start), "-i", src, "-t", str(dur), "-c", "copy", "-movflags", "+faststart", out], timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return PlainTextResponse("Falha ao gerar o corte. Tente novamente.", status_code=500)
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        if os.path.exists(out):
            os.remove(out)
        return PlainTextResponse("Corte vazio (tente outro intervalo).", status_code=500)
    fname = os.path.basename(relpath)[:-4] + "_corte.mp4"
    return FileResponse(out, media_type="video/mp4", filename=fname, background=BackgroundTask(lambda p=out: (os.path.exists(p) and os.remove(p))))


def _email_send_worker(relpath, to, msg, st, du, muser, mpass):
    import smtplib, ssl
    from email.message import EmailMessage
    try:
        label = os.path.basename(relpath)
        is_clip = (st is not None and du)
        if is_clip:
            link = _clip_signed_url(relpath, st, du, 7 * 86400)
            body = "Segue o link do CORTE solicitado (valido 7 dias). Clique para baixar/assistir:\n\n" + link
        else:
            link = _rec_signed_url(relpath, ttl=7 * 86400)
            body = "Segue o link da gravacao (valido 7 dias). Clique para baixar/assistir:\n\n" + link
        m = EmailMessage()
        m["From"] = "Corexia <%s>" % muser; m["To"] = ", ".join(to); m["Subject"] = "Gravacao Corexia - " + label
        text = (msg + "\n\n") if msg else ""
        text += body + "\n\n-- Corexia"
        m.set_content(text)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60) as srv:
            srv.login(muser, mpass); srv.send_message(m)
        print("email(link) enviado p/", ", ".join(to), "clip" if is_clip else "full", flush=True)
        return {"success": True, "mode": "link"}
    except Exception as e:
        print("email worker ERRO:", str(e)[:160], flush=True)
        return {"error": "falha ao enviar: " + str(e)[:140]}


@app.post("/api/gravacoes/email")
async def grav_email(req: Request):
    """Envia UMA gravacao (ou recorte) por e-mail como LINK assinado (nao anexa; anexo de video e barrado por varios provedores). Trabalho em thread pra nao travar o backend."""
    u = current_user(req)
    if not u:
        return _unauth()
    b = await req.json()
    relpath = (b.get("path") or "").strip().lstrip("/")
    if not relpath or ".." in relpath or not relpath.lower().endswith(".mp4"):
        return JSONResponse({"error": "path invalido"}, status_code=400)
    allowed = set(_rec_safe(c.get("cliente_nome") or "SEM_CLIENTE") for c in _gravacoes_visiveis(u))
    if relpath.split("/")[0] not in allowed:
        return _forbidden("sem acesso a esta gravacao")
    to = [e.strip() for e in re.split(r"[,;\s]+", (b.get("to") or "")) if "@" in e][:5]
    if not to:
        return JSONResponse({"error": "informe ao menos um e-mail valido"}, status_code=400)
    msg = (b.get("msg") or "").strip()[:2000]
    muser = os.getenv("MAIL_USER", ""); mpass = os.getenv("MAIL_PASS", "")
    if not (muser and mpass):
        return JSONResponse({"error": "e-mail nao configurado no servidor"}, status_code=500)
    st = b.get("start"); du = b.get("dur")
    if st is not None and du:
        try:
            st = max(0.0, float(st)); du = float(du)
        except Exception:
            return JSONResponse({"error": "tempos invalidos"}, status_code=400)
        if du <= 0 or du > 300:
            return JSONResponse({"error": "duracao invalida"}, status_code=400)
    else:
        st = None; du = None
    import asyncio, functools
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, functools.partial(_email_send_worker, relpath, to, msg, st, du, muser, mpass))
    return JSONResponse(result, status_code=200 if result.get("success") else 500)


@app.get("/gravacao/{camera_id}/{arquivo}")
async def grav_file(camera_id: str, arquivo: str, req: Request):
    u = current_user(req, allow_query_token=True)   # ?t= permitido SO aqui (tag <video>)
    if not u:
        return _unauth()
    if not _ARQ_RE.match(arquivo) or "/" in camera_id or ".." in camera_id:
        return JSONResponse({"error": "invalido"}, status_code=400)
    if not any(c["id"] == camera_id for c in _gravacoes_visiveis(u)):
        return _forbidden()
    p = os.path.join(GRAV_DIR, camera_id, arquivo)
    if not os.path.exists(p):
        return JSONResponse({"error": "nao encontrada"}, status_code=404)
    return FileResponse(p, media_type="video/mp4")



def _rec_sign(relpath, exp):
    return hmac.new(_MEDIA_KEY, ("/rec/" + relpath + "|" + str(exp)).encode("utf-8"), hashlib.sha256).hexdigest()

def _rec_signed_url(relpath, ttl=None):
    from urllib.parse import quote
    exp = int(time.time()) + int(ttl or MEDIA_TOKEN_TTL)
    base = os.getenv("MEDIA_EDGE_BASE", "https://media.grupocorexia.com.br")
    return "%s/rec/%s?exp=%d&sig=%s" % (base, quote(relpath), exp, _rec_sign(relpath, exp))

@app.get("/api/rec-ok")
def rec_ok(req: Request):
    """forward_auth do Caddy no storage (/rec/*): valida URL assinada (exp+sig)."""
    from urllib.parse import urlsplit, parse_qs, unquote
    sp = urlsplit(req.headers.get("x-forwarded-uri", ""))
    path = unquote(sp.path)
    q = parse_qs(sp.query)
    exp = (q.get("exp") or [""])[0].split("?")[0]; sig = (q.get("sig") or [""])[0].split("?")[0]
    if not (exp and sig and path.startswith("/rec/")):
        return Response(status_code=403)
    try:
        if int(exp) < int(time.time()):
            return Response(status_code=403)
    except Exception:
        return Response(status_code=403)
    if hmac.compare_digest(_rec_sign(path[len("/rec/"):], exp), sig):
        return Response(status_code=200)
    return Response(status_code=403)


# ==================== LIVE HLS (restream do gravador, p/ o player web) ====================
@app.get("/live/{camera_id}/{arquivo}")
def live_hls(camera_id: str, arquivo: str, req: Request):
    # HLS ao vivo — EXIGE auth e escopo de tenant (igual /gravacao). ?t= permitido pq o
    # player (hls.js/<video>) nao manda header nos segmentos; o m3u8 e reescrito p/ herdar o token.
    u = current_user(req, allow_query_token=True)
    if not u:
        return _unauth()
    if ("/" in camera_id or ".." in camera_id or "/" in arquivo or ".." in arquivo
            or not (arquivo.endswith(".m3u8") or arquivo.endswith(".ts"))):
        return JSONResponse({"error": "invalido"}, status_code=400)
    if not any(c["id"] == camera_id for c in _cameras_visiveis(u)):
        return _forbidden()
    p = os.path.join(LIVE_DIR, camera_id, arquivo)
    if not os.path.exists(p):
        return JSONResponse({"error": "sem transmissao ao vivo ainda"}, status_code=404)
    if arquivo.endswith(".m3u8"):
        # reescreve as URLs dos segmentos .ts pra carregar o mesmo ?t= (o player nao manda header)
        tok = req.query_params.get("t") or (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
        out = []
        for ln in open(p, "r", encoding="utf-8", errors="ignore").read().splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and tok:
                ln = ln + ("&" if "?" in s else "?") + "t=" + tok
            out.append(ln)
        return Response("\n".join(out) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})
    return FileResponse(p, media_type="video/mp2t", headers={"Cache-Control": "no-cache"})


# ==================== THUMBNAIL / FOTO DE CAPA DA CAMERA ====================
# 1 frame do restream local (gravacoes_live) -> jpg pequeno cacheado. Usado como capa dos
# cards do "Visualizar Cameras" (a grade nao toca video, so a foto = leve).
THUMB_DIR  = os.path.join(HERE, "thumbs"); os.makedirs(THUMB_DIR, exist_ok=True)
THUMB_TTL  = int(os.getenv("THUMB_TTL", "120"))            # regenera no maximo a cada Xs
_thumb_sem = threading.Semaphore(int(os.getenv("THUMB_CONC", "4")))  # limita ffmpeg simultaneos

def _gen_thumb(camera_id):
    # fonte 1: restream local (gravador). fonte 2: stream direto (MediaMTX rtmp / rtsp / m3u8)
    live = os.path.join(LIVE_DIR, camera_id, "index.m3u8")
    src = live if os.path.exists(live) else ""
    if not src:
        cam = _get_entity("Camera", camera_id) or {}
        u = (cam.get("rtsp_url") or cam.get("stream_url") or "").strip()
        if u.startswith("rtmp://") or u.startswith("rtsp://") or ".m3u8" in u:
            src = u
    if not src:
        return False
    src = src.replace("127.0.0.1", os.getenv("STREAM_INGEST_HOST", "181.191.109.136"))
    out = os.path.join(THUMB_DIR, f"{camera_id}.jpg")
    with _thumb_sem:
        # outro request pode ter gerado enquanto esperava o semaforo
        if os.path.exists(out) and time.time() - os.path.getmtime(out) < THUMB_TTL:
            return True
        try:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-rw_timeout", "9000000", "-i", src, "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "5", out],
                           timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False
    return os.path.exists(out)

_thumb_inflight = set()
def _gen_thumb_bg(camera_id):
    if camera_id in _thumb_inflight:
        return
    _thumb_inflight.add(camera_id)
    try:
        _gen_thumb(camera_id)
    finally:
        _thumb_inflight.discard(camera_id)


@app.get("/camthumb/{camera_id}")
def camthumb(camera_id: str, req: Request):
    if "/" in camera_id or ".." in camera_id:
        return JSONResponse({"error": "invalido"}, status_code=400)
    _camobj = _get_entity("Camera", camera_id) or {}
    if not _camobj.get("publico"):            # camera privada: exige login + escopo
        u = current_user(req, allow_query_token=True)
        if not u:
            return _unauth()
        if not any(c["id"] == camera_id for c in _cameras_visiveis(u)):
            return _forbidden()
    out = os.path.join(THUMB_DIR, f"{camera_id}.jpg")
    if not (os.path.exists(out) and time.time() - os.path.getmtime(out) < THUMB_TTL):
        if os.path.exists(out):
            threading.Thread(target=_gen_thumb_bg, args=(camera_id,), daemon=True).start()
        else:
            _gen_thumb(camera_id)
    if os.path.exists(out):
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": f"public, max-age={THUMB_TTL}"})
    return JSONResponse({"error": "sem thumbnail"}, status_code=404)


# ==================== IMAGENS DE ALERTA ====================
# nome = token_hex unico por alerta => imutavel => cache agressivo (nao re-baixa no polling)
# "private": conteudo autenticado — so o browser do usuario cacheia, proxy/CDN nao
_IMG_CACHE = {"Cache-Control": "private, max-age=31536000, immutable"}

@app.get("/img/{name}")
def img(name: str, req: Request, w: int = 0):
    u = current_user(req, allow_query_token=True)   # ?t= permitido (tag <img> nao manda header)
    if not u:
        return _unauth()
    if "/" in name or ".." in name:
        return JSONResponse({"error": "invalido"}, status_code=400)
    if u["role"] != "admin":
        # escopo por tenant: nome = "<id_alerta>-<hex>.jpg" -> confere o dono da camera do alerta
        try:
            aid = int(name.split("-", 1)[0])
        except ValueError:
            return _forbidden()
        c = db(); r = c.execute("SELECT camera_id, imagem FROM alertas WHERE id=?", (aid,)).fetchone(); c.close()
        if not r or r["imagem"] != name:
            return _forbidden()
        cam = _get_entity("Camera", r["camera_id"])
        if not cam or not _scope_ok("Camera", cam, u):
            return _forbidden()
    p = os.path.join(IMG_DIR, name)
    if not os.path.exists(p):
        return JSONResponse({"error": "nao encontrada"}, status_code=404)
    # ?w=NNN => thumbnail leve (gerado 1x e cacheado em disco). Evidencia cheia = 300KB;
    # thumb de 200px ~ 12KB. As listas de alerta usam ?w=200; o zoom usa a imagem cheia.
    if 0 < w <= 800:
        thumb = os.path.join(IMG_DIR, f".thumb{w}_{name}")
        if not os.path.exists(thumb):
            try:
                from PIL import Image
                im = Image.open(p); im.thumbnail((w, w * 2))
                im.convert("RGB").save(thumb, "JPEG", quality=72, optimize=True)
            except Exception:
                thumb = p   # se falhar, serve a original
        return FileResponse(thumb, headers=_IMG_CACHE)
    return FileResponse(p, headers=_IMG_CACHE)


# ==================== GRAVADOR (processo filho supervisionado) ====================
_grav_proc = None

@app.on_event("startup")
def _start_gravador():
    global _grav_proc
    threading.Thread(target=_bg_resolve_youtube, daemon=True).start()
    print("[yt] resolvedor YouTube->HLS iniciado")
    if not GRAVADOR_ATIVO:
        return
    script = os.path.join(HERE, "gravador.py")
    if os.path.exists(script):
        _grav_proc = subprocess.Popen([sys.executable, script])
        print(f"[gravador] iniciado (pid {_grav_proc.pid})")

@app.on_event("shutdown")
def _stop_gravador():
    if _grav_proc:
        try:
            _grav_proc.terminate()
        except Exception:
            pass


# ==================== INTEGRACOES / FUNCOES (compat Base44) ====================
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

@app.post("/api/integrations/uploadFile")
async def upload_file(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    form = await req.form()
    f = form.get("file")
    if not f or not hasattr(f, "read"):
        return JSONResponse({"error": "sem arquivo"}, status_code=400)
    ext = os.path.splitext(getattr(f, "filename", "") or "")[1][:8].lower()
    if ext not in _IMG_EXT:
        ext = ".jpg"
    name = secrets.token_hex(16) + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as out:
        out.write(await f.read())
    return {"file_url": f"/uploads/{name}"}

@app.post("/api/integrations/{acao}")
async def integracao_stub(acao: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # invokeLLM/sendEmail/generateImage ainda nao configurados — retorno benigno (nao quebra a pagina)
    return {"success": False, "message": f"integracao '{acao}' indisponivel neste backend"}

@app.post("/api/functions/{name}")
async def funcao_stub(name: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    # funcoes serverless do Base44 (analisarCameraAuto/hlsProxy/Analitico) ainda nao portadas.
    # A IA REAL roda no detector (vigia0), nao aqui — retorno benigno pra nao quebrar telas legadas.
    return {"success": False, "data": {"resultados": []}, "message": f"funcao '{name}' indisponivel (a IA roda no detector)"}


# ==================== WORKSPACES DE GRAVACAO (por provedor/cliente) ====================
_GRAV_TZ_H = int(os.getenv("GRAV_TZ_OFFSET_H", "-3"))   # Brasil (America/Sao_Paulo) = GMT-3, sem horario de verao
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d")
def _ts_local(v):
    """string timestamp UTC (%Y-%m-%dT%H:%M:%S ou com espaco) -> mesma string em horario local (Brasil). No-op nos demais."""
    if not isinstance(v, str) or not _TS_RE.match(v):
        return v
    fmt = "%Y-%m-%dT%H:%M:%S" if "T" in v[:19] else "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(v[:19], fmt) + timedelta(hours=_GRAV_TZ_H)).strftime(fmt)
    except Exception:
        return v
def _obj_ts_local(o):
    """aplica _ts_local a todo campo string do dict (created_date/updated_date + timestamps internos)."""
    if isinstance(o, dict):
        for k, v in list(o.items()):
            if isinstance(v, str):
                o[k] = _ts_local(v)
    return o
def _grav_local_dt(fname):
    """nome YYYY-MM-DD_HH-MM-SS.mp4 (gravado em UTC pelo recorder) -> datetime no fuso local (Brasil)."""
    try:
        return datetime.strptime(fname[:19], "%Y-%m-%d_%H-%M-%S") + timedelta(hours=_GRAV_TZ_H)
    except Exception:
        return None


def _grav_info_camera(camera_id):
    """dias + contagem + tamanho (MB) das gravacoes de UMA camera (le GRAV_DIR / NFS)."""
    d = os.path.join(GRAV_DIR, camera_id)
    dias, nfiles, size = {}, 0, 0
    try:
        arqs = os.listdir(d)
    except OSError:
        return {"dias": [], "dias_contagem": {}, "total_arquivos": 0, "tamanho_mb": 0.0}
    for f in arqs:
        if not _ARQ_RE.match(f) or len(f) < 10:
            continue
        _ldt = _grav_local_dt(f)
        dia = _ldt.strftime("%Y-%m-%d") if _ldt else f[:10]
        dias[dia] = dias.get(dia, 0) + 1
        nfiles += 1
        try:
            size += os.path.getsize(os.path.join(d, f))
        except OSError:
            pass
    return {"dias": sorted(dias.keys(), reverse=True), "dias_contagem": dias,
            "total_arquivos": nfiles, "tamanho_mb": round(size / 1048576, 1)}


def _build_workspaces(cams):
    """agrupa cameras JA ESCOPADAS em provedor -> cliente -> cameras (com gravacoes)."""
    provs = {}
    for cam in cams:
        pid = (cam.get("provedor_id") or "")
        pnome = (cam.get("provedor_nome") or "") or "Sem provedor"
        cid = (cam.get("cliente_id") or "")
        cnome = (cam.get("cliente_nome") or "") or "Nao atribuidas"
        info = _grav_info_camera(cam["id"])
        prov = provs.setdefault(pid, {"provedor_id": pid, "provedor_nome": pnome, "clientes": {}})
        cli = prov["clientes"].setdefault(cid, {"cliente_id": cid, "cliente_nome": cnome, "cameras": []})
        cli["cameras"].append({"camera_id": cam["id"], "camera_nome": cam.get("nome", ""),
                               "decode_engine": cam.get("decode_engine", "") or "", **info})
    out = []
    for prov in provs.values():
        clientes = []
        for cli in prov["clientes"].values():
            cs = sorted(cli["cameras"], key=lambda x: (x["camera_nome"] or "").lower())
            clientes.append({**cli, "cameras": cs,
                             "total_arquivos": sum(c["total_arquivos"] for c in cs),
                             "tamanho_mb": round(sum(c["tamanho_mb"] for c in cs), 1)})
        clientes.sort(key=lambda x: (x["cliente_id"] == "", (x["cliente_nome"] or "").lower()))
        out.append({**prov, "clientes": clientes})
    out.sort(key=lambda x: (x["provedor_id"] == "", (x["provedor_nome"] or "").lower()))
    return out


@app.get("/api/gravacoes/workspaces")
async def grav_workspaces(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    cams = _cameras_visiveis(u)   # ja escopado: admin=tudo, provedor/cliente=so os seus
    # o scan de diretorios (NFS) roda em threadpool p/ nao travar o event loop do backend
    loop = asyncio.get_event_loop()
    ws = await loop.run_in_executor(None, _build_workspaces, cams)
    return {"role": u["role"], "workspaces": ws}


@app.get("/gravacoes-hd")
def gravacoes_hd_page():
    p = os.path.join(HERE, "gravacoes_hd.html")
    if os.path.exists(p):
        return FileResponse(p, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>Corexia</h1><p>pagina de gravacoes nao publicada.</p>", status_code=404)


# ==================== INFRAESTRUTURA (monitor do dashboard admin) ====================
# Metricas da propria Xeon direto do /proc + nvidia-smi. As da storage (10.93.0.122)
# vem de _metrics.json que um cron de la escreve no proprio disco de gravacoes —
# o Xeon le pelo mount NFS (GRAV_DIR), sem precisar de rede/credencial extra.
def _infra_cpu_pct():
    def snap():
        with open("/proc/stat") as f:
            p = list(map(int, f.readline().split()[1:8]))
        return sum(p), p[3] + p[4]
    t1, i1 = snap(); time.sleep(0.25); t2, i2 = snap()
    dt = t2 - t1
    return round(100.0 * (1 - (i2 - i1) / dt), 1) if dt else 0.0

def _infra_mem():
    m = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, v = ln.split(":", 1)
            m[k] = int(v.strip().split()[0])
    tot, av = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    return {"total_gb": round(tot / 1048576, 1), "usado_gb": round((tot - av) / 1048576, 1),
            "pct": round(100.0 * (tot - av) / tot, 1) if tot else 0}

def _infra_disco(path):
    try:
        s = os.statvfs(path)
        tot = s.f_blocks * s.f_frsize
        usado = tot - s.f_bavail * s.f_frsize
        return {"total_tb": round(tot / 1e12, 2), "usado_tb": round(usado / 1e12, 2),
                "pct": round(100.0 * usado / tot, 1) if tot else 0}
    except OSError:
        return None

def _i(x):
    """int tolerante: '[N/A]', '', lixo -> 0 (uma GPU esquisita nao derruba as outras)."""
    x = str(x).strip()
    return int(x) if x.lstrip("-").isdigit() else 0

def _infra_gpus():
    out, dec = [], {}
    try:
        d = subprocess.run(["nvidia-smi", "dmon", "-c", "1", "-s", "u"],
                           capture_output=True, text=True, timeout=6)
        for ln in d.stdout.splitlines():
            p = ln.split()
            if p and p[0].isdigit() and len(p) > 4 and p[4].lstrip("-").isdigit():
                dec[int(p[0])] = max(0, int(p[4]))
    except Exception as e:
        print("[infra] dmon:", str(e)[:80])
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6)
        for ln in r.stdout.strip().splitlines():
            try:   # uma linha nao-parseavel NAO descarta as GPUs seguintes
                i, nome, ut, mu, mtot, tp = [x.strip() for x in ln.split(",")]
                idx = _i(i)
                out.append({"idx": idx, "nome": nome, "uso_pct": _i(ut), "decoder_pct": dec.get(idx, 0),
                            "vram_usada_mb": _i(mu), "vram_total_mb": _i(mtot), "temp_c": _i(tp)})
            except Exception:
                continue
    except Exception as e:
        print("[infra] gpus:", str(e)[:80])
    return out

def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        print("[infra]", str(e)[:80]); return default

def _infra_xeon():
    """Metricas LOCAIS do Xeon (/proc, statvfs de '/', nvidia-smi) — nada de NFS aqui,
    entao pode rodar no pool global sem risco de travar."""
    return {"cpu_pct": _safe(_infra_cpu_pct, 0.0),
            "load": _safe(lambda: round(os.getloadavg()[0], 2), 0),
            "nucleos": os.cpu_count(),
            "mem": _safe(_infra_mem, None),
            "disco": _safe(lambda: _infra_disco("/"), None),
            "gpus": _safe(_infra_gpus, [])}

def _infra_storage_nfs():
    """Toca o mount NFS (GRAV_DIR): statvfs + _metrics.json. So roda no pool DEDICADO
    (_nfs_pool, 1 worker) com single-flight — se o NFS pendurar, no maximo 1 thread fica
    presa e NUNCA sangra a pool global (que serve /listarCamerasIA e /api/gravacoes)."""
    st = {"disco": _infra_disco(GRAV_DIR)}
    try:
        with open(os.path.join(GRAV_DIR, "_metrics.json")) as f:
            m = json.load(f)
        st.update(m)
        if time.time() - m.get("ts", 0) >= 300:
            st["stale"] = True
    except Exception:
        st["sem_agente"] = True
    return st

# pool dedicado (1 worker) + single-flight: mount pendurado prende no maximo 1 thread aqui
_nfs_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infra-nfs")
_nfs_state = {"fut": None, "last": None}

async def _infra_storage():
    f = _nfs_state["fut"]
    if f is None or f.done():
        if f is not None:                       # coleta o resultado da rodada anterior
            try: _nfs_state["last"] = f.result()
            except Exception: _nfs_state["last"] = {"sem_agente": True}
        _nfs_state["fut"] = _nfs_pool.submit(_infra_storage_nfs)
    try:                                        # espera curto; se o NFS travar, usa o ultimo
        return await asyncio.wait_for(asyncio.wrap_future(_nfs_state["fut"]), timeout=3)
    except Exception:
        return _nfs_state["last"] or {"sem_agente": True}

_infra_cache = {"t": 0.0, "data": None}
_infra_lock = asyncio.Lock()

_vps_net_state = {"ts": 0.0, "ifs": {}}
def _vps_net():
    """RX/TX (Mbps) das interfaces da propria VPS, delta entre chamadas do /api/infra."""
    now = time.time(); cur = {}
    try:
        with open("/proc/net/dev") as _f:
            for _ln in _f.readlines()[2:]:
                _nm, _s, _rest = _ln.partition(":")
                _nm = _nm.strip()
                if _nm == "lo" or _nm.startswith(("veth", "docker", "br-", "virbr", "tap")):
                    continue
                _p = _rest.split(); cur[_nm] = (int(_p[0]), int(_p[8]))
    except Exception:
        return None
    prev = _vps_net_state; dt = now - prev["ts"]; ifs = []; rxt = 0.0; txt = 0.0
    if prev["ts"] and dt > 0.2:
        for _nm, _v in cur.items():
            if _nm in prev["ifs"]:
                _rx = max(0, _v[0] - prev["ifs"][_nm][0]) * 8 / 1e6 / dt
                _tx = max(0, _v[1] - prev["ifs"][_nm][1]) * 8 / 1e6 / dt
                rxt += _rx; txt += _tx
                ifs.append({"iface": _nm, "rx_mbps": round(_rx, 2), "tx_mbps": round(_tx, 2)})
    _vps_net_state["ts"] = now; _vps_net_state["ifs"] = cur
    return {"rx_mbps": round(rxt, 2), "tx_mbps": round(txt, 2), "ifaces": ifs}

_NET_WIDGET_JS = '<script>/* corexia-net */(function(){\n var TOK=localStorage.getItem(\'corexia_token\'); if(!TOK)return;\n var data=null;\n function fmt(v){ v=+v||0; if(v>=1000)return (v/1000).toFixed(1)+\' Gb/s\'; if(v<0.01)return \'0\'; return v.toFixed(v<10?2:0)+\' Mb/s\'; }\n function netHtml(net){ net=net||{}; return \'<span style="color:#34d399;white-space:nowrap">&#9660; \'+fmt(net.rx_mbps)+\'</span><span style="color:#f97316;white-space:nowrap;margin-left:12px">&#9650; \'+fmt(net.tx_mbps)+\'</span>\'; }\n function findCard(txt){ var els=document.querySelectorAll(\'div[class*="rounded-2xl"]\');\n   for(var i=0;i<els.length;i++){ if((els[i].textContent||\'\').indexOf(txt)>=0) return els[i]; } return null; }\n function injLine(card,net,key,label){ if(!card)return;\n   var id=\'cxnet-\'+key; var el=card.querySelector(\'#\'+id);\n   if(!el){ el=document.createElement(\'div\'); el.id=id;\n     el.style.cssText=\'display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;padding-top:11px;border-top:1px solid rgba(255,255,255,.07);font-size:12.5px;font-variant-numeric:tabular-nums\';\n     card.appendChild(el); }\n   el.innerHTML=\'<span style="color:#8b96a6;font-weight:600">&#127760; \'+label+\'</span><span>\'+netHtml(net)+\'</span>\';\n }\n function injVPS(net){ var ia=findCard(\'Xeon\'); if(!ia||!ia.parentElement)return;\n   var grid=ia.parentElement, id=\'cxnet-vpscard\', el=grid.querySelector(\'#\'+id);\n   if(!el){ el=document.createElement(\'div\'); el.id=id; el.className=ia.className; grid.appendChild(el); }\n   el.innerHTML=\'<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">\'\n     +\'<div><div style="font-weight:700;font-size:15px">VPS — Controle</div><div style="color:#8b96a6;font-size:12px;margin-top:3px">179.198.127.65 · SaaS + Cobrança + Banco</div></div>\'\n     +\'<div style="font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap"><span style="color:#8b96a6;font-weight:600;margin-right:10px">&#127760; Rede</span>\'+netHtml(net)+\'</div></div>\';\n }\n function removeFloating(){ var f=document.getElementById(\'cxnet\'); if(f)f.remove(); }\n function apply(){ if(!data)return; removeFloating();\n   injLine(findCard(\'Xeon\'),(data.xeon||{}).net,\'ia\',\'Rede (10.93.0.126)\');\n   injLine(findCard(\'Storage\'),(data.storage||{}).net,\'stg\',\'Rede (10.93.0.122)\');\n   injVPS(data.vps_net);\n }\n function poll(){ fetch(\'/api/infra\',{headers:{\'Authorization\':\'Bearer \'+TOK}}).then(function(r){ if(!r.ok)throw 0; return r.json(); })\n   .then(function(d){ data=d; apply(); }).catch(function(){}); }\n setInterval(apply,1500);\n setTimeout(function(){ poll(); setInterval(poll,5000); },1200);\n})();</script>'

_ANLX_JS = '<script>/* corexia-anlx */(function(){\n var TOK=localStorage.getItem(\'corexia_token\'); if(!TOK)return;\n var PATHS=[\'/config-analiticos\'];\n var ANALITICOS=[\n   [\'arma\',\'Arma (geral)\'],[\'arma_fogo\',\'Arma de fogo\'],[\'arma_branca\',\'Arma branca\'],\n   [\'fogo\',\'Fogo / Incêndio\'],[\'intruso\',\'Intrusão\'],[\'movimento\',\'Movimento\'],\n   [\'pessoa\',\'Pessoa\'],[\'aglomeracao\',\'Aglomeração\'],[\'placa\',\'Placa (veículo)\']\n ];\n var H=function(s){return String(s==null?\'\':s);};\n function esc(s){var d=document.createElement(\'div\'); d.textContent=H(s); return d.innerHTML;}\n function api(m,p,b){return fetch(p,{method:m,headers:{\'Authorization\':\'Bearer \'+TOK,\'Content-Type\':\'application/json\'},body:b?JSON.stringify(b):undefined})\n   .then(function(r){ if(!r.ok)return r.text().then(function(t){throw new Error(\'HTTP \'+r.status+\' \'+t.slice(0,80));}); return r.status===204?null:r.json(); });}\n function cid(o){return o&&(o.id||o._id||o.ID)||\'\';}\n\n // ---- estado ----\n var cams=[], cfgByCam={}, selected={}, launcher=null, modal=null, _lastFocus=null, _bodyOv=\'\', _tameDone=false, _tameTries=0;\n\n // ---- CSS ----\n function injectCSS(){ if(document.getElementById(\'cxax-css\'))return;\n   var st=document.createElement(\'style\'); st.id=\'cxax-css\';\n   st.textContent=[\n   \'#cxax-btn{position:fixed;left:16px;bottom:16px;z-index:9997;background:#f97316;color:#1a1205;font:700 13.5px system-ui,-apple-system,sans-serif;border:none;border-radius:12px;padding:12px 18px;cursor:pointer;box-shadow:0 10px 34px rgba(249,115,22,.35);display:none;align-items:center;gap:8px}\',\n   \'#cxax-btn:hover{background:#fb8b3a;transform:translateY(-1px)}\',\n   \'#cxax-btn.pulse{animation:cxaxpulse 1.6s ease-out 3}\',\n   \'@keyframes cxaxpulse{0%{box-shadow:0 0 0 0 rgba(249,115,22,.5),0 10px 34px rgba(249,115,22,.35)}70%{box-shadow:0 0 0 14px rgba(249,115,22,0),0 10px 34px rgba(249,115,22,.35)}100%{box-shadow:0 0 0 0 rgba(249,115,22,0),0 10px 34px rgba(249,115,22,.35)}}\',\n   /* dom o paredão de nomes: mira o 2º <p> dentro do box amarelo do aviso (classes reais do React) */\n   \'div[class*="yellow-500/10"]>p:last-child{max-height:5.4em;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;line-height:1.55;margin-top:8px;padding-top:8px;border-top:1px solid rgba(234,179,8,.18)}\',\n   \'div[class*="yellow-500/10"]{max-height:none}\',\n   \'#cxax-ov{position:fixed;inset:0;z-index:10000;background:rgba(3,5,8,.66);display:none;align-items:flex-start;justify-content:center;padding:3vh 12px;overflow:auto;overscroll-behavior:contain;font:14px system-ui,-apple-system,sans-serif}\',\n   \'#cxax-ov.open{display:flex}\',\n   \'#cxax-md{background:#12151b;color:#f2f4f6;border:1px solid #262d38;border-radius:16px;width:100%;max-width:940px;box-shadow:0 20px 70px rgba(0,0,0,.6);overflow:hidden}\',\n   \'#cxax-md .hd{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 20px;border-bottom:1px solid #262d38;background:linear-gradient(180deg,rgba(249,115,22,.10),transparent)}\',\n   \'#cxax-md .hd h2{margin:0;font-size:17px;font-weight:700}\',\n   \'#cxax-md .hd .x{background:none;border:none;color:#8b96a6;font-size:22px;cursor:pointer;line-height:1;padding:2px 6px}\',\n   \'#cxax-md .body{display:grid;grid-template-columns:1.35fr 1fr;gap:0}\',\n   \'#cxax-left{border-right:1px solid #262d38;min-width:0;display:flex;flex-direction:column;max-height:74vh}\',\n   \'#cxax-right{padding:16px 18px;display:flex;flex-direction:column;gap:12px;max-height:74vh;overflow:auto;overscroll-behavior:contain}\',\n   \'#cxax-tools{padding:14px 16px 8px;display:flex;flex-direction:column;gap:9px}\',\n   \'#cxax-srch{width:100%;background:#1c2129;border:1px solid #2a323d;border-radius:9px;color:#f2f4f6;padding:10px 12px;font-size:14px}\',\n   \'#cxax-srch:focus{outline:none;border-color:#f97316}\',\n   \'.cxax-chips{display:flex;gap:6px;flex-wrap:wrap}\',\n   \'.cxax-chip{font-size:12px;padding:5px 11px;border-radius:999px;border:1px solid #2a323d;background:#171b22;color:#8b96a6;cursor:pointer;user-select:none}\',\n   \'.cxax-chip.on{background:rgba(249,115,22,.16);border-color:#f97316;color:#ffd6b0}\',\n   \'#cxax-list{flex:1;overflow:auto;padding:4px 8px 12px}\',\n   \'.cxax-row{display:flex;align-items:center;gap:10px;padding:8px 8px;border-radius:8px;cursor:pointer}\',\n   \'.cxax-row:hover{background:#171b22}\',\n   \'.cxax-row .nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\',\n   \'.cxax-row .cli{color:#8b96a6;font-size:12px}\',\n   \'.cxax-badge{font-size:10px;padding:2px 7px;border-radius:999px;border:1px solid #2a323d;color:#8b96a6;white-space:nowrap}\',\n   \'.cxax-badge.ok{color:#34d399;border-color:rgba(52,211,153,.4)}\',\n   \'#cxax-selhd{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 16px;border-top:1px solid #262d38;border-bottom:1px solid #262d38;background:#141821;font-size:12px;color:#8b96a6}\',\n   \'#cxax-selhd a{color:#f97316;cursor:pointer;text-decoration:none}\',\n   \'.cxax-ana{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:8px;cursor:pointer;font-size:13px}\',\n   \'.cxax-ana:hover{background:#171b22}\',\n   \'.cxax-sec{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8b96a6;font-family:ui-monospace,Menlo,monospace}\',\n   \'#cxax-apply{background:#f97316;color:#1a1205;border:none;border-radius:10px;padding:12px 14px;font-weight:700;font-size:14px;cursor:pointer}\',\n   \'#cxax-apply:disabled{opacity:.5;cursor:not-allowed}\',\n   \'#cxax-apply:hover:not(:disabled){background:#fb8b3a}\',\n   \'#cxax-msg{font-size:13px;min-height:18px}\',\n   \'input[type=checkbox].cxax-cb{width:16px;height:16px;accent-color:#f97316;flex:none}\',\n   \'.cxax-vbtn{flex:none;background:#1c2129;border:1px solid #2a323d;color:#f97316;border-radius:8px;width:30px;height:30px;font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1}\',\n   \'.cxax-vbtn:hover{background:#f97316;color:#1a1205;border-color:#f97316}\',\n   \'#cxax-view{position:fixed;inset:0;z-index:10001;background:rgba(3,5,8,.82);display:none;align-items:center;justify-content:center;padding:3vh 12px}\',\n   \'#cxax-view.open{display:flex}\',\n   \'.cxax-vcard{background:#0d0f14;border:1px solid #262d38;border-radius:14px;width:100%;max-width:960px;overflow:hidden;box-shadow:0 20px 70px rgba(0,0,0,.6)}\',\n   \'.cxax-vhd{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;border-bottom:1px solid #262d38;color:#f2f4f6}\',\n   \'.cxax-vx{background:none;border:none;color:#8b96a6;font-size:22px;cursor:pointer;line-height:1;padding:0 6px}\',\n   \'.cxax-vbody{position:relative;background:#000;width:100%;aspect-ratio:16/9}\',\n   \'.cxax-vbody iframe{position:absolute;inset:0;width:100%;height:100%;border:0}\',\n   \'.cxax-vempty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#8b96a6;font-size:13px;padding:20px;text-align:center}\',\n   \'@media(max-width:760px){#cxax-md .body{grid-template-columns:1fr}#cxax-left{border-right:none;border-bottom:1px solid #262d38;max-height:46vh}}\'\n   ].join(\'\\n\');\n   document.head.appendChild(st);\n }\n\n // ---- tame do paredão de aviso (CSS + nota) ----\n // o paredão de nomes é domado por CSS preciso (ver injectCSS), não mais por heurística de DOM.\n\n // ---- dados ----\n function load(){\n   return Promise.all([ api(\'GET\',\'/api/entities/Camera\'), api(\'GET\',\'/api/entities/ConfigAnalitico\') ])\n    .then(function(res){\n      cams=(res[0]||[]).filter(function(o){return cid(o);});\n      cams.sort(function(a,b){return H(a.nome).localeCompare(H(b.nome),\'pt\');});\n      cfgByCam={}; (res[1]||[]).forEach(function(c){ if(c&&c.camera_id) cfgByCam[c.camera_id]={eid:cid(c),cfg:c}; });\n    });\n }\n\n // ---- render lista ----\n function curFilter(){ return (modal.querySelector(\'.cxax-chip.on\')||{}).getAttribute?modal.querySelector(\'.cxax-chip.on\').getAttribute(\'data-f\'):\'all\'; }\n function renderList(){\n   var q=(modal.querySelector(\'#cxax-srch\').value||\'\').trim().toLowerCase();\n   var f=curFilter();\n   var host=modal.querySelector(\'#cxax-list\'); host.innerHTML=\'\';\n   var shown=0;\n   cams.forEach(function(o){\n     var id=cid(o); var has=!!cfgByCam[id];\n     if(f===\'no\'&&has)return; if(f===\'yes\'&&!has)return;\n     var hay=(H(o.nome)+\' \'+H(o.cliente_nome)).toLowerCase();\n     if(q&&hay.indexOf(q)<0)return;\n     shown++;\n     var row=document.createElement(\'div\'); row.className=\'cxax-row\';\n     var cb=document.createElement(\'input\'); cb.type=\'checkbox\'; cb.className=\'cxax-cb\'; cb.checked=!!selected[id];\n     cb.addEventListener(\'click\',function(e){e.stopPropagation(); selected[id]=cb.checked; updSel();});\n     row.addEventListener(\'click\',function(){ cb.checked=!cb.checked; selected[id]=cb.checked; updSel();});\n     var nm=document.createElement(\'div\'); nm.className=\'nm\';\n     nm.innerHTML=\'<span>\'+esc(o.nome||\'(sem nome)\')+\'</span>\'+(o.cliente_nome?\' <span class="cli">· \'+esc(o.cliente_nome)+\'</span>\':\'\');\n     var bd=document.createElement(\'span\'); bd.className=\'cxax-badge\'+(has?\' ok\':\'\'); bd.textContent=has?\'configurada\':\'sem config\';\n     var vb=document.createElement(\'button\'); vb.className=\'cxax-vbtn\'; vb.title=\'Ver ao vivo\'; vb.textContent=\'▶\';\n     vb.addEventListener(\'click\',function(e){ e.stopPropagation(); viewCam(o); });\n     row.appendChild(cb); row.appendChild(nm); row.appendChild(bd); row.appendChild(vb); host.appendChild(row);\n   });\n   modal.querySelector(\'#cxax-count\').textContent=shown+\' câmera(s)\';\n   updSel();\n }\n function visibleIds(){\n   var q=(modal.querySelector(\'#cxax-srch\').value||\'\').trim().toLowerCase(), f=curFilter(), out=[];\n   cams.forEach(function(o){var id=cid(o),has=!!cfgByCam[id];\n     if(f===\'no\'&&has)return; if(f===\'yes\'&&!has)return;\n     var hay=(H(o.nome)+\' \'+H(o.cliente_nome)).toLowerCase(); if(q&&hay.indexOf(q)<0)return; out.push(id);});\n   return out;\n }\n function updSel(){\n   var n=Object.keys(selected).filter(function(k){return selected[k];}).length;\n   modal.querySelector(\'#cxax-seln\').textContent=n;\n   var ap=modal.querySelector(\'#cxax-apply\'); ap.disabled=(n===0);\n   ap.textContent = n? (\'Aplicar a \'+n+\' câmera(s)\') : \'Selecione câmeras\';\n }\n function chosenAnaliticos(){ return ANALITICOS.map(function(a){return a[0];}).filter(function(k){var el=modal.querySelector(\'#cxax-ana-\'+k); return el&&el.checked;}); }\n\n // ---- aplicar em massa ----\n function apply(){\n   var ids=Object.keys(selected).filter(function(k){return selected[k];});\n   var ana=chosenAnaliticos();\n   var ativo=modal.querySelector(\'#cxax-ativo\').checked;\n   var msg=modal.querySelector(\'#cxax-msg\');\n   if(!ids.length){return;}\n   if(!ana.length){ msg.style.color=\'#f87171\'; msg.textContent=\'Escolha ao menos 1 analítico.\'; return; }\n   var overwrite=modal.querySelector(\'#cxax-ow\').checked;\n   var ap=modal.querySelector(\'#cxax-apply\'); ap.disabled=true;\n   var ok=0, skip=0, err=0, done=0;\n   msg.style.color=\'#8b96a6\'; msg.textContent=\'Aplicando... 0/\'+ids.length;\n   // sequencial em lotes pequenos p/ nao afogar o backend\n   var i=0;\n   function next(){\n     if(i>=ids.length){ finish(); return; }\n     var id=ids[i++]; var cam=cams.filter(function(o){return cid(o)===id;})[0]||{};\n     var existing=cfgByCam[id];\n     var p;\n     if(existing && !overwrite){ skip++; done++; msg.textContent=\'Aplicando... \'+done+\'/\'+ids.length; return next(); }\n     if(existing){\n       var body=Object.assign({},existing.cfg,{analiticos_padrao:ana,ativo:ativo});\n       p=api(\'PUT\',\'/api/entities/ConfigAnalitico/\'+existing.eid, body);\n     } else {\n       p=api(\'POST\',\'/api/entities/ConfigAnalitico\', {camera_id:id, camera_nome:H(cam.nome), ativo:ativo, analiticos_padrao:ana, horarios:[], zonas_intrusao:[]});\n     }\n     p.then(function(){ok++;}).catch(function(){err++;}).then(function(){ done++; msg.textContent=\'Aplicando... \'+done+\'/\'+ids.length; next(); });\n   }\n   function finish(){\n     msg.style.color = err? \'#f8b74d\' : \'#34d399\';\n     msg.textContent=\'Pronto: \'+ok+\' aplicada(s)\'+(skip?\', \'+skip+\' já tinham (mantidas)\':\'\')+(err?\', \'+err+\' com erro\':\'\')+\'. Vale em ~2 min.\';\n     selected={};\n     load().then(function(){ renderList(); ap.disabled=false; });\n   }\n   next();\n }\n\n // ---- modal ----\n function buildModal(){\n   injectCSS();\n   var ov=document.createElement(\'div\'); ov.id=\'cxax-ov\';\n   var anaHtml=ANALITICOS.map(function(a){return \'<label class="cxax-ana"><input type="checkbox" class="cxax-cb" id="cxax-ana-\'+a[0]+\'"\'+((a[0]===\'arma\'||a[0]===\'fogo\'||a[0]===\'intruso\'||a[0]===\'movimento\')?\' checked\':\'\')+\'><span>\'+esc(a[1])+\'</span></label>\';}).join(\'\');\n   ov.innerHTML=\'\'\n   +\'<div id="cxax-md" role="dialog" aria-modal="true" aria-label="Configuração em massa de analíticos">\'\n   +\' <div class="hd"><h2>⚙️ Configurar Analíticos — em massa</h2><button class="x" id="cxax-x" aria-label="Fechar">×</button></div>\'\n   +\' <div class="body">\'\n   +\'  <div id="cxax-left">\'\n   +\'   <div id="cxax-tools">\'\n   +\'     <input id="cxax-srch" placeholder="Buscar câmera por nome ou cliente...">\'\n   +\'     <div class="cxax-chips"><span class="cxax-chip on" data-f="all">Todas</span><span class="cxax-chip" data-f="no">Sem config</span><span class="cxax-chip" data-f="yes">Com config</span></div>\'\n   +\'   </div>\'\n   +\'   <div id="cxax-selhd"><span id="cxax-count">—</span><span><a id="cxax-selall">Selecionar visíveis</a> · <a id="cxax-selnone">Limpar</a></span></div>\'\n   +\'   <div id="cxax-list"></div>\'\n   +\'  </div>\'\n   +\'  <div id="cxax-right">\'\n   +\'   <div class="cxax-sec">Analíticos a aplicar</div>\'\n   +\'   <div>\'+anaHtml+\'</div>\'\n   +\'   <label class="cxax-ana"><input type="checkbox" class="cxax-cb" id="cxax-ativo" checked><span>Config. ativa (a câmera passa a rodar IA)</span></label>\'\n   +\'   <label class="cxax-ana"><input type="checkbox" class="cxax-cb" id="cxax-ow"><span>Sobrescrever quem já tem config</span></label>\'\n   +\'   <div style="flex:1"></div>\'\n   +\'   <div id="cxax-msg"></div>\'\n   +\'   <button id="cxax-apply" disabled>Selecione câmeras</button>\'\n   +\'   <div style="font-size:11px;color:#8b96a6">Cria/atualiza ConfigAnalitico. Sem horários = vale o dia todo. Zonas continuam pela tela de Zonas.</div>\'\n   +\'  </div>\'\n   +\' </div>\'\n   +\'</div>\';\n   document.body.appendChild(ov); modal=ov;\n   ov.addEventListener(\'click\',function(e){ if(e.target===ov)close(); });\n   ov.querySelector(\'#cxax-x\').addEventListener(\'click\',close);\n   ov.querySelector(\'#cxax-srch\').addEventListener(\'input\',renderList);\n   ov.querySelectorAll(\'.cxax-chip\').forEach(function(ch){ch.addEventListener(\'click\',function(){ ov.querySelectorAll(\'.cxax-chip\').forEach(function(c){c.classList.remove(\'on\');}); ch.classList.add(\'on\'); renderList();});});\n   ov.querySelector(\'#cxax-selall\').addEventListener(\'click\',function(){ visibleIds().forEach(function(id){selected[id]=true;}); renderList();});\n   ov.querySelector(\'#cxax-selnone\').addEventListener(\'click\',function(){ selected={}; renderList();});\n   ov.querySelector(\'#cxax-apply\').addEventListener(\'click\',apply);\n   document.addEventListener(\'keydown\',function(e){ if(e.key===\'Escape\'&&modal&&modal.classList.contains(\'open\'))close(); });\n   ov.addEventListener(\'keydown\',function(e){ if(e.key!==\'Tab\')return;\n     var fs=[].filter.call(ov.querySelectorAll(\'input,button,a[href],[tabindex]\'),function(el){return !el.disabled && el.offsetParent!==null;});\n     if(!fs.length)return; var first=fs[0], last=fs[fs.length-1];\n     if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }\n     else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }\n   });\n }\n function open(){ if(!modal)buildModal(); _lastFocus=document.activeElement; _bodyOv=document.body.style.overflow; document.body.style.overflow=\'hidden\'; modal.classList.add(\'open\');\n   var msg=modal.querySelector(\'#cxax-msg\'); msg.style.color=\'#8b96a6\'; msg.textContent=\'Carregando câmeras...\';\n   setTimeout(function(){ try{ modal.querySelector(\'#cxax-srch\').focus(); }catch(e){} }, 40);\n   load().then(function(){ msg.textContent=\'\'; renderList(); }).catch(function(e){ msg.style.color=\'#f87171\'; msg.textContent=\'Erro: \'+e.message; });\n }\n function close(){ if(modal)modal.classList.remove(\'open\'); document.body.style.overflow=_bodyOv||\'\'; try{ if(_lastFocus&&_lastFocus.focus)_lastFocus.focus(); }catch(e){} }\n\n // ---- ver ao vivo (iframe do embed do analitico) ----\n function closeView(){ var ov=document.getElementById(\'cxax-view\'); if(ov){ ov.classList.remove(\'open\'); ov.innerHTML=\'\'; } }\n function viewCam(o){\n   var em=(o.embed_url||\'\').trim();\n   var ov=document.getElementById(\'cxax-view\');\n   if(!ov){ ov=document.createElement(\'div\'); ov.id=\'cxax-view\'; document.body.appendChild(ov);\n     ov.addEventListener(\'click\',function(ev){ if(ev.target===ov)closeView(); });\n     document.addEventListener(\'keydown\',function(ev){ if(ev.key===\'Escape\'&&ov.classList.contains(\'open\'))closeView(); }); }\n   var inner = em ? (\'<iframe src="\'+esc(em)+\'" allow="autoplay; fullscreen" allowfullscreen frameborder="0"></iframe>\')\n                  : \'<div class="cxax-vempty">Esta câmera não tem link de player (embed) cadastrado — não dá pra ver ao vivo por aqui.</div>\';\n   ov.innerHTML=\'<div class="cxax-vcard"><div class="cxax-vhd"><b>\'+esc(o.nome||\'Câmera\')+\' — ao vivo</b><button class="cxax-vx" id="cxax-vx" aria-label="Fechar">×</button></div><div class="cxax-vbody">\'+inner+\'</div></div>\';\n   var xb=ov.querySelector(\'#cxax-vx\'); if(xb)xb.addEventListener(\'click\',closeView);\n   ov.classList.add(\'open\');\n }\n\n // ---- launcher visível só na página certa ----\n function onPage(){\n   try{ if(PATHS.some(function(p){return location.pathname.toLowerCase().indexOf(p)>=0;})) return true; }catch(e){}\n   // fallback robusto por CONTEÚDO: o box amarelo de aviso específico desta página\n   try{ var w=document.querySelector(\'div[class*="yellow-500/10"]\'); if(w && (w.textContent||\'\').indexOf(\'sem configura\')>=0) return true; }catch(e){}\n   return false;\n }\n function ensureLauncher(){\n   // Botão flutuante removido a pedido (estava no lugar errado). Mantém só o CSS que dom o paredão do aviso.\n   injectCSS();\n   if(launcher){ launcher.remove(); launcher=null; }   // limpa se algum ficou de deploy anterior\n }\n // observa navegação SPA + DOM\n var _ps=history.pushState, _rs=history.replaceState;\n history.pushState=function(){var r=_ps.apply(this,arguments); setTimeout(ensureLauncher,50); return r;};\n history.replaceState=function(){var r=_rs.apply(this,arguments); setTimeout(ensureLauncher,50); return r;};\n window.addEventListener(\'popstate\',function(){setTimeout(ensureLauncher,50);});\n setInterval(ensureLauncher, 1200);\n setTimeout(ensureLauncher, 800);\n})();</script>'

_ANXREDIR_JS = "<script>/* corexia-anxredir */(function(){\n function chk(){ try{\n   if(location.pathname.toLowerCase().indexOf('/config-analiticos')>=0 && !/legacy=1/.test(location.search)){\n     location.replace('/comercial/analiticos'); return true;\n   }\n }catch(e){} return false; }\n var _ps=history.pushState,_rs=history.replaceState;\n history.pushState=function(){var r=_ps.apply(this,arguments); setTimeout(chk,0); return r;};\n history.replaceState=function(){var r=_rs.apply(this,arguments); setTimeout(chk,0); return r;};\n window.addEventListener('popstate',function(){setTimeout(chk,0);});\n setInterval(chk,300);\n try{ if(navigator.serviceWorker){ navigator.serviceWorker.getRegistrations().then(function(rs){rs.forEach(function(r){try{r.update();}catch(e){}});}); } }catch(e){}\n chk();\n})();</script>"

@app.get("/api/infra")
async def api_infra(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    if u["role"] != "admin":
        return _forbidden()
    if _infra_cache["data"] and time.time() - _infra_cache["t"] < 5:
        return _infra_cache["data"]   # nvidia-smi custa ~100ms; 5s de cache segura o polling
    async with _infra_lock:            # single-flight: so 1 coleta por vez (anti thundering-herd)
        if _infra_cache["data"] and time.time() - _infra_cache["t"] < 5:
            return _infra_cache["data"]
        loop = asyncio.get_event_loop()
        xeon = await loop.run_in_executor(None, _infra_xeon)
        try:   # topo-3nos: se o no de IA reporta (ia_health.json fresco), usa as metricas DELE (com GPU)
            _hp = os.path.join(HERE, "ia_health.json")
            if time.time() - os.stat(_hp).st_mtime < 180:
                with open(_hp) as _f: _iah = json.load(_f)
                if isinstance(_iah.get("xeon"), dict): xeon = _iah["xeon"]
        except Exception:
            pass   # local: seguro no pool global
        storage = await _infra_storage()                       # NFS: pool dedicado + timeout
        data = {"ts": int(time.time()), "xeon": xeon, "storage": storage, "vps_net": _vps_net()}
        # atualiza t SEMPRE (mesmo se storage falhou) — throttle: nao re-tenta a cada 10s
        _infra_cache.update(t=time.time(), data=data)
        return data


# ==================== CHAMADOS / SUPORTE (tickets de provedor e cliente) ====================
# Provedor e cliente final ABREM chamado (suporte/financeiro/geral). Admin (Corexia) ve TODOS
# e responde/muda status. Escopo reusa _scope_ok: provedor ve os seus + os dos clientes dele;
# cliente ve so os seus. Armazenado na tabela entities (entity='Chamado').
CHAMADO_TIPOS = {"suporte", "financeiro", "geral"}
CHAMADO_STATUS = {"aberto", "em_andamento", "resolvido", "fechado"}

@app.post("/api/chamados")
async def chamado_criar(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    if u["role"] not in ("cliente", "provedor"):
        return _forbidden("apenas cliente ou provedor podem abrir chamado")
    b = await req.json()
    tipo = (b.get("tipo") or "suporte").lower()
    if tipo not in CHAMADO_TIPOS:
        tipo = "geral"
    desc = (b.get("descricao") or "").strip()
    if len(desc) < 3:
        return JSONResponse({"error": "descreva a solicitacao"}, status_code=400)
    data = {"tipo": tipo, "descricao": desc[:2000], "status": "aberto",
            "aberto_por_id": u["id"], "aberto_por_email": u["email"],
            "aberto_por_nome": u["full_name"], "aberto_por_role": u["role"],
            "cliente_id": "", "cliente_nome": "", "provedor_id": "", "provedor_nome": "",
            "telefone": (b.get("telefone") or "").strip(), "resposta": ""}
    if u["role"] == "cliente":
        cli = _get_entity("Cliente", u["cliente_id"]) or {}
        data["cliente_id"] = u["cliente_id"]
        data["cliente_nome"] = cli.get("nome", "")
        data["provedor_id"] = cli.get("provedor_id", "")
        data["telefone"] = data["telefone"] or cli.get("telefone", "") or cli.get("whatsapp", "")
        if data["provedor_id"]:
            data["provedor_nome"] = (_get_entity("Provedor", data["provedor_id"]) or {}).get("nome", "")
    else:  # provedor
        prov = _get_entity("Provedor", u["provedor_id"]) or {}
        data["provedor_id"] = u["provedor_id"]
        data["provedor_nome"] = prov.get("nome", "")
        data["telefone"] = data["telefone"] or prov.get("telefone", "") or prov.get("whatsapp", "")
    eid = secrets.token_hex(12); now = _now_iso()
    c = db(); c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                        ("Chamado", eid, json.dumps(data), now, now)); c.commit(); c.close()
    # PUSH pra quem atende: admins + users do provedor responsavel (push nunca derruba a rota)
    try:
        cc = db(); ids = set()
        for r in cc.execute("SELECT id FROM users WHERE role='admin' AND status='ativo'"):
            ids.add(r["id"])
        if data["provedor_id"]:
            for r in cc.execute("SELECT id FROM users WHERE provedor_id=? AND status='ativo'",
                                (data["provedor_id"],)):
                ids.add(r["id"])
        cc.close()
        ids.discard(u["id"])   # quem abriu nao precisa ser avisado do proprio chamado
        n_push = _send_push(list(ids), f"🎫 Novo chamado ({tipo})",
                            f"{data['aberto_por_nome']}: {desc[:100]}",
                            url="/suporte", tag=f"chamado-{eid}")
        if n_push:
            print(f"[push] novo chamado -> {n_push} dispositivo(s)")
    except Exception as e:
        print("[push-chamado] erro:", e)
    # WhatsApp (Z-API) alem do push web -- fluxo de chamados
    try:
        import comercial as _com
        if data.get("aberto_por_role") == "provedor":
            _txt = ("🎫 CHAMADO DE PROVEDOR - %s\n\nProvedor: %s\nContato: %s\n\n%s"
                    % (tipo.upper(), data.get("provedor_nome", "-"), data.get("telefone", "-"), desc[:600]))
            for _n in _com._plantao_corexia_nums():
                try:
                    _com._zapi_send(_n, _txt)
                except Exception:
                    pass
        elif data.get("aberto_por_role") == "cliente" and data.get("provedor_id"):
            _pid = data["provedor_id"]
            if _com._prov_tem_zapi(_pid):
                _zi, _zt, _zc = _com._zapi_do_provedor(_pid)
                _alvos = _com._plantao_prov_nums(_pid) or ([data.get("telefone")] if data.get("telefone") else [])
                _txt = ("🎫 NOVO CHAMADO - %s\n\nCliente: %s\nContato: %s\n\n%s"
                        % (tipo.upper(), data.get("cliente_nome", "-"), data.get("telefone", "-"), desc[:600]))
                for _n in _alvos:
                    try:
                        _com._zapi_send(_n, _txt, _zi, _zt, _zc)
                    except Exception:
                        pass
    except Exception as _e:
        print("[wa-chamado-criar] erro:", _e)
    o = dict(data); o.update(id=eid, created_date=now, updated_date=now)
    return _obj_ts_local(o)

@app.get("/api/chamados")
async def chamado_listar(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    status = req.query_params.get("status", "")
    c = db(); rows = c.execute("SELECT * FROM entities WHERE entity='Chamado' ORDER BY created_date DESC").fetchall(); c.close()
    out = []
    for r in rows:
        o = _row_to_obj(r)
        if _scope_ok("Chamado", o, u) and (not status or o.get("status") == status):
            out.append(_obj_ts_local(o))
    return out

@app.put("/api/chamados/{cid}")
async def chamado_update(cid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db(); r = c.execute("SELECT * FROM entities WHERE entity='Chamado' AND id=?", (cid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    o = _row_to_obj(r)
    if not _scope_ok("Chamado", o, u):
        c.close(); return _forbidden()
    b = await req.json()
    cur = json.loads(r["data"])
    # autor = quem abriu (por id; fallback por tenant p/ chamados antigos sem aberto_por_id)
    is_author = cur.get("aberto_por_id") == u["id"] or (
        not cur.get("aberto_por_id")
        and ((u["role"] == "cliente" and cur.get("cliente_id") == u["cliente_id"])
             or (u["role"] == "provedor" and cur.get("aberto_por_role") == "provedor"
                 and cur.get("provedor_id") == u["provedor_id"])))
    respondeu = False
    if u["role"] == "admin":
        if b.get("status") in CHAMADO_STATUS:
            cur["status"] = b["status"]
        if "resposta" in b:
            cur["resposta"] = (b.get("resposta") or "")[:2000]
            cur["respondido_por"] = "Corexia"
            cur["respondido_em"] = _now_iso()
            respondeu = True
    elif (u["role"] == "provedor" and cur.get("aberto_por_role") == "cliente"
          and cur.get("provedor_id") == u["provedor_id"]):
        # provedor atende os chamados abertos pelos clientes DELE
        if b.get("status") in CHAMADO_STATUS:
            cur["status"] = b["status"]
        if "resposta" in b:
            cur["resposta"] = (b.get("resposta") or "")[:2000]
            cur["respondido_por"] = u["full_name"]
            cur["respondido_em"] = _now_iso()
            respondeu = True
    elif is_author:
        # autor so reabre/fecha o proprio chamado
        if b.get("status") in ("aberto", "fechado"):
            cur["status"] = b["status"]
    else:
        c.close(); return _forbidden()
    now = _now_iso()
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Chamado' AND id=?",
              (json.dumps(cur), now, cid)); c.commit(); c.close()
    # PUSH pro autor quando gravou resposta (push nunca derruba a rota)
    if respondeu and cur.get("aberto_por_id"):
        try:
            alvo = "/portal/suporte" if cur.get("aberto_por_role") == "cliente" else "/suporte"
            _send_push([cur["aberto_por_id"]], "💬 Chamado respondido",
                       (cur.get("resposta") or "")[:120] or "Seu chamado foi atualizado.",
                       url=alvo, tag=f"chamado-{cid}")
        except Exception as e:
            print("[push-chamado] erro:", e)
    # WhatsApp (Z-API) pro autor -- resposta / conclusao / reabertura
    try:
        import comercial as _com
        _new_status = cur.get("status")
        try:
            _old_status = json.loads(r["data"]).get("status")
        except Exception:
            _old_status = None
        _role = cur.get("aberto_por_role")
        _tel = (cur.get("telefone") or "").strip()
        _acts = []
        if respondeu:
            _acts.append(("resp", (cur.get("resposta") or "")[:600]))
        if (not is_author) and _new_status != _old_status:
            if _new_status == "resolvido":
                _acts.append(("fim", ""))
            elif _new_status == "aberto" and _old_status in ("resolvido", "fechado"):
                _acts.append(("reab", ""))
        if _tel and _acts and _role == "provedor":
            for _k, _v in _acts:
                if _k == "resp":
                    _t = "🦅 COREXIA - Resposta do suporte\n\nOla %s!\n\n%s" % (cur.get("aberto_por_nome", ""), _v)
                elif _k == "fim":
                    _t = "🦅 COREXIA - Chamado concluido\n\nSeu chamado foi marcado como concluido. Precisando, abra um novo."
                else:
                    _t = "🦅 COREXIA - Chamado reaberto\n\nSeu chamado foi reaberto e esta sendo analisado."
                try:
                    _com._zapi_send(_tel, _t)
                except Exception:
                    pass
        elif _tel and _acts and _role == "cliente":
            _pid = cur.get("provedor_id", "")
            if _com._prov_tem_zapi(_pid):
                _zi, _zt, _zc = _com._zapi_do_provedor(_pid)
                for _k, _v in _acts:
                    if _k == "resp":
                        _t = "Ola %s! Sua solicitacao foi respondida:\n\n%s" % (cur.get("cliente_nome", "") or "", _v)
                    elif _k == "fim":
                        _t = "Seu chamado foi marcado como concluido. Precisando, abra um novo pelo app."
                    else:
                        _t = "Seu chamado foi reaberto e esta sendo analisado novamente."
                    try:
                        _com._zapi_send(_tel, _t, _zi, _zt, _zc)
                    except Exception:
                        pass
    except Exception as _e:
        print("[wa-chamado-upd] erro:", _e)
    o = dict(cur); o.update(id=cid, updated_date=now); return _obj_ts_local(o)


def _mos_esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_WATCH_HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Camera</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:#000;overflow:hidden}
#wrap{position:fixed;inset:0;overflow:hidden;background:#000;touch-action:none;cursor:grab}
#wrap.z{cursor:grab}#wrap.z.drag{cursor:grabbing}
#v{width:100%;height:100%;object-fit:contain;transform-origin:0 0;will-change:transform;background:#000;display:block}
#ctrl{position:fixed;right:10px;bottom:10px;display:flex;gap:6px;z-index:10}
#ctrl button{width:42px;height:42px;border:0;border-radius:9px;background:rgba(20,20,20,.62);color:#fff;font-size:19px;font-weight:700;cursor:pointer;-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;line-height:1}
#ctrl button:active{background:rgba(249,115,22,.92);color:#111}
#zlbl{position:fixed;left:10px;bottom:12px;color:rgba(255,255,255,.75);font:600 12px system-ui,sans-serif;z-index:10;pointer-events:none;background:rgba(0,0,0,.35);padding:3px 8px;border-radius:8px}
</style></head>
<body><div id="wrap"><video id="v" playsinline autoplay muted></video></div>
<div id="zlbl">1.0x</div>
<div id="ctrl">
 <button id="zin" title="Aproximar">+</button>
 <button id="zout" title="Afastar">&minus;</button>
 <button id="zrst" title="Redefinir zoom">&#10226;</button>
 <button id="mut" title="Som">&#128264;</button>
 <button id="fs" title="Tela cheia">&#9974;</button>
</div>
<script src="/assets/hls-zoom.min.js"></script>
<script>
var KEY="__KEY__", SRC="__SRC__";
var v=document.getElementById("v"), wrap=document.getElementById("wrap"), zlbl=document.getElementById("zlbl");
/* ---- HLS com auto-reconexao ---- */
function tryplay(){ try{ var p=v.play(); if(p&&p.catch)p.catch(function(){}); }catch(e){} }
function start(){
 try{
  if(v.canPlayType("application/vnd.apple.mpegurl")){ v.src=SRC; }
  else if(window.Hls && Hls.isSupported()){
   var h=new Hls({liveSyncDurationCount:2,maxBufferLength:8,manifestLoadingMaxRetry:8});
   h.loadSource(SRC); h.attachMedia(v);
   h.on(Hls.Events.ERROR,function(_e,d){ if(d&&d.fatal){ try{h.destroy()}catch(x){} setTimeout(start,2500);} });
  } else { v.src=SRC; }
 }catch(e){ setTimeout(start,2500); }
 tryplay();
}
v.addEventListener("loadedmetadata",tryplay);
v.addEventListener("canplay",tryplay);
document.addEventListener("click",function(){ if(v.paused)tryplay(); });
start();
/* ---- ZOOM / PAN ---- */
var scale=1,tx=0,ty=0,MIN=1,MAX=6;
function upd(){ if(scale<=1.001){scale=1;tx=0;ty=0;wrap.classList.remove("z");} else {wrap.classList.add("z");}
 v.style.transform="translate("+tx+"px,"+ty+"px) scale("+scale+")"; zlbl.textContent=scale.toFixed(1)+"x"; }
function clamp(){ var w=wrap.clientWidth,h=wrap.clientHeight,mx=(scale-1)*w,my=(scale-1)*h;
 if(tx>0)tx=0; if(ty>0)ty=0; if(tx<-mx)tx=-mx; if(ty<-my)ty=-my; }
function zoomAt(cx,cy,ns){ ns=Math.max(MIN,Math.min(MAX,ns)); var r=wrap.getBoundingClientRect(),px=cx-r.left,py=cy-r.top;
 tx=px-(px-tx)*(ns/scale); ty=py-(py-ty)*(ns/scale); scale=ns; clamp(); upd(); }
wrap.addEventListener("wheel",function(e){ e.preventDefault(); zoomAt(e.clientX,e.clientY,scale*(e.deltaY<0?1.2:1/1.2)); },{passive:false});
document.getElementById("zin").onclick=function(){ zoomAt(wrap.clientWidth/2,wrap.clientHeight/2,scale*1.4); };
document.getElementById("zout").onclick=function(){ zoomAt(wrap.clientWidth/2,wrap.clientHeight/2,scale/1.4); };
document.getElementById("zrst").onclick=function(){ scale=1;tx=0;ty=0;upd(); };
wrap.addEventListener("dblclick",function(e){ if(scale>1){scale=1;tx=0;ty=0;upd();} else zoomAt(e.clientX,e.clientY,2.5); });
/* mouse pan */
var drag=false,lx=0,ly=0;
wrap.addEventListener("mousedown",function(e){ if(scale<=1)return; drag=true;lx=e.clientX;ly=e.clientY;wrap.classList.add("drag"); });
window.addEventListener("mousemove",function(e){ if(!drag)return; tx+=e.clientX-lx;ty+=e.clientY-ly;lx=e.clientX;ly=e.clientY;clamp();upd(); });
window.addEventListener("mouseup",function(){ drag=false;wrap.classList.remove("drag"); });
/* touch: pinca + pan */
var P={},pd=0;
wrap.addEventListener("touchstart",function(e){ for(var i=0;i<e.changedTouches.length;i++){var t=e.changedTouches[i];P[t.identifier]={x:t.clientX,y:t.clientY};} pd=0; },{passive:false});
wrap.addEventListener("touchmove",function(e){ e.preventDefault(); var ids=Object.keys(P);
 if(e.touches.length>=2){ var a=e.touches[0],b=e.touches[1]; var nd=Math.hypot(b.clientX-a.clientX,b.clientY-a.clientY),cx=(a.clientX+b.clientX)/2,cy=(a.clientY+b.clientY)/2;
  if(pd>0)zoomAt(cx,cy,scale*(nd/pd)); pd=nd;
 } else if(e.touches.length==1 && scale>1){ var t=e.touches[0],id=t.identifier,p=P[id]||{x:t.clientX,y:t.clientY}; tx+=t.clientX-p.x;ty+=t.clientY-p.y;P[id]={x:t.clientX,y:t.clientY};clamp();upd(); }
},{passive:false});
wrap.addEventListener("touchend",function(e){ for(var i=0;i<e.changedTouches.length;i++){delete P[e.changedTouches[i].identifier];} pd=0; },{passive:false});
/* som + tela cheia */
document.getElementById("mut").onclick=function(){ v.muted=!v.muted; this.innerHTML=v.muted?"&#128264;":"&#128266;"; if(!v.muted){var p=v.play();if(p&&p.catch)p.catch(function(){});} };
document.getElementById("fs").onclick=function(){ try{ if(document.fullscreenElement){document.exitFullscreen();return;} if(wrap.requestFullscreen){wrap.requestFullscreen();return;} if(wrap.webkitRequestFullscreen){wrap.webkitRequestFullscreen();return;} if(v.webkitEnterFullscreen){v.webkitEnterFullscreen();return;} if(v.requestFullscreen){v.requestFullscreen();} }catch(e){} };
window.addEventListener("resize",function(){ clamp();upd(); });
</script></body></html>"""


@app.get("/watch/{key}")
def watch_player(key: str):
    if not re.match(r"^[a-zA-Z0-9]+$", key or ""):
        return JSONResponse({"error": "invalido"}, status_code=400)
    _mb = os.getenv("STREAM_EMBED_BASE", "https://media.grupocorexia.com.br/cam").rstrip("/")
    _src = "%s/%s/index.m3u8" % (_mb, key)
    return HTMLResponse(_WATCH_HTML.replace("__KEY__", key).replace("__SRC__", _src), headers={"Cache-Control": "no-cache"})


@app.get("/mosaico/{mid}")
def mosaico_viewer(mid: str, req: Request):
    _pg = "<body style='font-family:sans-serif;background:#0a0a0a;color:#fff;padding:40px'>%s</body>"
    u = current_user(req, allow_query_token=True)  # mosaico viewer via ?t=
    if not u:
        return HTMLResponse(_pg % "Faca login para ver o mosaico.", status_code=401)
    _tq = (req.query_params.get("t") or "").strip()
    _bk = "/meus-mosaicos" + ("?t=" + _tq if _tq else "")
    m = _get_entity("Mosaico", mid)
    if not m:
        return HTMLResponse(_pg % "Mosaico nao encontrado.", status_code=404)
    ok = False
    if u["role"] == "admin":
        ok = True
    elif u["role"] == "provedor" and m.get("provedor_id") == u.get("provedor_id"):
        ok = True
    elif u.get("user_type") == "subuser":
        ok = (mid in (u.get("allowed_mosaicos") or [])) and (not u.get("sub_blocked")) and (m.get("ativo", True) is not False)
    elif u["role"] == "cliente":
        ok = (m.get("cliente_id") == u.get("cliente_id")) and (m.get("ativo", True) is not False)
    if not ok:
        return HTMLResponse(_pg % "Sem acesso a este mosaico.", status_code=403)
    cams = (m.get("cameras") or [])[:4]
    cells = []
    for i in range(4):
        if i < len(cams):
            cam = _get_entity("Camera", cams[i]) or {}
            emb = cam.get("embed_url", "") or ""
            nome = _mos_esc(cam.get("nome", ""))
            if emb:
                cells.append('<div class="q"><iframe src="%s" allowfullscreen allow="autoplay; fullscreen"></iframe><span>%s</span></div>' % (_mos_esc(emb), nome))
            else:
                cells.append('<div class="q empty">sem stream publico<span>%s</span></div>' % nome)
        else:
            cells.append('<div class="q empty">-</div>')
    nome_m = _mos_esc(m.get("nome", "Mosaico"))
    cli_m = _mos_esc(m.get("cliente_nome", ""))
    html = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + nome_m + "</title>"
            "<style>*{box-sizing:border-box}body{margin:0;background:#0a0a0a;font-family:system-ui,sans-serif}"
            ".top{color:#fff;padding:9px 16px;font-size:15px;display:flex;justify-content:space-between;align-items:center}"
            ".top a{color:#111;background:#f97316;font-weight:700;text-decoration:none;font-size:13px;padding:6px 12px;border-radius:16px;margin-right:10px}"
            ".grid{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:4px;height:calc(100vh - 42px);padding:0 4px 4px}"
            ".q{position:relative;background:#000;border-radius:6px;overflow:hidden}.q iframe{width:100%;height:100%;border:0}"
            ".q span{position:absolute;left:8px;bottom:8px;background:rgba(0,0,0,.6);color:#fff;font-size:12px;padding:2px 8px;border-radius:5px}"
            ".q.empty{display:flex;align-items:center;justify-content:center;color:#555;font-size:13px}</style></head>"
            "<body><div class='top'><span><a href='" + _bk + "'>&larr; Voltar</a><b>" + nome_m + "</b></span><span style='color:#888'>" + cli_m + "</span></div>"
            "<div class='grid'>" + "".join(cells) + "</div></body></html>")
    return HTMLResponse(html)


_PORTAL_FATURAS_JS = r"""<script>/* corexia-portal-faturas */(function(){
 if(!localStorage.getItem('corexia_token'))return;
 function onPortal(){ return location.pathname.indexOf('/portal')===0; }
 function fixHrefs(){
  if(!onPortal())return;
  var as=document.querySelectorAll('a[href]');
  for(var i=0;i<as.length;i++){ var a=as[i]; var t=(a.textContent||'').replace(/\s+/g,' ').trim(); var h=a.getAttribute('href')||'';
   if(t==='Faturas' && h.indexOf('/portal/faturas')<0 && (h.indexOf('fatura')>=0||h.indexOf('comercial')>=0)){ a.setAttribute('href','/portal/faturas'); }
  }
 }
 document.addEventListener('click',function(e){
  if(!onPortal())return;
  var el=e.target;
  for(var i=0;i<6 && el;i++,el=el.parentElement){
   if(el.children && el.children.length>3)continue;
   var t=(el.textContent||'').replace(/\s+/g,' ').trim();
   if(t==='Faturas'){
    if(location.pathname==='/portal/faturas')return;
    e.preventDefault(); e.stopPropagation();
    window.location.assign('/portal/faturas');
    return;
   }
  }
 },true);
 fixHrefs(); try{new MutationObserver(fixHrefs).observe(document.body||document.documentElement,{childList:true,subtree:true});}catch(e){}
})();</script>"""


_PORTAL_MENU_JS = r"""<script>/* corexia-portal-menu */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 var IS_CLI=false;
 function onPortal(){ return location.pathname.indexOf('/portal')===0; }
 function findByText(words){ var ns=document.querySelectorAll('a,button,li,[role=menuitem]');
   for(var i=0;i<ns.length;i++){ var el=ns[i]; if(el.children.length>4)continue; var s=(el.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
     if(s.length<=16){ for(var j=0;j<words.length;j++){ if(s.indexOf(words[j])>=0) return el; } } } return null; }
 function relabel(node,txt){ var w=node.querySelectorAll('*'); for(var i=0;i<w.length;i++){ if(w[i].children.length===0 && (w[i].textContent||'').trim().length>1){ w[i].textContent=txt; return; } } node.textContent=txt; }
 function seticon(node,path){ try{ var sv=node.querySelector('svg'); if(sv){ sv.setAttribute('viewBox','0 0 24 24'); sv.setAttribute('fill','none'); sv.setAttribute('stroke','currentColor'); sv.setAttribute('stroke-width','2'); sv.setAttribute('stroke-linecap','round'); sv.setAttribute('stroke-linejoin','round'); sv.innerHTML=path; } }catch(e){} }
 function mkItem(id,ref,txt,icon,onclick){ var a=ref.cloneNode(true); a.id=id; a.removeAttribute('href'); a.style.cursor='pointer'; a.classList.remove('active'); relabel(a,txt); if(icon)seticon(a,icon); a.addEventListener('click',onclick,true); return a; }
 var MOS_ICON='<rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect>';
 var BUSCA_ICON='<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>';
 function goMos(e){ if(e){e.preventDefault();e.stopPropagation();} window.location.href='/meus-mosaicos?t='+encodeURIComponent(t); }
 function goBusca(e){ if(e){e.preventDefault();e.stopPropagation();} window.location.assign('/portal/busca'); }
 function goFat(e){ if(e){e.preventDefault();e.stopPropagation();} window.location.assign('/portal/faturas'); }
 function killFab(id){ var x=document.getElementById(id); if(x&&x.parentNode)x.parentNode.removeChild(x); }
 function fab(id,txt,onclick,bottom){ if(document.getElementById(id))return; var b=document.createElement('button'); b.id=id; b.type='button'; b.textContent=txt;
   b.style.cssText='position:fixed;right:14px;bottom:'+(bottom||16)+'px;z-index:99999;background:#f97316;color:#111;font-weight:700;font-family:system-ui,sans-serif;font-size:14px;padding:11px 16px;border:0;border-radius:24px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35)';
   b.addEventListener('click',onclick); document.body.appendChild(b); }
 function isMobile(){ return (window.innerWidth||999) <= 860; }
 function killItems(){ ['cx-mos-item','cx-busca-item'].forEach(function(id){ var e=document.getElementById(id); if(e&&e.parentNode)e.parentNode.removeChild(e); }); }
 function mkbtn(txt,onclick,primary){ var b=document.createElement('button'); b.type='button'; b.textContent=txt;
   b.style.cssText='display:block;white-space:nowrap;font-weight:700;font-family:system-ui,sans-serif;font-size:14px;padding:11px 18px;border-radius:24px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.30);'+(primary?'background:#f97316;color:#111;border:0':'background:#fff7ed;color:#7c2d12;border:1px solid #fdba74');
   b.addEventListener('click',onclick); return b; }
 function mobileMenu(){
   if(document.getElementById('cx-menu-wrap'))return;
   var open=false;
   var wrap=document.createElement('div'); wrap.id='cx-menu-wrap';
   wrap.style.cssText='position:fixed;right:14px;bottom:94px;z-index:99999;display:flex;flex-direction:column;align-items:flex-end;gap:10px';
   var panel=document.createElement('div'); panel.id='cx-menu-panel';
   panel.style.cssText='display:none;flex-direction:column;align-items:flex-end;gap:8px';
   function render(){ panel.style.display=open?'flex':'none'; }
   function close(){ open=false; render(); }
   function toggle(){ open=!open; render(); }
   function nav(fn){ return function(e){ if(e){e.preventDefault();e.stopPropagation();} close(); fn(e); }; }
   if(IS_CLI) panel.appendChild(mkbtn('💳 Faturas',nav(goFat),false));
   if(IS_CLI) panel.appendChild(mkbtn('▦ Mosaico',nav(goMos),false));
   panel.appendChild(mkbtn('🔍 Pergunte',nav(goBusca),false));
   var tog=mkbtn('☰ Menu',function(e){ if(e){e.preventDefault();e.stopPropagation();} toggle(); },true); tog.id='cx-menu-toggle';
   wrap.appendChild(panel); wrap.appendChild(tog);
   document.body.appendChild(wrap);
   document.addEventListener('click',function(ev){ if(open && !wrap.contains(ev.target)) close(); });
 }
 function ensure(){
   if(!onPortal()){ killFab('cx-mos-fab'); killFab('cx-busca-fab'); killFab('cx-fat-fab'); killFab('cx-menu-wrap'); killItems(); return; }
   if(isMobile()){
     killItems(); killFab('cx-mos-fab'); killFab('cx-busca-fab'); killFab('cx-fat-fab');
     mobileMenu();
     return;
   }
   killFab('cx-mos-fab'); killFab('cx-busca-fab'); killFab('cx-fat-fab'); killFab('cx-menu-wrap');
   var anchor=findByText(['ajuste']);
   if(!anchor||!anchor.parentNode){
     if(IS_CLI) fab('cx-mos-fab','Mosaico',goMos,78);
     fab('cx-busca-fab','Pergunte ao Corexia',goBusca,20); return;
   }
   if(IS_CLI && !document.getElementById('cx-mos-item')){
     var m=mkItem('cx-mos-item',anchor,'Meu Mosaico',MOS_ICON,goMos);
     anchor.parentNode.insertBefore(m, anchor.nextSibling);
   }
   var busAnchor=document.getElementById('cx-mos-item')||anchor;
   if(!document.getElementById('cx-busca-item')){
     var b=mkItem('cx-busca-item',busAnchor,'Pergunte ao Corexia',BUSCA_ICON,goBusca);
     busAnchor.parentNode.insertBefore(b, busAnchor.nextSibling);
   }
 }
 var pend=false; function sched(){ if(pend)return; pend=true; setTimeout(function(){pend=false; ensure();},140); }
 function start(){ ensure(); try{ new MutationObserver(sched).observe(document.body||document.documentElement,{childList:true,subtree:true}); }catch(e){} window.addEventListener('popstate',sched); window.addEventListener('resize',sched); }
 fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.json();}).then(function(u){ IS_CLI=(u&&u.role==='cliente'); start(); }).catch(function(){ start(); });
})();</script>"""


_PORTAL_BUSCA_JS = r"""<script>/* corexia-portal-busca */(function(){
  if(!localStorage.getItem('corexia_token'))return;
  function onPortal(){ return location.pathname.indexOf('/portal')===0; }
  function go(){ window.location.assign('/portal/busca'); }
  function tick(){
    var f=document.getElementById('cx-busca-fab');
    if(!onPortal()){ if(f&&f.parentNode)f.parentNode.removeChild(f); return; }
    if(f) return;
    var b=document.createElement('button'); b.id='cx-busca-fab'; b.type='button';
    b.innerHTML='<span style="font-size:16px;line-height:1">&#128269;</span> Pergunte ao Corexia';
    b.style.cssText='position:fixed;right:18px;bottom:18px;z-index:99999;display:inline-flex;align-items:center;gap:8px;background:#f97316;color:#1a1205;border:none;border-radius:26px;padding:12px 18px;font-weight:700;font-size:14px;font-family:system-ui,-apple-system,sans-serif;box-shadow:0 6px 22px rgba(0,0,0,.4);cursor:pointer';
    b.addEventListener('click',go);
    document.body.appendChild(b);
  }
  setInterval(tick,1500); tick();
  window.addEventListener('popstate',tick);
})();</script>"""

_PORTAL_MOS_JS = """<script>/* corexia-portal-mos */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.json();}).then(function(u){
  if(!u||u.role!=='cliente')return;
  var URL='/meus-mosaicos?t='+encodeURIComponent(t);
  function findCam(){ var ns=document.querySelectorAll('a,button,li,[role=menuitem]'); for(var i=0;i<ns.length;i++){ var el=ns[i]; if(el.children.length>4)continue; var s=(el.textContent||'').trim().toLowerCase(); if(s.length<=22 && s.indexOf('minhas c')>=0 && s.indexOf('mera')>=0) return el; } return null; }
  function relabel(node){ var set=false; var w=node.querySelectorAll('*'); for(var i=0;i<w.length;i++){ if(w[i].children.length===0 && (w[i].textContent||'').trim().length>2){ w[i].textContent='Meu Mosaico'; set=true; break; } } if(!set)node.textContent='Meu Mosaico'; }
  function go(e){ if(e){e.preventDefault();e.stopPropagation();} window.location.href=URL; }
  function ensure(){
   if(document.getElementById('cx-mos-item'))return;
   var ref=findCam();
   if(ref){ var a=ref.cloneNode(true); a.id='cx-mos-item'; a.removeAttribute('href'); a.style.cursor='pointer'; a.classList.remove('active'); relabel(a); a.addEventListener('click',go,true); ref.parentNode.insertBefore(a, ref.nextSibling); var fb=document.getElementById('cx-mos-btn'); if(fb)fb.remove(); return; }
   if(document.getElementById('cx-mos-btn'))return;
   var b=document.createElement('a'); b.id='cx-mos-btn'; b.href=URL; b.textContent='Meu Mosaico';
   b.style.cssText='position:fixed;left:16px;bottom:16px;z-index:99999;background:#f97316;color:#111;font-weight:700;font-family:system-ui,sans-serif;font-size:14px;padding:10px 16px;border-radius:24px;text-decoration:none;box-shadow:0 4px 14px rgba(0,0,0,.3)';
   document.body.appendChild(b);
  }
  var pend=false; function sched(){ if(pend)return; pend=true; setTimeout(function(){pend=false; ensure();},120); }
  ensure(); try{ var mo=new MutationObserver(sched); mo.observe(document.body||document.documentElement,{childList:true,subtree:true}); }catch(e){}
 }).catch(function(){});
})();</script>"""


_PORTAL_LOGOUT_JS = """<script>/* corexia-portal-logout */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 function done(){ try{localStorage.removeItem('corexia_token');localStorage.removeItem('corexia_user');}catch(e){} window.location.href='/'; }
 function doLogout(e){ if(e){e.preventDefault();e.stopPropagation();} var tk=localStorage.getItem('corexia_token'); try{ fetch('/api/auth/logout',{method:'POST',headers:{'Authorization':'Bearer '+(tk||'')}}).then(done,done); }catch(_){ done(); } }
 function findAjustes(){ var ns=document.querySelectorAll('a,button,li,[role=menuitem]'); for(var i=0;i<ns.length;i++){ var el=ns[i]; if(el.children.length>4)continue; var x=(el.textContent||'').trim().toLowerCase(); if(x.length<=14 && x.indexOf('ajuste')>=0) return el; } return null; }
 function relabel(node,txt){ var set=false; var w=node.querySelectorAll('*'); for(var i=0;i<w.length;i++){ if(w[i].children.length===0 && (w[i].textContent||'').trim().length>1){ w[i].textContent=txt; set=true; break; } } if(!set)node.textContent=txt; }
 function ensure(){
  if(document.getElementById('cx-logout-item'))return;
  var ref=findAjustes();
  if(ref){ var a=ref.cloneNode(true); a.id='cx-logout-item'; a.removeAttribute('href'); a.style.cursor='pointer'; a.classList.remove('active'); relabel(a,'Sair'); try{var _sv=a.querySelector('svg'); if(_sv){_sv.setAttribute('viewBox','0 0 24 24');_sv.setAttribute('fill','none');_sv.setAttribute('stroke','currentColor');_sv.setAttribute('stroke-width','2');_sv.setAttribute('stroke-linecap','round');_sv.setAttribute('stroke-linejoin','round');_sv.innerHTML='<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line>';}}catch(e){} a.addEventListener('click',doLogout,true); ref.parentNode.insertBefore(a, ref.nextSibling); var fb=document.getElementById('cx-logout-btn'); if(fb)fb.remove(); return; }
  if(document.getElementById('cx-logout-btn'))return;
  var b=document.createElement('button'); b.id='cx-logout-btn'; b.innerHTML='&#128682; Sair';
  b.style.cssText='position:fixed;right:16px;top:16px;z-index:99999;background:#1f2937;color:#fff;font-weight:700;font-family:system-ui,sans-serif;font-size:13px;padding:8px 14px;border:0;border-radius:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.3)';
  b.addEventListener('click',doLogout,true); document.body.appendChild(b);
 }
 var pend=false; function sched(){ if(pend)return; pend=true; setTimeout(function(){pend=false; ensure();},150); }
 ensure(); try{ var mo=new MutationObserver(sched); mo.observe(document.body||document.documentElement,{childList:true,subtree:true}); }catch(e){}
})();</script>"""


_PORTAL_THUMB_JS = """<script>/* corexia-portal-thumb */(function(){
 if(!localStorage.getItem('corexia_token'))return;
 function vis(el){ try{var r=el.getBoundingClientRect(); return r.bottom>0 && r.top<(window.innerHeight||1200) && r.right>0 && r.left<(window.innerWidth||1200);}catch(e){return true;} }
 function bump(){ try{ var g=document.querySelectorAll('img[src*="/camthumb/"]'); for(var i=0;i<g.length;i++){ var im=g[i]; if(!vis(im))continue; var b=(im.src||'').split('?')[0]; if(b) im.src=b+'?t='+Date.now(); } }catch(e){} }
 setTimeout(bump,5000); setInterval(bump,40000);
})();</script>"""


_PORTAL_REC_JS = """<script>/* corexia-portal-rec */(function(){
 if(!localStorage.getItem('corexia_token'))return;
 var BTN='display:inline-flex;align-items:center;gap:6px;padding:9px 16px;border-radius:10px;border:0;cursor:pointer;font:600 13px system-ui,sans-serif';
 function recVideo(){ var vs=document.querySelectorAll('video'); for(var i=0;i<vs.length;i++){ if((vs[i].currentSrc||vs[i].src||'').indexOf('/rec/')>=0) return vs[i]; } return null; }
 function baseUrl(src){ return src.split('?t=')[0]; }
 function relpath(src){ try{ var p=src.split('/rec/')[1]; return p?decodeURIComponent(p.split('?')[0]):''; }catch(e){return '';} }
 function toast(msg,ok){ var t=document.createElement('div'); t.textContent=msg; t.style.cssText='position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:100000;background:'+(ok?'#065f46':'#334155')+';color:#fff;padding:10px 18px;border-radius:22px;font:600 13px system-ui;box-shadow:0 6px 20px rgba(0,0,0,.4)'; document.body.appendChild(t); setTimeout(function(){try{t.remove();}catch(e){}},2800); }
 function baixar(){ var v=recVideo(); var src=v&&(v.currentSrc||v.src); if(!src){toast('Toque num trecho primeiro');return;} var u=baseUrl(src); u+=(u.indexOf('?')>=0?'&':'?')+'dl=1'; var a=document.createElement('a'); a.href=u; a.download=''; document.body.appendChild(a); a.click(); a.remove(); toast('Baixando o trecho...',true); }
 function copiar(){ var v=recVideo(); var src=v&&(v.currentSrc||v.src); if(!src){toast('Toque num trecho primeiro');return;} var rp=relpath(src); if(!rp){toast('Nao identifiquei o trecho');return;} var tk=localStorage.getItem('corexia_token'); fetch('/api/gravacoes/sharelink?dias=7&path='+encodeURIComponent(rp),{headers:{'Authorization':'Bearer '+tk}}).then(function(r){return r.json();}).then(function(j){ if(j&&j.url){ if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(j.url).then(function(){toast('Link copiado! Vale 7 dias',true);},function(){window.prompt('Copie o link (7 dias):',j.url);}); } else { window.prompt('Copie o link (7 dias):',j.url); } } else { toast('Nao consegui gerar o link'); } }).catch(function(){toast('Erro ao gerar o link');}); }
 function ensure(){ var v=recVideo(); if(!v){ var o=document.getElementById('cx-rec-bar'); if(o)o.remove(); return; } if(document.getElementById('cx-rec-bar'))return; var bar=document.createElement('div'); bar.id='cx-rec-bar'; bar.style.cssText='display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:12px 0 4px'; var d=document.createElement('button'); d.innerHTML='&#11015; Baixar trecho'; d.style.cssText=BTN+';background:#f97316;color:#111'; d.onclick=baixar; var c=document.createElement('button'); c.innerHTML='&#128279; Copiar link'; c.style.cssText=BTN+';background:#1f2937;color:#fff'; c.onclick=copiar; bar.appendChild(d); bar.appendChild(c); var host=v.closest('div')||v; (host.parentNode||document.body).insertBefore(bar, host.nextSibling); }
 var pend=false; function sched(){if(pend)return;pend=true;setTimeout(function(){pend=false;ensure();},250);}
 ensure(); try{new MutationObserver(sched).observe(document.body||document.documentElement,{childList:true,subtree:true});}catch(e){}
})();</script>"""


_PORTAL_CLIP_JS = """<script>/* corexia-portal-clip */(function(){
 if(!localStorage.getItem('corexia_token'))return;
 var S=null,E=null,lastSrc='';
 var BTN='padding:8px 12px;border-radius:9px;border:0;cursor:pointer;font:600 12.5px system-ui,sans-serif';
 function recVideo(){ var vs=document.querySelectorAll('video'); for(var i=0;i<vs.length;i++){ if((vs[i].currentSrc||vs[i].src||'').indexOf('/rec/')>=0) return vs[i]; } return null; }
 function relpath(src){ try{ var p=src.split('/rec/')[1]; return p?decodeURIComponent(p.split('?')[0]):''; }catch(e){return '';} }
 function fmt(s){ s=Math.max(0,Math.floor(s)); var m=Math.floor(s/60),x=s%60; return (m<10?'0':'')+m+':'+(x<10?'0':'')+x; }
 function toast(msg,ok){ var t=document.createElement('div'); t.textContent=msg; t.style.cssText='position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:100000;background:'+(ok?'#065f46':'#334155')+';color:#fff;padding:10px 18px;border-radius:22px;font:600 13px system-ui'; document.body.appendChild(t); setTimeout(function(){try{t.remove();}catch(e){}},2800); }
 function upd(){ var l=document.getElementById('cx-clip-lbl'); if(l)l.textContent='Recorte -> inicio: '+(S!=null?fmt(S):'--')+'  fim: '+(E!=null?fmt(E):'--'); }
 function recortar(){ var v=recVideo(); var src=v&&(v.currentSrc||v.src); if(!src){toast('Toque num trecho');return;} if(S==null||E==null){toast('Marque inicio e fim');return;} if(E<=S){toast('O fim deve ser depois do inicio');return;} var dur=E-S; if(dur>300){toast('Recorte maximo de 5 min');return;} var rp=relpath(src); if(!rp){toast('Nao identifiquei o trecho');return;} var tk=localStorage.getItem('corexia_token'); var u='/api/gravacoes/clip?path='+encodeURIComponent(rp)+'&start='+S.toFixed(1)+'&dur='+dur.toFixed(1)+'&t='+encodeURIComponent(tk); toast('Recortando '+fmt(dur)+'... aguarde alguns segundos',true); var a=document.createElement('a'); a.href=u; a.download=''; document.body.appendChild(a); a.click(); a.remove(); }
 function enviar(){ var v=recVideo(); var src=v&&(v.currentSrc||v.src); if(!src){toast('Toque num trecho');return;} var rp=relpath(src); if(!rp){toast('Nao identifiquei o trecho');return;} var to=window.prompt('E-mail(s) do destinatario (separe por virgula):',''); if(!to)return; var msg=window.prompt('Mensagem (opcional):','')||''; var body={path:rp,to:to,msg:msg}; if(S!=null&&E!=null&&E>S){ body.start=S; body.dur=E-S; } var tk=localStorage.getItem('corexia_token'); toast('Enviando... aguarde',true); fetch('/api/gravacoes/email',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+tk},body:JSON.stringify(body)}).then(function(r){return r.json();}).then(function(j){ if(j&&j.success){ toast('Enviado! O link do corte foi para o e-mail.',true); } else { toast((j&&j.error)||'Falha ao enviar'); } }).catch(function(){toast('Erro ao enviar');}); }
 function ensure(){ var v=recVideo(); if(!v){ var o=document.getElementById('cx-clip-bar'); if(o)o.remove(); return; } var cs=v.currentSrc||v.src||''; if(cs!==lastSrc){ lastSrc=cs; S=null; E=null; upd(); } if(document.getElementById('cx-clip-bar'))return; var bar=document.createElement('div'); bar.id='cx-clip-bar'; bar.style.cssText='display:flex;gap:8px;justify-content:center;align-items:center;flex-wrap:wrap;margin:2px 0 12px'; var lbl=document.createElement('span'); lbl.id='cx-clip-lbl'; lbl.style.cssText='font:600 12px system-ui;color:#94a3b8;margin-right:4px'; var bi=document.createElement('button'); bi.textContent='Marcar inicio'; bi.style.cssText=BTN+';background:#334155;color:#fff'; bi.onclick=function(){S=v.currentTime; if(E!=null&&E<=S)E=null; upd(); toast('Inicio marcado',true);}; var bf=document.createElement('button'); bf.textContent='Marcar fim'; bf.style.cssText=BTN+';background:#334155;color:#fff'; bf.onclick=function(){E=v.currentTime; upd(); toast('Fim marcado',true);}; var br=document.createElement('button'); br.innerHTML='&#9986; Recortar e baixar'; br.style.cssText=BTN+';background:#f97316;color:#111'; br.onclick=recortar; bar.appendChild(lbl); bar.appendChild(bi); bar.appendChild(bf); bar.appendChild(br); var be=document.createElement('button'); be.innerHTML='&#9993; Enviar por e-mail'; be.style.cssText=BTN+';background:#0e7490;color:#fff'; be.onclick=enviar; bar.appendChild(be); var rb=document.getElementById('cx-rec-bar'); if(rb&&rb.parentNode){ rb.parentNode.insertBefore(bar, rb.nextSibling); } else { var host=v.closest('div')||v; (host.parentNode||document.body).insertBefore(bar, host.nextSibling); } upd(); }
 var pend=false; function sched(){if(pend)return;pend=true;setTimeout(function(){pend=false;ensure();},280);}
 ensure(); try{new MutationObserver(sched).observe(document.body||document.documentElement,{childList:true,subtree:true});}catch(e){}
})();</script>"""


@app.get("/meus-mosaicos")
def meus_mosaicos(req: Request):
    _pg = "<body style='font-family:system-ui,sans-serif;background:#0a0a0a;color:#fff;padding:40px'>%s</body>"
    u = current_user(req, allow_query_token=True)  # mosaico lista via ?t=
    if not u:
        return HTMLResponse(_pg % "Faca login para ver seus mosaicos.", status_code=401)
    _tq = (req.query_params.get("t") or "").strip(); _ts = ("?t=" + _tq) if _tq else ""
    c = db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Mosaico'").fetchall(); c.close()
    mos = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("ativo", True) is False:
            continue
        vis = False
        if u.get("user_type") == "subuser":
            vis = (r["id"] in (u.get("allowed_mosaicos") or [])) and (not u.get("sub_blocked"))
        elif u["role"] == "cliente":
            vis = d.get("cliente_id") == u.get("cliente_id")
        elif u["role"] == "provedor":
            vis = d.get("provedor_id") == u.get("provedor_id")
        elif u["role"] == "admin":
            vis = True
        if vis:
            mos.append((r["id"], _mos_esc(d.get("nome", "")), len(d.get("cameras") or [])))
    cards = ""
    for mid, nome, n in mos:
        cards += ("<a class='card' href='/mosaico/" + mid + _ts + "'><div class='nm'>" + nome + "</div>"
                  "<div class='sub'>" + str(n) + " camera(s) - grade 2x2</div></a>")
    if not cards:
        cards = "<div style='color:#888'>Nenhum mosaico disponivel para voce ainda.</div>"
    html = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Meus Mosaicos</title>"
            "<style>*{box-sizing:border-box}body{margin:0;background:#0a0a0a;color:#fff;font-family:system-ui,sans-serif}"
            ".hd{padding:16px 20px;font-size:18px;font-weight:700;display:flex;justify-content:space-between;align-items:center}"
            ".hd a{color:#111;background:#f97316;font-weight:700;text-decoration:none;font-size:14px;padding:8px 14px;border-radius:20px}"
            ".wrap{padding:0 16px 24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}"
            ".card{display:block;background:#161616;border:1px solid #262626;border-radius:12px;padding:16px;text-decoration:none;color:#fff}"
            ".card:hover{border-color:#f97316}.card .nm{font-weight:700;font-size:15px}.card .sub{color:#888;font-size:12px;margin-top:4px}</style></head>"
            "<body><div class='hd'><span>Meus Mosaicos</span><a href='/'>&larr; Voltar ao painel</a></div><div class='wrap'>" + cards + "</div></body></html>")
    return HTMLResponse(html)


# ==================== SUB-USUARIOS (contas secundarias de um cliente master) ====================
def _pode_subuser(u, client_id):
    if not client_id:
        return False
    if u["role"] == "admin":
        return True
    if u["role"] == "provedor":
        return _cliente_do_provedor(client_id, u["provedor_id"])
    if u["role"] == "cliente" and u.get("user_type") != "subuser":
        return u.get("cliente_id") == client_id
    return False


def _clean_ids(v):
    return [str(x) for x in (v or []) if str(x).strip()]


@app.get("/api/subusers")
async def subusers_list(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    client_id = (req.query_params.get("cliente_id") or "").strip()
    if not _pode_subuser(u, client_id):
        return _forbidden()
    c = db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='SubUser'").fetchall(); c.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("client_id") == client_id:
            d["id"] = r["id"]; d.pop("auth_user_id", None); out.append(d)
    out.sort(key=lambda x: (x.get("nome") or "").lower())
    return out


@app.get("/api/cliente-cameras")
async def cliente_cameras(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    client_id = (req.query_params.get("cliente_id") or "").strip()
    if not _pode_subuser(u, client_id):
        return _forbidden()
    out = [{"id": o.get("id"), "nome": o.get("nome", "") or o.get("id")}
           for o in _todas_cameras() if o.get("cliente_id") == client_id]
    out.sort(key=lambda x: (x["nome"] or "").lower())
    return out


@app.post("/api/subusers")
async def subuser_criar(req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    b = await req.json()
    client_id = (b.get("client_id") or b.get("cliente_id") or "").strip()
    if not _pode_subuser(u, client_id):
        return _forbidden()
    nome = (b.get("nome") or "").strip()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("senha") or b.get("password") or ""
    if not nome or not email or len(pw) < 4:
        return JSONResponse({"error": "informe nome, email e senha (min 4)"}, status_code=400)
    master = _get_entity("Cliente", client_id) or {}
    prov_id = master.get("provedor_id", "")
    _mc = {o.get("id") for o in _todas_cameras() if o.get("cliente_id") == client_id}
    c = db(); uid = secrets.token_hex(8)
    try:
        c.execute("INSERT INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, email, _hash_pw(pw), nome, "cliente", prov_id, client_id, "ativo", _now_iso()))
    except sqlite3.IntegrityError:
        c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
    data = {"client_id": client_id, "provedor_id": prov_id, "nome": nome, "email": email,
            "telefone": (b.get("telefone") or "").strip(), "unidade": (b.get("unidade") or "").strip(),
            "allowed_cameras": [x for x in _clean_ids(b.get("allowed_cameras")) if x in _mc],
            "allowed_gravacoes": [x for x in _clean_ids(b.get("allowed_gravacoes")) if x in _mc],
            "allowed_mosaicos": _clean_ids(b.get("allowed_mosaicos")),
            "receber_alertas_whatsapp": bool(b.get("receber_alertas_whatsapp")),
            "status": "ativo", "auth_user_id": uid, "criado": _now_iso()}
    eid = secrets.token_hex(12); now = _now_iso()
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("SubUser", eid, json.dumps(data), now, now))
    c.commit(); c.close()
    return {"success": True, "id": eid}


@app.put("/api/subusers/{sid}")
async def subuser_update(sid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db(); r = c.execute("SELECT data FROM entities WHERE entity='SubUser' AND id=?", (sid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    d = json.loads(r["data"])
    if not _pode_subuser(u, d.get("client_id", "")):
        c.close(); return _forbidden()
    b = await req.json()
    for k in ("nome", "telefone", "unidade"):
        if k in b:
            d[k] = (b.get(k) or "").strip()
    _mc = {o.get("id") for o in _todas_cameras() if o.get("cliente_id") == d.get("client_id")}
    for k in ("allowed_cameras", "allowed_gravacoes"):
        if k in b:
            d[k] = [x for x in _clean_ids(b.get(k)) if x in _mc]
    if "allowed_mosaicos" in b:
        d["allowed_mosaicos"] = _clean_ids(b.get("allowed_mosaicos"))
    if "receber_alertas_whatsapp" in b:
        d["receber_alertas_whatsapp"] = bool(b.get("receber_alertas_whatsapp"))
    uid = d.get("auth_user_id", "")
    novo_email = (b.get("email") or "").strip().lower()
    if novo_email and novo_email != d.get("email"):
        try:
            c.execute("UPDATE users SET email=? WHERE id=?", (novo_email, uid))
        except sqlite3.IntegrityError:
            c.close(); return JSONResponse({"error": "email ja cadastrado"}, status_code=409)
        d["email"] = novo_email
    if d.get("nome") and uid:
        c.execute("UPDATE users SET full_name=? WHERE id=?", (d["nome"], uid))
    nova_senha = b.get("senha") or b.get("password") or ""
    if nova_senha:
        if len(nova_senha) < 4:
            c.close(); return JSONResponse({"error": "senha min 4"}, status_code=400)
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(nova_senha), uid))
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='SubUser' AND id=?",
              (json.dumps(d), _now_iso(), sid))
    c.commit(); c.close()
    return {"success": True}


@app.post("/api/subusers/{sid}/status")
async def subuser_status(sid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db(); r = c.execute("SELECT data FROM entities WHERE entity='SubUser' AND id=?", (sid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    d = json.loads(r["data"])
    if not _pode_subuser(u, d.get("client_id", "")):
        c.close(); return _forbidden()
    b = await req.json()
    ativo = bool(b.get("ativo", True))
    d["status"] = "ativo" if ativo else "inativo"
    uid = d.get("auth_user_id", "")
    if uid:
        c.execute("UPDATE users SET status=? WHERE id=?", ("ativo" if ativo else "bloqueado", uid))
        if not ativo:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='SubUser' AND id=?",
              (json.dumps(d), _now_iso(), sid))
    c.commit(); c.close()
    return {"success": True}


@app.delete("/api/subusers/{sid}")
async def subuser_delete(sid: str, req: Request):
    u = current_user(req)
    if not u:
        return _unauth()
    c = db(); r = c.execute("SELECT data FROM entities WHERE entity='SubUser' AND id=?", (sid,)).fetchone()
    if not r:
        c.close(); return JSONResponse({"error": "nao encontrado"}, status_code=404)
    d = json.loads(r["data"])
    if not _pode_subuser(u, d.get("client_id", "")):
        c.close(); return _forbidden()
    uid = d.get("auth_user_id", "")
    if uid:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.execute("DELETE FROM entities WHERE entity='SubUser' AND id=?", (sid,))
    c.commit(); c.close()
    return {"success": True}


# ==================== EVENTOS RECENTES (p/ o gravador por-alerta da storage) ====================
@app.post("/eventosRecentes")
async def eventos_recentes(req: Request):
    b = await req.json()
    if not _secret_ok(b):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        segs = max(5, min(3600, int(b.get("segundos", 30))))
    except (TypeError, ValueError):
        segs = 30
    c = db()
    rows = c.execute(
        "SELECT id, camera_id, camera_nome, tipo, confianca, criado FROM alertas "
        "WHERE camera_id != '' AND criado >= datetime('now', ?) ORDER BY id DESC LIMIT 300",
        (f"-{segs} seconds",)).fetchall()
    c.close()
    return {"eventos": [dict(r) for r in rows]}


# ==================== SERVE O FRONT DO COREXIA (SPA) ====================
WEB = os.path.join(HERE, "web")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
if os.path.isdir(os.path.join(WEB, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEB, "assets")), name="assets")
if os.path.isdir(os.path.join(WEB, "brand")):
    app.mount("/brand", StaticFiles(directory=os.path.join(WEB, "brand")), name="brand")

@app.get("/manifest.json")
def manifest(request: Request):
    try:
        _b = _wl_brand(request.headers.get("host", ""))
    except Exception:
        _b = None
    if _b and _b.get("nome_marca"):
        _nm = _b["nome_marca"]
        _th = _b.get("cor_menu") or _b.get("cor") or "#0f1115"
        _m = {"id": "/", "name": _nm, "short_name": (_nm or "App")[:12],
              "description": "Suas cameras e alertas na palma da mao.",
              "lang": "pt-BR", "start_url": "/", "scope": "/", "display": "standalone",
              "display_override": ["standalone", "minimal-ui"], "orientation": "portrait-primary",
              "background_color": "#0f1115", "theme_color": _th,
              "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                        {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                        {"src": "/pwa-icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
        return JSONResponse(_m, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})
    p = os.path.join(WEB, "manifest.json")
    return FileResponse(p, media_type="application/manifest+json") if os.path.exists(p) else JSONResponse({}, status_code=404)

@app.get("/sw.js")
def sw():
    p = os.path.join(WEB, "sw.js")
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"}) if os.path.exists(p) else JSONResponse({}, status_code=404)

# ---- PWA white-label: icones compostos por provedor (resolvem por Host) ----
_PWA_ICON_CACHE = os.path.join(WEB, "brand", "pwa_cache")

def _hex_rgb(hx):
    hx = (hx or "").strip().lstrip("#")
    if len(hx) == 3:
        hx = "".join(c * 2 for c in hx)
    if len(hx) != 6:
        return (15, 17, 21)
    try:
        return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))
    except Exception:
        return (15, 17, 21)

def _pwa_default_icon(size):
    fn = "icon-512.png" if size >= 512 else "icon-192.png"
    p = os.path.join(WEB, fn)
    return p if os.path.exists(p) else None

def _pwa_icon_for_host(host, size, maskable=False):
    try:
        b = _wl_brand(host)
    except Exception:
        b = None
    if not b or not b.get("logo"):
        return _pwa_default_icon(size)
    logo_path = os.path.join(WEB, b["logo"].lstrip("/"))
    if not os.path.exists(logo_path):
        return _pwa_default_icon(size)
    try:
        os.makedirs(_PWA_ICON_CACHE, exist_ok=True)
        cache = os.path.join(_PWA_ICON_CACHE, "%s-%d-%s.png" % (b.get("pid", "x"), size, "m" if maskable else "n"))
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(logo_path):
            return cache
        from PIL import Image
        try:
            _RES = Image.Resampling.LANCZOS
        except Exception:
            _RES = Image.LANCZOS
        canvas = Image.new("RGBA", (size, size), _hex_rgb(b.get("cor_menu") or "#0f1115") + (255,))
        logo = Image.open(logo_path).convert("RGBA")
        pad = int(size * (0.22 if maskable else 0.14))
        box = max(1, size - 2 * pad)
        lw, lh = logo.size
        sc = min(box / float(lw), box / float(lh))
        nw, nh = max(1, int(lw * sc)), max(1, int(lh * sc))
        logo = logo.resize((nw, nh), _RES)
        canvas.alpha_composite(logo, ((size - nw) // 2, (size - nh) // 2))
        canvas.convert("RGB").save(cache, "PNG")
        return cache
    except Exception as _e:
        print("[pwa-icon] falha:", _e)
        return _pwa_default_icon(size)

@app.get("/pwa-icon-{spec}")
def pwa_icon(spec: str, request: Request):
    s = spec[:-4] if spec.lower().endswith(".png") else spec
    maskable = s.endswith("-maskable")
    if maskable:
        s = s[:-len("-maskable")]
    try:
        size = int(s)
    except Exception:
        size = 192
    if size not in (180, 192, 512):
        size = 192
    p = _pwa_icon_for_host(request.headers.get("host", ""), size, maskable)
    if p and os.path.exists(p):
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    return JSONResponse({}, status_code=404)

# === LP Corexia + analytics (rotas /lp, /lp/admin, /api/lp/*) ===
try:
    from lp_analytics import router as _lp_router
    app.include_router(_lp_router)
    _lp_assets_dir = os.path.join(HERE, "lp", "assets")
    if os.path.isdir(_lp_assets_dir):
        app.mount("/lp-assets", StaticFiles(directory=_lp_assets_dir), name="lp-assets")
    print("[lp_analytics] rotas /lp, /lp/admin e /api/lp/* ativas")
except Exception as _lp_err:
    print("[lp_analytics] falha ao carregar:", _lp_err)

# === Comercial Corexia (telas proprias servidas aqui: /comercial/*, /api/comercial/*) ===
# IMPORTANTE: incluir ANTES do catch-all do SPA abaixo, senao /comercial cai no index.html do SPA.
_COM_BRIDGE = ""   # ponte injetada no index.html do SPA (itens comerciais do menu -> /comercial/*)
_WL_JS = ""
def _wl_style(_h): return ""
def _wl_brand(_h): return None
try:
    from comercial import (router as _com_router, COMERCIAL_BRIDGE_JS as _COM_BRIDGE,
                           WHITE_LABEL_JS as _WL_JS, wl_style_for_host as _wl_style)

    # === Proposta Personalizada (LP com preco em /p/{slug} + tela /comercial/proposta-lp) ===
    # ORDEM IMPORTA: entra ANTES do _com_router porque comercial.py tem /comercial/{slug} catch-all.
    try:
        from proposta_lp import router as _plp_router
        app.include_router(_plp_router)
        print("[proposta_lp] rotas /comercial/proposta-lp, /api/comercial/proposta-lp* e /p/{slug} ativas")
    except Exception as _plp_err:
        print("[proposta_lp] falha ao carregar:", _plp_err)
    app.include_router(_com_router)
    print("[comercial] rotas /comercial e /api/comercial/* ativas")
    try:
        from comercial import wl_brand_for_host as _wl_brand
    except Exception:
        pass
except Exception as _com_err:
    print("[comercial] falha ao carregar:", _com_err)


@app.post("/api/storage/heartbeat")
async def api_storage_heartbeat(req: Request):
    """Heartbeat do agente de storage (.122): grava _metrics.json que o /api/infra lê. Autenticado por WEBHOOK_SECRET."""
    b = await req.json()
    if not _secret_ok(b):
        return _forbidden()
    b.pop("secret", None); b.pop("validar", None)
    b["ts"] = int(time.time())
    d = os.getenv("GRAV_DIR") or os.path.join(HERE, "gravacoes")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_metrics.json"), "w") as fp:
            json.dump(b, fp)
    except Exception as e:
        return JSONResponse({"error": str(e)[:80]}, status_code=500)
    return {"ok": True}


@app.post("/api/ia/heartbeat")
async def api_ia_heartbeat(req: Request):
    """Heartbeat do no de IA (.126): grava ia_health.json (que /healthz e o painel leem). Autenticado por WEBHOOK_SECRET."""
    b = await req.json()
    if not _secret_ok(b):
        return _forbidden()
    b.pop("secret", None); b.pop("validar", None)
    b["updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(os.path.join(HERE, "ia_health.json"), "w") as fp:
            json.dump(b, fp)
    except Exception as e:
        return JSONResponse({"error": str(e)[:80]}, status_code=500)
    return {"ok": True}


# ============ LIVE REMUX (video copy + audio->AAC; resolve AC-3/MP3 que o browser nao decodifica) ============
_CAMLIVE_DIR = os.path.join(HERE, "camlive")
try: os.makedirs(_CAMLIVE_DIR, exist_ok=True)
except Exception: pass
_camlive = {}
_camlive_lock = threading.Lock()

def _camlive_spawn(cid, m3u8):
    import shutil as _sh
    d = os.path.join(_CAMLIVE_DIR, cid)
    try: _sh.rmtree(d)
    except Exception: pass
    os.makedirs(d, exist_ok=True)
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
         "-rw_timeout", "15000000", "-i", m3u8,
         "-map", "0:v:0", "-c:v", "copy", "-an",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "8",
         "-hls_flags", "omit_endlist+independent_segments",
         "-hls_segment_filename", os.path.join(d, "seg%d.ts"), os.path.join(d, "index.m3u8")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _camlive[cid] = {"proc": p, "dir": d, "last": time.time()}
    return _camlive[cid]

def _camlive_reaper():
    import shutil as _sh
    while True:
        time.sleep(15)
        now = time.time()
        with _camlive_lock:
            for cid in list(_camlive):
                st = _camlive[cid]
                if now - st["last"] > 45 or st["proc"].poll() is not None:
                    try: st["proc"].kill()
                    except Exception: pass
                    try: _sh.rmtree(st["dir"])
                    except Exception: pass
                    _camlive.pop(cid, None)
threading.Thread(target=_camlive_reaper, daemon=True).start()

_EMBED_LIVE_RE_CL = re.compile(r"let live = '([^']+)'")
def _resolve_cam_m3u8(cam):
    _u = (cam.get("rtsp_url") or cam.get("stream_url") or "").strip()
    if ".m3u8" in _u:
        return _u
    _e = "".join(_c for _c in (cam.get("embed_url") or "") if ord(_c) >= 32).strip()
    if _e:
        try:
            _h = requests.get(_e, timeout=8, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://analitico.grupocorexia.com.br/"}).text
            _m = _EMBED_LIVE_RE_CL.search(_h)
            if _m:
                return _m.group(1)
        except Exception:
            pass
    return ""

@app.get("/api/camlive/{cid}/{fname}")
def camlive(cid: str, fname: str, req: Request):
    if any(x in cid for x in ("/", "\\", "..")) or any(x in fname for x in ("/", "\\", "..")):
        return JSONResponse({"error": "invalido"}, status_code=400)
    u = current_user(req, allow_query_token=True)
    if not u:
        return _unauth()
    cam = _get_entity("Camera", cid) or {}
    m3u8 = _resolve_cam_m3u8(cam)
    if ".m3u8" not in m3u8:
        return JSONResponse({"error": "sem stream hls"}, status_code=404)
    with _camlive_lock:
        st = _camlive.get(cid)
        if not st or st["proc"].poll() is not None:
            st = _camlive_spawn(cid, m3u8)
        st["last"] = time.time()
        d = st["dir"]
    path = os.path.join(d, fname)
    if fname.endswith(".m3u8"):
        for _ in range(70):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.1)
        if not os.path.exists(path):
            return JSONResponse({"error": "stream nao iniciou"}, status_code=504)
        _txt = open(path, encoding="utf-8", errors="replace").read()
        _tok = req.query_params.get("t") or (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
        if _tok:
            import urllib.parse as _up
            _tq = _up.quote(_tok, safe="")
            _lns = []
            for _ln in _txt.splitlines():
                _st = _ln.strip()
                if _st and (not _st.startswith("#")) and _st.endswith(".ts"):
                    _lns.append(_st + "?t=" + _tq)
                else:
                    _lns.append(_ln)
            _txt = "\n".join(_lns) + "\n"
        return Response(_txt, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "segmento"}, status_code=404)


_PERF_JS = r"""<script>/* corexia-perf */
(function(){
  if(window.__cxPerf)return; window.__cxPerf=true;
  var Native=window.MutationObserver; if(!Native)return;
  var scrolling=false,t=null,deferred=[];
  function settle(){ scrolling=false; var d=deferred; deferred=[]; for(var i=0;i<d.length;i++){ try{d[i]();}catch(e){} } }
  function onScroll(){ scrolling=true; if(t)clearTimeout(t); t=setTimeout(settle,200); }
  try{ window.addEventListener('scroll',onScroll,{passive:true,capture:true}); window.addEventListener('touchmove',onScroll,{passive:true,capture:true}); }catch(e){}
  function W(cb){ var self=this; self._pend=false;
    self._i=new Native(function(recs,o){
      if(scrolling && self._wide){ if(!self._pend){ self._pend=true; deferred.push(function(){ self._pend=false; try{cb([],self);}catch(e){} }); } return; }
      cb(recs,o);
    });
  }
  W.prototype.observe=function(target,opts){ try{ this._wide=!!(opts&&opts.subtree)&&(target===document.body||target===document.documentElement); }catch(e){ this._wide=false; } return this._i.observe(target,opts); };
  W.prototype.disconnect=function(){ return this._i.disconnect(); };
  W.prototype.takeRecords=function(){ return this._i.takeRecords(); };
  window.MutationObserver=W;
})();</script>"""

_PORTAL_PWA_JS = r"""<script>/* corexia-pwa */
(function(){
  if(window.__cxPwa)return; window.__cxPwa=true;
  if('serviceWorker' in navigator){ window.addEventListener('load',function(){ navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){}); }); }
  var mm=window.matchMedia;
  var standalone=(mm && mm('(display-mode: standalone)').matches) || (navigator.standalone===true);
  if(standalone) return;
  var ua=navigator.userAgent||'';
  var isIOS=/iphone|ipad|ipod/i.test(ua) || (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
  var isMobile=isIOS || /android/i.test(ua) || (mm && mm('(max-width: 860px)').matches);
  if(!isMobile) return;
  try{ var dz=parseInt(localStorage.getItem('cx_pwa_dismiss')||'0',10); if(dz && (Date.now()-dz) < 7*24*3600*1000) return; }catch(e){}
  var deferred=null;
  var BRAND=(window.__WL__&&window.__WL__.nome)?String(window.__WL__.nome):'Corexia';
  var SHARE='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b9dff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-3px"><path d="M12 15V3"></path><path d="M8 7l4-4 4 4"></path><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"></path></svg>';
  var PLUSB='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3b9dff" stroke-width="2" stroke-linecap="round" style="vertical-align:-3px"><rect x="3" y="3" width="18" height="18" rx="4"></rect><path d="M12 8v8M8 12h8"></path></svg>';
  function el(t,c,x){ var e=document.createElement(t); if(c)e.style.cssText=c; if(x!=null)e.textContent=x; return e; }
  function save(ms){ try{ localStorage.setItem('cx_pwa_dismiss',String(ms)); }catch(e){} }
  function hide(){ var b=document.getElementById('cx-pwa'); if(b&&b.parentNode)b.parentNode.removeChild(b); }
  function dismiss(){ save(Date.now()); hide(); }
  window.addEventListener('appinstalled',function(){ save(Date.now()+3650*24*3600*1000); hide(); });
  window.addEventListener('beforeinstallprompt',function(e){ e.preventDefault(); deferred=e; show('android'); });
  function show(kind){
    if(document.getElementById('cx-pwa'))return;
    if(!document.body){ setTimeout(function(){show(kind);},400); return; }
    if(!document.getElementById('cx-pwa-css')){ var s=el('style'); s.id='cx-pwa-css'; s.textContent='@keyframes cxpwaup{from{transform:translateY(-16px);opacity:0}to{transform:translateY(0);opacity:1}}'; (document.head||document.documentElement).appendChild(s); }
    var wrap=el('div','position:fixed;top:0;left:0;right:0;z-index:2147483000;display:flex;justify-content:center;padding:10px 12px;padding-top:calc(env(safe-area-inset-top, 0px) + 10px);box-sizing:border-box;pointer-events:none;font-family:system-ui,-apple-system,sans-serif'); wrap.id='cx-pwa';
    var card=el('div','pointer-events:auto;width:100%;max-width:440px;max-height:82vh;overflow-y:auto;-webkit-overflow-scrolling:touch;background:#12151b;color:#f2f4f6;border:1px solid #262d38;border-radius:16px;box-shadow:0 16px 50px rgba(0,0,0,.55);padding:16px 16px 14px;position:relative;animation:cxpwaup .26s ease');
    var x=el('button','position:absolute;top:6px;right:10px;background:none;border:0;color:#8b96a6;font-size:23px;line-height:1;cursor:pointer','×'); x.setAttribute('aria-label','Fechar'); x.onclick=dismiss;
    var rowc=el('div','display:flex;align-items:center;gap:12px;padding-right:16px');
    var ic=el('img','width:54px;height:54px;border-radius:13px;flex:none;background:#0a0a0a'); ic.src='/pwa-icon-192.png'; ic.alt=BRAND;
    var tc=el('div','min-width:0;flex:1');
    tc.appendChild(el('div','font-weight:700;font-size:15.5px','Instale o app '+BRAND));
    tc.appendChild(el('div','color:#9aa4b2;font-size:12.5px;margin-top:2px','Suas câmeras e alertas na tela inicial, com abertura rápida.'));
    rowc.appendChild(ic); rowc.appendChild(tc); card.appendChild(x); card.appendChild(rowc);
    if(kind==='android'){
      var b1=el('button','margin-top:14px;width:100%;background:#f97316;color:#111;font-weight:700;font-size:15px;border:0;border-radius:12px;padding:12px;cursor:pointer','Instalar aplicativo');
      b1.onclick=function(){ if(!deferred){ dismiss(); return; } deferred.prompt(); deferred.userChoice.then(function(){ deferred=null; hide(); }); };
      var b2=el('button','margin-top:8px;width:100%;background:none;color:#8b96a6;font-size:13px;border:0;cursor:pointer','Agora não'); b2.onclick=dismiss;
      card.appendChild(b1); card.appendChild(b2);
    } else {
      var st=el('div','margin-top:13px;background:#0e1116;border:1px solid #232a34;border-radius:12px;padding:12px;font-size:13.5px;line-height:1.6;color:#cdd5df');
      st.innerHTML='Para instalar no iPhone/iPad:<br>1) Toque em <b>Compartilhar</b> '+SHARE+' na barra do Safari.<br>2) Escolha <b>Adicionar à Tela de Início</b> '+PLUSB+'.';
      var ok=el('button','margin-top:12px;width:100%;background:#f97316;color:#111;font-weight:700;font-size:15px;border:0;border-radius:12px;padding:11px;cursor:pointer','Entendi'); ok.onclick=dismiss;
      card.appendChild(st); card.appendChild(ok);
    }
    wrap.appendChild(card); document.body.appendChild(wrap);
  }
  if(isIOS){ setTimeout(function(){ show('ios'); }, 1600); }
})();</script>"""

@app.get("/{full_path:path}")
def spa(full_path: str, request: Request):
    # arquivos estaticos da raiz do build (favicon, icones pwa) — com containment robusto
    if (full_path and not os.path.isabs(full_path)
            and not any(s in full_path for s in ("/", "\\", ":"))
            and ".." not in full_path and "." in full_path):
        web_root = os.path.realpath(WEB)
        p = os.path.realpath(os.path.join(web_root, full_path))
        if os.path.commonpath([p, web_root]) == web_root and os.path.isfile(p):
            return FileResponse(p)
    idx = os.path.join(WEB, "index.html")
    if os.path.exists(idx):
        _hdr = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
        try:
            _html = open(idx, "r", encoding="utf-8").read()
            # white-label por DOMINIO PROPRIO: tema no <head> (sem flash)
            try:
                _head = _wl_style(request.headers.get("host", ""))
                if _head and "wl-theme" not in _html and "</head>" in _html:
                    _html = _html.replace("</head>", _head + "</head>")
            except Exception as _we:
                print("[wl] head:", _we)
            # injeta ponte comercial + white-label client no <body> (index.html em disco intacto)
            _inj = ""
            if "corexia-perf" not in _html:
                _inj += _PERF_JS
            if _COM_BRIDGE and "corexia-bridge" not in _html:
                _inj += _COM_BRIDGE
            if _WL_JS and "corexia-wl" not in _html:
                _inj += _WL_JS
            if "corexia-portal-menu" not in _html:
                _inj += _PORTAL_MENU_JS
            # (Sair da barra removido — usa o logout nativo do portal, embaixo do nome do cliente)
            if "corexia-portal-thumb" not in _html:
                _inj += _PORTAL_THUMB_JS
            if "corexia-portal-rec" not in _html:
                _inj += _PORTAL_REC_JS
            if "corexia-portal-clip" not in _html:
                _inj += _PORTAL_CLIP_JS
            if "corexia-portal-faturas" not in _html:
                _inj += _PORTAL_FATURAS_JS
            if "corexia-pwa" not in _html:
                _inj += _PORTAL_PWA_JS
            # (busca FAB e meu-mosaico agora sao itens de menu via _PORTAL_MENU_JS)
            if "corexia-net" not in _html:
                _inj += _NET_WIDGET_JS
            if "corexia-anxredir" not in _html:
                _inj += _ANXREDIR_JS
            if _inj:
                _html = (_html.replace("</body>", _inj + "</body>") if "</body>" in _html else _html + _inj)
            return HTMLResponse(_html, headers=_hdr)
        except Exception as _e:
            print("[spa] injecao falhou:", _e)
        return FileResponse(idx, headers=_hdr)
    return HTMLResponse("<h1>Corexia</h1><p>Front nao publicado.</p>")
