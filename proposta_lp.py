"""
Corexia - PROPOSTA PERSONALIZADA (a LP oficial, com preco, num link por slug).

- Tela admin em /comercial/proposta-lp (mesma casca do console comercial)
- API  em /api/comercial/proposta-lp*  (somente admin)
- Publica em /p/{slug} : serve a LP OFICIAL (lp/index.html) com um bloco de
  INVESTIMENTO injetado em tempo de resposta. O arquivo da LP NUNCA e alterado
  em disco -> /lp continua exatamente como esta hoje (sem preco).

Padrao: R$ 797/mes = acesso completo ao sistema. Valor, entregas, nome do cliente
e nome do provedor sao editaveis na hora da criacao.

Incluido pelo server.py:
    from proposta_lp import router as _plp_router; app.include_router(_plp_router)
"""
import os, re, json, time, secrets, threading
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

# reusa os helpers ja existentes do modulo comercial (casca, auth, CRUD de entidades)
from comercial import _shell, _current_user, _db, _now_iso, _get_ent, _update_ent, _create_ent

try:
    import asaas
except Exception as _e:          # nunca derruba a LP por causa do gateway
    asaas = None
    print("[proposta_lp] asaas indisponivel:", _e)

HERE = os.path.dirname(os.path.abspath(__file__))
LP_INDEX = os.path.join(HERE, "lp", "index.html")
ENT = "PropostaLP"

router = APIRouter()

# ---------------------------------------------------------------- checkout
# TRAVA DE SEGURANCA: enquanto isto for 0, o botao roda o fluxo INTEIRO mas NAO
# cria nada no Asaas (mostra uma pagina de simulacao). A conta configurada e de
# PRODUCAO e ja tem cobrancas reais de outra operacao -> so ligar apos conferir.
CHECKOUT_LIVE = os.getenv("PROPOSTA_CHECKOUT_LIVE", "0").strip().lower() in ("1", "true", "yes", "on")
VENC_DIAS = int(os.getenv("PROPOSTA_VENC_DIAS", "3") or 3)   # 1o vencimento = hoje + N dias

_RL_LOCK = threading.Lock()
_RL = {}                      # ip -> [timestamps]  (anti-abuso do endpoint publico)
_RL_JANELA = 600              # 10 min
_RL_MAX = 6

# ---------------------------------------------------------------- padroes
# ATENCAO: os textos abaixo sao lidos PELO CLIENTE na proposta -> vao acentuados
# de proposito (o resto do projeto usa ASCII, mas ali sao telas internas).
VALOR_PADRAO = 797.0
PERIODO_PADRAO = "/mês"
ENTREGAS_PADRAO = [
    "Acesso completo ao sistema Corexia (painel web + app do cliente)",
    "IA de detecção em tempo real: arma de fogo, arma branca, fogo, pessoa e veículo",
    "Alerta instantâneo no WhatsApp e no painel, com foto do momento",
    "Multi-tenant: cada cliente final enxerga apenas as próprias câmeras",
    "Painel com a sua marca (logo e cores do provedor)",
    "Mapa de calor de ocorrências e relatórios",
    "Onboarding da equipe e suporte técnico",
]

# sem l / o / 0 / 1 -> slug nunca vira confusao visual quando ditado por telefone
_SLUG_ALFA = "abcdefghijkmnpqrstuvwxyz23456789"
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")


# ---------------------------------------------------------------- util
def _esc(s):
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _num_br(v):
    """797.0 -> '797'   |   1297.5 -> '1.297,50'"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    inteiro, dec = ("%.2f" % v).split(".")
    mil = ""
    while len(inteiro) > 3:
        mil = "." + inteiro[-3:] + mil
        inteiro = inteiro[:-3]
    inteiro += mil
    return inteiro if dec == "00" else inteiro + "," + dec


def _data_br(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def _admin(req):
    u = _current_user(req)
    return u if (u and u.get("role") == "admin") else None


def _todas():
    c = _db()
    rows = c.execute(
        "SELECT id, data, created_date FROM entities WHERE entity=? ORDER BY created_date DESC",
        (ENT,)).fetchall()
    c.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except ValueError:
            continue
        d["id"] = r["id"]
        d.setdefault("criado_em", r["created_date"])
        out.append(d)
    return out


def _por_slug(slug):
    alvo = (slug or "").strip().lower()
    if not alvo:
        return None
    for p in _todas():
        if (p.get("slug") or "").lower() == alvo:
            return p
    return None


def _slug_novo(n=7):
    for _ in range(40):
        s = "".join(secrets.choice(_SLUG_ALFA) for _ in range(n))
        if not _por_slug(s):
            return s
    return "".join(secrets.choice(_SLUG_ALFA) for _ in range(n + 4))


def _entregas_norm(v):
    """aceita lista OU texto com uma entrega por linha"""
    if isinstance(v, list):
        itens = [str(x).strip() for x in v]
    else:
        itens = [ln.strip() for ln in str(v or "").replace("\r", "").split("\n")]
    itens = [i for i in itens if i][:20]
    return itens or list(ENTREGAS_PADRAO)


# ---------------------------------------------------------------- API admin
@router.get("/api/comercial/proposta-lp")
def api_listar(req: Request):
    if not _admin(req):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    return {"success": True, "propostas": _todas(),
            "valor_padrao": VALOR_PADRAO, "entregas_padrao": ENTREGAS_PADRAO,
            "checkout_live": CHECKOUT_LIVE, "venc_dias": VENC_DIAS}


@router.post("/api/comercial/proposta-lp/salvar")
async def api_salvar(req: Request):
    u = _admin(req)
    if not u:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()

    cliente = (b.get("cliente_nome") or "").strip()
    if not cliente:
        return JSONResponse({"error": "informe o nome do cliente"}, status_code=400)

    try:
        valor = float(b.get("valor_mensal", VALOR_PADRAO) or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "valor mensal invalido"}, status_code=400)
    if valor < 0:
        return JSONResponse({"error": "valor mensal invalido"}, status_code=400)

    pid = (b.get("id") or "").strip()
    atual = _get_ent(ENT, pid) if pid else None
    if pid and not atual:
        return JSONResponse({"error": "proposta nao encontrada"}, status_code=404)

    # slug: informado (validado) OU mantem o atual OU gera um novo
    slug = (b.get("slug") or "").strip().lower()
    if slug:
        if not _SLUG_OK.match(slug):
            return JSONResponse(
                {"error": "slug invalido: use 3 a 32 caracteres (a-z, 0-9 e hifen)"}, status_code=400)
        outro = _por_slug(slug)
        if outro and outro.get("id") != pid:
            return JSONResponse({"error": "ja existe uma proposta com esse slug"}, status_code=400)
    else:
        slug = (atual or {}).get("slug") or _slug_novo()

    data = {
        "slug": slug,
        "cliente_nome": cliente,
        "provedor_nome": (b.get("provedor_nome") or "").strip(),
        "valor_mensal": valor,
        "periodo": (b.get("periodo") or PERIODO_PADRAO).strip() or PERIODO_PADRAO,
        "titulo": (b.get("titulo") or "").strip(),
        "subtitulo": (b.get("subtitulo") or "").strip(),
        "entregas": _entregas_norm(b.get("entregas")),
        "observacao": (b.get("observacao") or "").strip(),
        "validade": (b.get("validade") or "").strip(),
        "whatsapp": (b.get("whatsapp") or "").strip(),
        "cta_label": (b.get("cta_label") or "").strip(),
        "cobranca": "unica" if (b.get("cobranca") or "") == "unica" else "assinatura",
        "status": (b.get("status") or "rascunho").strip(),
        "ativo": bool(b.get("ativo", True)),
    }
    # o aceite (dados do checkout) nunca e sobrescrito por uma edicao da proposta
    if atual and atual.get("aceite"):
        data["aceite"] = atual["aceite"]
    if atual and atual.get("aceites"):
        data["aceites"] = atual["aceites"]

    if pid:
        _update_ent(ENT, pid, data)
    else:
        data["criado_em"] = _now_iso()
        data["criado_por"] = u.get("email", "")
        pid = _create_ent(ENT, data)

    return {"success": True, "id": pid, "slug": slug, "url": "/p/" + slug}


@router.post("/api/comercial/proposta-lp/{pid}/excluir")
def api_excluir(pid: str, req: Request):
    if not _admin(req):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    c.execute("DELETE FROM entities WHERE entity=? AND id=?", (ENT, pid))
    c.commit()
    c.close()
    return {"success": True}


# ---------------------------------------------------------------- pagina admin
@router.get("/comercial/proposta-lp")
def pagina_admin(req: Request):
    return _shell("proposta-lp", "Proposta Personalizada", _BODY)


# ---------------------------------------------------------------- CHECKOUT (publico)
def _ip(req):
    fwd = (req.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (req.client.host if req.client else "?")


def _rate_ok(ip):
    agora = time.time()
    with _RL_LOCK:
        h = [t for t in _RL.get(ip, []) if agora - t < _RL_JANELA]
        if len(h) >= _RL_MAX:
            _RL[ip] = h
            return False
        h.append(agora)
        _RL[ip] = h
        if len(_RL) > 4000:            # nao deixa o dict crescer sem fim
            for k in [k for k, v in _RL.items() if not v or agora - v[-1] > _RL_JANELA]:
                _RL.pop(k, None)
    return True


def _expirada(p):
    v = (p.get("validade") or "").strip()
    if not v:
        return False
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").date() < datetime.now().date()
    except ValueError:
        return False


def _doc_ok(doc):
    return len(doc) in (11, 14)


@router.post("/api/proposta/{slug}/checkout")
async def checkout(slug: str, req: Request):
    """Publico: recebe os dados do cliente e devolve a URL de pagamento.
    Enquanto PROPOSTA_CHECKOUT_LIVE=0 roda tudo menos a chamada ao Asaas."""
    p = _por_slug(slug)
    if not p or not p.get("ativo", True):
        return JSONResponse({"error": "proposta indisponível"}, status_code=404)
    if _expirada(p):
        return JSONResponse({"error": "esta proposta expirou — fale com o seu contato comercial"},
                            status_code=400)
    if not _rate_ok(_ip(req)):
        return JSONResponse({"error": "muitas tentativas — aguarde alguns minutos"}, status_code=429)

    try:
        b = await req.json()
    except Exception:
        return JSONResponse({"error": "requisição inválida"}, status_code=400)

    nome = (b.get("nome") or "").strip()
    doc = "".join(c for c in str(b.get("doc") or "") if c.isdigit())
    email = (b.get("email") or "").strip()
    whats = "".join(c for c in str(b.get("whatsapp") or "") if c.isdigit())

    if len(nome) < 3:
        return JSONResponse({"error": "informe o nome ou a razão social"}, status_code=400)
    if not _doc_ok(doc):
        return JSONResponse({"error": "CPF ou CNPJ inválido"}, status_code=400)
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "e-mail inválido"}, status_code=400)

    # idempotencia: mesmo documento clicando de novo volta pro MESMO pagamento
    # (checa TODO o historico, nao so o ultimo -> nunca cobra o mesmo CNPJ duas vezes)
    historico = list(p.get("aceites") or ([p["aceite"]] if p.get("aceite") else []))
    for h in historico:
        if h.get("doc") == doc and h.get("url"):
            return {"success": True, "url": h["url"], "repetido": True,
                    "simulado": bool(h.get("simulado"))}

    valor = float(p.get("valor_mensal") or 0)
    unica = (p.get("cobranca") or "assinatura") == "unica"
    venc = (datetime.now() + timedelta(days=VENC_DIAS)).strftime("%Y-%m-%d")
    desc = "Corexia - %s%s" % (p.get("cliente_nome") or "", "" if unica else " (mensal)")
    ref = "proposta-lp:%s" % (p.get("slug") or "")

    aceite = {"nome": nome, "doc": doc, "email": email, "whatsapp": whats,
              "quando": _now_iso(), "ip": _ip(req), "valor": valor,
              "cobranca": "unica" if unica else "assinatura"}

    if not CHECKOUT_LIVE:
        # ---- MODO SIMULACAO: nada e criado no Asaas ----
        aceite.update({"simulado": True, "url": "/p/%s/simulacao" % p.get("slug")})
    else:
        if asaas is None:
            return JSONResponse({"error": "gateway indisponível no momento"}, status_code=503)
        try:
            cli = asaas.create_customer(name=nome, cpf_cnpj=doc, email=email,
                                        phone=whats, external_ref=ref)
            cid = cli.get("id")
            if unica:
                pg = asaas.create_payment(customer=cid, value=valor, due_date=venc,
                                          billing_type="UNDEFINED", description=desc, external_ref=ref)
                url = pg.get("invoiceUrl")
                aceite.update({"asaas_customer": cid, "asaas_payment": pg.get("id")})
            else:
                sub = asaas.create_subscription(customer=cid, value=valor, next_due_date=venc,
                                                cycle="MONTHLY", billing_type="UNDEFINED",
                                                description=desc, external_ref=ref)
                sid = sub.get("id")
                url = ""
                pays = asaas.list_payments(subscription_id=sid) or {}
                for pay in (pays.get("data") or []):
                    if pay.get("invoiceUrl"):
                        url = pay["invoiceUrl"]
                        break
                aceite.update({"asaas_customer": cid, "asaas_subscription": sid})
            if not url:
                return JSONResponse(
                    {"error": "cobrança criada, mas o link ainda não ficou pronto — "
                              "nosso comercial vai te enviar em instantes"}, status_code=502)
            aceite.update({"simulado": False, "url": url})
        except Exception as e:
            print("[proposta_lp] checkout %s FALHOU: %r" % (slug, e))
            return JSONResponse({"error": "não foi possível gerar o pagamento agora — tente de novo "
                                          "em instantes ou fale com o seu contato comercial"},
                                status_code=502)

    # guarda o historico completo: cada cobranca emitida fica registrada
    historico.append(aceite)
    _update_ent(ENT, p["id"], {"status": "aceita", "aceite": aceite,
                               "aceites": historico[-20:]})
    print("[proposta_lp] aceite %s por %s (%s) simulado=%s" %
          (slug, nome, doc[:4] + "…", not CHECKOUT_LIVE))
    return {"success": True, "url": aceite["url"], "simulado": not CHECKOUT_LIVE}


@router.get("/p/{slug}/simulacao")
def pagina_simulacao(slug: str):
    p = _por_slug(slug)
    if not p:
        return HTMLResponse(_NAO_ENCONTRADA, status_code=404)
    a = p.get("aceite") or {}
    unica = (a.get("cobranca") or p.get("cobranca")) == "unica"
    linhas = [
        ("Proposta", "%s (%s)" % (p.get("cliente_nome") or "", p.get("slug") or "")),
        ("Tipo", "Cobrança única" if unica else "Assinatura mensal recorrente"),
        ("Valor", "R$ %s%s" % (_num_br(a.get("valor") or p.get("valor_mensal")),
                               "" if unica else " por mês")),
        ("1º vencimento", _data_br((datetime.now() + timedelta(days=VENC_DIAS)).strftime("%Y-%m-%d"))),
        ("Pagador", a.get("nome") or "-"),
        ("CPF/CNPJ", a.get("doc") or "-"),
        ("E-mail", a.get("email") or "-"),
        ("WhatsApp", a.get("whatsapp") or "-"),
    ]
    tr = "".join("<tr><td>%s</td><td><b>%s</b></td></tr>" % (_esc(k), _esc(v)) for k, v in linhas)
    return HTMLResponse(_SIMULACAO_TPL.replace("__LINHAS__", tr).replace("__SLUG__", _esc(slug)),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


_SIMULACAO_TPL = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Corexia - Simulação</title>
<meta name="robots" content="noindex,nofollow"><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0D0D0D;color:#F5F5F3;
font-family:Inter,system-ui,sans-serif;padding:28px}
.box{width:min(560px,100%);background:#1A1A1A;border:1px solid #2A2A2A;border-radius:16px;padding:32px}
.tag{display:inline-block;font-size:.72rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase;
color:#FFB020;background:rgba(255,176,32,.12);border:1px solid rgba(255,176,32,.35);padding:6px 12px;border-radius:999px}
h1{font-size:21px;margin:18px 0 8px}p{color:#A6A6A0;line-height:1.6;margin:0 0 22px}
table{width:100%;border-collapse:collapse;font-size:.95rem}
td{padding:11px 0;border-bottom:1px solid #2A2A2A}td:first-child{color:#A6A6A0}td:last-child{text-align:right}
tr:last-child td{border-bottom:none}
a{display:inline-block;margin-top:24px;color:#FF5C1A;text-decoration:none;font-weight:700}
</style></head><body><div class="box">
<span class="tag">Simulação</span>
<h1>Nenhuma cobrança real foi criada</h1>
<p>O fluxo rodou inteiro e os dados abaixo foram gravados na proposta. É exatamente isto que
seria enviado ao Asaas quando o checkout for liberado.</p>
<table>__LINHAS__</table>
<a href="/p/__SLUG__">&larr; Voltar para a proposta</a>
</div></body></html>"""


# ---------------------------------------------------------------- LP publica
_CSS = """
<style id="cxp-style">
  .cxp-sec{position:relative;padding:120px 0;background:linear-gradient(180deg,var(--bg-soft),var(--bg));
    border-top:1px solid var(--card-border);overflow:hidden}
  .cxp-sec::before{content:"";position:absolute;left:50%;top:-200px;transform:translateX(-50%);
    width:780px;height:440px;border-radius:50%;
    background:radial-gradient(closest-side,var(--accent-soft),transparent);pointer-events:none}
  .cxp-sec .container{position:relative}
  .cxp-head{max-width:780px}
  .cxp-h2{font-size:clamp(28px,3.6vw,44px);margin:0 0 14px}
  .cxp-sub{color:var(--text-dim);font-size:1.05rem;line-height:1.65;margin:0 0 46px}
  .cxp-grid{display:grid;grid-template-columns:minmax(0,380px) minmax(0,1fr);gap:26px;align-items:start}
  @media(max-width:900px){.cxp-grid{grid-template-columns:1fr}.cxp-sec{padding:88px 0}}
  .cxp-card{background:var(--card);border:1px solid var(--card-border);border-radius:var(--radius);padding:32px}
  .cxp-price{border-color:rgba(255,92,26,.38);box-shadow:0 22px 60px -30px var(--accent-glow)}
  .cxp-badge{display:inline-block;font-size:.72rem;font-weight:800;letter-spacing:.12em;
    text-transform:uppercase;color:var(--accent);background:var(--accent-soft);
    border:1px solid rgba(255,92,26,.3);padding:6px 12px;border-radius:999px}
  .cxp-val{display:flex;align-items:baseline;gap:7px;margin:24px 0 8px;font-weight:800;letter-spacing:-.03em}
  .cxp-cur{font-size:1.45rem;color:var(--text-dim)}
  .cxp-val strong{font-size:clamp(46px,7vw,66px);line-height:1}
  .cxp-per{font-size:1.02rem;color:var(--text-dim);font-weight:600}
  .cxp-note{color:var(--text-dim);font-size:.95rem;line-height:1.6;margin:0 0 26px}
  .cxp-price .btn{width:100%}
  .cxp-meta{margin-top:16px;text-align:center;font-size:.84rem;color:var(--text-dim)}
  .cxp-entregas h3{font-size:1.16rem;margin:0 0 22px}
  .cxp-list{list-style:none;margin:0;padding:0;display:grid;gap:15px}
  .cxp-list li{display:flex;gap:13px;align-items:flex-start;line-height:1.55;font-size:.98rem}
  .cxp-list li::before{content:"\\2713";flex:none;width:22px;height:22px;display:grid;place-items:center;
    border-radius:50%;background:var(--accent-soft);border:1px solid rgba(255,92,26,.45);
    color:var(--accent);font-size:.72rem;font-weight:800;margin-top:1px}
  .cxp-obs{margin:26px 0 0;padding-top:22px;border-top:1px solid var(--card-border);
    color:var(--text-dim);font-size:.93rem;line-height:1.65}
  .cxp-for{margin-top:34px;padding:18px 22px;border:1px dashed var(--card-border);border-radius:12px;
    color:var(--text-dim);font-size:.92rem;display:flex;flex-wrap:wrap;gap:8px 26px;align-items:center}
  .cxp-for b{color:var(--text)}
  .cxp-alt{display:block;margin-top:14px;text-align:center;color:var(--text-dim);font-size:.9rem;
    text-decoration:none;border-bottom:1px solid transparent;transition:color .2s,border-color .2s}
  .cxp-alt:hover{color:var(--accent);border-color:var(--accent)}
  /* ---- modal de checkout ---- */
  .cxp-ov{position:fixed;inset:0;z-index:9000;display:none;align-items:flex-start;justify-content:center;
    background:rgba(0,0,0,.72);backdrop-filter:blur(4px);padding:6vh 18px;overflow:auto}
  .cxp-ov.on{display:flex}
  .cxp-modal{position:relative;width:min(460px,100%);background:var(--card);
    border:1px solid var(--card-border);border-radius:var(--radius);padding:32px}
  .cxp-x{position:absolute;top:14px;right:16px;background:none;border:none;color:var(--text-dim);
    font-size:26px;line-height:1;cursor:pointer;padding:4px 8px;border-radius:8px}
  .cxp-x:hover{color:var(--text);background:rgba(255,255,255,.06)}
  .cxp-modal h3{font-size:1.3rem;margin:0 0 6px}
  .cxp-resumo{display:flex;flex-direction:column;gap:2px;margin:0 0 22px}
  .cxp-resumo strong{font-size:1.7rem;font-weight:800;letter-spacing:-.02em;color:var(--accent)}
  .cxp-resumo span{color:var(--text-dim);font-size:.88rem}
  .cxp-f{margin-bottom:14px}
  .cxp-f label{display:block;font-size:.76rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
    color:var(--text-dim);margin-bottom:6px}
  .cxp-f input{width:100%;box-sizing:border-box;background:var(--bg-soft);border:1px solid var(--card-border);
    border-radius:10px;color:var(--text);padding:12px 14px;font-size:1rem;font-family:inherit}
  .cxp-f input:focus{outline:none;border-color:var(--accent)}
  .cxp-erro{display:none;margin:0 0 14px;padding:11px 14px;border-radius:10px;font-size:.9rem;
    color:#FFB4A0;background:rgba(255,92,26,.1);border:1px solid rgba(255,92,26,.35)}
  .cxp-modal .btn{width:100%}
  .cxp-modal .btn:disabled{opacity:.65;cursor:progress}
  .cxp-seg{margin:16px 0 0;font-size:.8rem;line-height:1.55;color:var(--text-dim);text-align:center}
  @media(max-width:520px){.cxp-modal{padding:26px 20px}}
</style>
"""

_SECAO_TPL = """
    <!-- ===================== PROPOSTA PERSONALIZADA ===================== -->
    <section id="proposta" class="cxp-sec">
      <div class="container">
        <div class="cxp-head">
          <span class="eyebrow">Proposta comercial</span>
          <h2 class="cxp-h2">__TITULO__</h2>
          <p class="cxp-sub">__SUBTITULO__</p>
        </div>
        <div class="cxp-grid">
          <div class="cxp-card cxp-price">
            <span class="cxp-badge">Acesso completo</span>
            <div class="cxp-val"><span class="cxp-cur">R$</span><strong>__VALOR__</strong><span class="cxp-per">__PERIODO__</span></div>
            <p class="cxp-note">Plano completo do sistema Corexia, com tudo o que está listado ao lado.</p>
            __CTA__
            __VALIDADE__
          </div>
          <div class="cxp-card cxp-entregas">
            <h3>O que está incluso nesta proposta</h3>
            <ul class="cxp-list">__ENTREGAS__</ul>
            __OBS__
          </div>
        </div>
        __RODAPE__
      </div>
    </section>
__MODAL__
"""


def _cta_html(p):
    """Botao principal -> checkout (pagamento). WhatsApp vira alternativa secundaria."""
    rotulo = _esc(p.get("cta_label") or "Aceitar e ir para o pagamento")
    out = ['<button class="btn btn-primary btn-lg" onclick="cxpAbrir()">%s</button>' % rotulo]

    wa = "".join(ch for ch in (p.get("whatsapp") or "") if ch.isdigit())
    if wa:
        if not wa.startswith("55"):
            wa = "55" + wa
        txt = "Olá! Quero falar sobre a proposta Corexia (%s)." % (p.get("slug") or "")
        out.append('<a class="cxp-alt" target="_blank" rel="noopener" href="https://wa.me/%s?text=%s">'
                   'Prefiro falar com um consultor</a>' % (wa, quote(txt)))
    return "".join(out)


def _modal_html(p):
    unica = (p.get("cobranca") or "assinatura") == "unica"
    resumo = "R$ %s%s" % (_num_br(p.get("valor_mensal")),
                          "" if unica else (" %s" % _esc(p.get("periodo") or PERIODO_PADRAO)))
    tipo = "Cobrança única" if unica else "Assinatura mensal — cancele quando quiser"
    return (_MODAL_TPL
            .replace("__SLUG__", _esc(p.get("slug") or ""))
            .replace("__RESUMO__", resumo)
            .replace("__TIPO__", tipo))


_MODAL_TPL = """
    <div class="cxp-ov" id="cxpOv" role="dialog" aria-modal="true" aria-labelledby="cxpTit">
      <div class="cxp-modal">
        <button class="cxp-x" type="button" onclick="cxpFecha()" aria-label="Fechar">&times;</button>
        <h3 id="cxpTit">Aceitar proposta</h3>
        <p class="cxp-resumo"><strong>__RESUMO__</strong><span>__TIPO__</span></p>
        <div class="cxp-f"><label for="cxpNome">Nome ou razão social</label>
          <input id="cxpNome" autocomplete="organization"></div>
        <div class="cxp-f"><label for="cxpDoc">CPF ou CNPJ</label>
          <input id="cxpDoc" inputmode="numeric" autocomplete="off"></div>
        <div class="cxp-f"><label for="cxpEmail">E-mail</label>
          <input id="cxpEmail" type="email" autocomplete="email"></div>
        <div class="cxp-f"><label for="cxpWhats">WhatsApp</label>
          <input id="cxpWhats" inputmode="numeric" autocomplete="tel"></div>
        <div class="cxp-erro" id="cxpErro"></div>
        <button class="btn btn-primary" type="button" id="cxpGo" onclick="cxpEnviar()">Ir para o pagamento</button>
        <p class="cxp-seg">Pagamento processado pelo <strong>Asaas</strong> &mdash; PIX, boleto ou cartão.
          Seus dados são usados apenas para emitir a cobrança.</p>
      </div>
    </div>
    <script>
      (function () {
        var SLUG = "__SLUG__", enviando = false;
        function el(i) { return document.getElementById(i); }
        window.cxpAbrir = function () {
          el("cxpOv").classList.add("on"); document.body.style.overflow = "hidden";
          setTimeout(function () { el("cxpNome").focus(); }, 60);
        };
        window.cxpFecha = function () {
          el("cxpOv").classList.remove("on"); document.body.style.overflow = "";
        };
        el("cxpOv").addEventListener("click", function (e) { if (e.target === el("cxpOv")) cxpFecha(); });
        document.addEventListener("keydown", function (e) {
          if (e.key === "Escape" && el("cxpOv").classList.contains("on")) cxpFecha();
        });
        function erro(m) { var d = el("cxpErro"); d.textContent = m || ""; d.style.display = m ? "block" : "none"; }
        window.cxpEnviar = function () {
          if (enviando) return;
          var b = {
            nome: el("cxpNome").value.trim(), doc: el("cxpDoc").value.trim(),
            email: el("cxpEmail").value.trim(), whatsapp: el("cxpWhats").value.trim()
          };
          var d = b.doc.replace(/\\D/g, "");
          if (b.nome.length < 3) return erro("Informe o nome ou a razão social.");
          if (d.length !== 11 && d.length !== 14) return erro("CPF ou CNPJ inválido.");
          if (b.email.indexOf("@") < 0) return erro("Informe um e-mail válido.");
          erro(""); enviando = true;
          var btn = el("cxpGo"); var txt = btn.textContent;
          btn.textContent = "Gerando pagamento..."; btn.disabled = true;
          fetch("/api/proposta/" + encodeURIComponent(SLUG) + "/checkout", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b)
          }).then(function (r) {
            return r.json().then(function (j) { return { ok: r.ok, j: j }; });
          }).then(function (x) {
            if (!x.ok || !x.j.url) throw new Error((x.j && x.j.error) || "Não foi possível gerar o pagamento.");
            window.location.href = x.j.url;
          }).catch(function (e) {
            erro(e.message); enviando = false; btn.textContent = txt; btn.disabled = false;
          });
        };
      })();
    </script>
"""


def _secao_html(p):
    cliente = _esc(p.get("cliente_nome") or "")
    provedor = _esc(p.get("provedor_nome") or "")

    titulo = p.get("titulo") or ("Proposta preparada para %s" % (p.get("cliente_nome") or ""))
    subtitulo = p.get("subtitulo") or (
        "Este é o valor fechado para colocar a Corexia rodando na sua operação: "
        "um único plano, sem surpresa e sem cobrança por módulo.")

    entregas = "".join("<li>%s</li>" % _esc(x) for x in (p.get("entregas") or ENTREGAS_PADRAO))

    val = _data_br(p.get("validade"))
    validade = ('<div class="cxp-meta">Proposta válida até %s</div>' % val) if val else ""

    obs = p.get("observacao") or ""
    obs_html = ('<p class="cxp-obs">%s</p>' % _esc(obs)) if obs else ""

    rod = []
    if cliente:
        rod.append("Preparada para <b>%s</b>" % cliente)
    if provedor:
        rod.append("Provedor <b>%s</b>" % provedor)
    rod.append("Referência <b>%s</b>" % _esc(p.get("slug") or ""))
    rodape = '<div class="cxp-for">%s</div>' % "".join("<span>%s</span>" % x for x in rod)

    return (_SECAO_TPL
            .replace("__TITULO__", _esc(titulo))
            .replace("__SUBTITULO__", _esc(subtitulo))
            .replace("__VALOR__", _num_br(p.get("valor_mensal", VALOR_PADRAO)))
            .replace("__PERIODO__", _esc(p.get("periodo") or PERIODO_PADRAO))
            .replace("__CTA__", _cta_html(p))
            .replace("__VALIDADE__", validade)
            .replace("__ENTREGAS__", entregas)
            .replace("__OBS__", obs_html)
            .replace("__RODAPE__", rodape)
            .replace("__MODAL__", _modal_html(p)))


def _injeta(html, p):
    cliente = p.get("cliente_nome") or ""
    # 1) head: css do bloco + noindex (proposta nao pode cair no Google)
    html = html.replace("</head>", _CSS + '<meta name="robots" content="noindex,nofollow">\n</head>', 1)
    # 2) titulo da aba
    html = re.sub(r"<title>.*?</title>",
                  "<title>%s</title>" % _esc("Proposta Corexia - %s" % cliente if cliente else "Proposta Corexia"),
                  html, count=1, flags=re.S)
    # 3) link no menu (1a ocorrencia = nav do topo; a 2a e o rodape, fica intacta)
    html = html.replace('<li><a href="#clientes">Clientes</a></li>',
                        '<li><a href="#clientes">Clientes</a></li>'
                        '<li><a href="#proposta">Investimento</a></li>', 1)
    # 4) o bloco entra no fim da LP, logo antes do </main>
    html = html.replace("</main>", _secao_html(p) + "\n  </main>", 1)
    return html


_NAO_ENCONTRADA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Corexia</title>
<meta name="robots" content="noindex,nofollow"><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0D0D0D;color:#F5F5F3;
font-family:Inter,system-ui,sans-serif;text-align:center;padding:24px}
h1{font-size:22px;margin:0 0 10px}p{color:#A6A6A0;margin:0 0 22px;line-height:1.6}
a{color:#FF5C1A;text-decoration:none;font-weight:700}</style></head><body><div>
<h1>Proposta não encontrada</h1>
<p>Este link de proposta não existe mais ou foi desativado.<br>Fale com o seu contato comercial.</p>
<a href="/lp">Conhecer a Corexia &rarr;</a></div></body></html>"""


@router.get("/p/{slug}")
def pagina_publica(slug: str):
    p = _por_slug(slug)
    if not p or not p.get("ativo", True):
        return HTMLResponse(_NAO_ENCONTRADA, status_code=404)
    try:
        with open(LP_INDEX, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        print("[proposta_lp] falha lendo a LP:", e)
        return HTMLResponse("<h1>Corexia</h1><p>LP indisponível no momento.</p>", status_code=500)
    return HTMLResponse(_injeta(html, p),
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


# ---------------------------------------------------------------- corpo da tela admin
_BODY = """
<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px">
 <div style="flex:1;color:var(--muted);font-size:13px">
  Gera um link da LP oficial <b>com preco</b>, personalizado por cliente. A LP publica (/lp) nao muda.</div>
 <button class="btn-primary" onclick="novo()">+ Nova Proposta</button></div>
<div id="modo" style="display:none;margin-bottom:16px;padding:12px 16px;border-radius:10px;font-size:13px"></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Propostas</div><div class="v" id="k_tot">-</div></div>
 <div class="kpi"><div class="k">Ativas</div><div class="v" id="k_ativ" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Aceitas</div><div class="v" id="k_ace" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Ticket medio</div><div class="v" id="k_tic">-</div></div></div>
<table><thead><tr><th>Cliente</th><th>Provedor</th><th>Valor</th><th>Link</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>

<div class="ov" id="ov"><div class="modal" style="max-width:680px">
 <h2 id="mtitle">Nova Proposta Personalizada</h2><input type="hidden" id="f_id">
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:4px 0 10px">Quem vai receber</div>
 <div class="two"><div class="fld"><label>Nome do cliente *</label><input id="f_cliente" placeholder="Ex.: Supermercado Sao Jorge"></div>
  <div class="fld"><label>Nome do provedor</label><input id="f_provedor" placeholder="Ex.: Viggia Telecom"></div></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:14px 0 10px">Valor</div>
 <div class="two"><div class="fld"><label>Valor mensal (R$)</label><input id="f_valor" type="number" step="0.01" value="797"></div>
  <div class="fld"><label>Periodo</label><input id="f_periodo" value="/m&ecirc;s"></div></div>
 <div class="two"><div class="fld"><label>Tipo de cobranca</label><select id="f_cobranca">
   <option value="assinatura">Assinatura mensal recorrente</option>
   <option value="unica">Cobranca unica</option></select></div>
  <div class="fld"><label>Valido ate</label><input id="f_validade" type="date"></div></div>
 <div class="fld"><label>WhatsApp (alternativa ao pagamento, opcional)</label><input id="f_whats" placeholder="DDD + numero"></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:14px 0 10px">Entregas (uma por linha)</div>
 <div class="fld"><textarea id="f_entregas" rows="8" style="width:100%;font-family:inherit;font-size:14px;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);color:var(--ink);resize:vertical"></textarea></div>
 <div class="fld"><label>Observacao (opcional)</label><input id="f_obs" placeholder="Ex.: instalacao inclusa nos 3 primeiros meses"></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:14px 0 10px">Avancado</div>
 <div class="two"><div class="fld"><label>Slug do link</label><input id="f_slug" placeholder="deixe vazio = gera sozinho (ex.: lajsk29)"></div>
  <div class="fld"><label>Texto do botao</label><input id="f_cta" placeholder="Aceitar proposta"></div></div>
 <div class="two"><div class="fld"><label>Titulo (opcional)</label><input id="f_titulo" placeholder="deixe vazio = Proposta preparada para <cliente>"></div>
  <div class="fld"><label>Status</label><select id="f_status">
   <option value="rascunho">Rascunho</option><option value="enviada">Enviada</option><option value="aceita">Aceita</option></select></div></div>
 <div class="fld"><label>Subtitulo (opcional)</label><input id="f_subtitulo" placeholder="deixe vazio = texto padrao"></div>
 <div class="fld"><label>Ativa</label><select id="f_ativo"><option value="true">Sim - link no ar</option><option value="false">Nao - link desativado</option></select></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar proposta</button></div>
</div></div>

<script>
var PADRAO_ENTREGAS=[], PROPS=[];
function fecha(){$('ov').classList.remove('open');}
function linkDe(s){return location.origin+'/p/'+s;}

function novo(){
 $('mtitle').textContent='Nova Proposta Personalizada';
 $('f_id').value='';$('f_cliente').value='';$('f_provedor').value='';
 $('f_valor').value='797';$('f_periodo').value='/m\\u00eas';$('f_validade').value='';$('f_whats').value='';
 $('f_entregas').value=PADRAO_ENTREGAS.join('\\n');
 $('f_obs').value='';$('f_slug').value='';$('f_cta').value='';$('f_titulo').value='';$('f_subtitulo').value='';
 $('f_cobranca').value='assinatura';$('f_status').value='rascunho';$('f_ativo').value='true';
 $('ov').classList.add('open');
}

function editar(id){
 var p=PROPS.filter(function(x){return x.id===id;})[0]; if(!p)return;
 $('mtitle').textContent='Editar Proposta';
 $('f_id').value=p.id;$('f_cliente').value=p.cliente_nome||'';$('f_provedor').value=p.provedor_nome||'';
 $('f_valor').value=p.valor_mensal;$('f_periodo').value=p.periodo||'/m\\u00eas';$('f_validade').value=p.validade||'';
 $('f_whats').value=p.whatsapp||'';$('f_entregas').value=(p.entregas||[]).join('\\n');
 $('f_obs').value=p.observacao||'';$('f_slug').value=p.slug||'';$('f_cta').value=p.cta_label||'';
 $('f_titulo').value=p.titulo||'';$('f_subtitulo').value=p.subtitulo||'';
 $('f_cobranca').value=p.cobranca||'assinatura';
 $('f_status').value=p.status||'rascunho';$('f_ativo').value=(p.ativo===false?'false':'true');
 $('ov').classList.add('open');
}

async function salvar(){
 var b={id:$('f_id').value,cliente_nome:$('f_cliente').value.trim(),provedor_nome:$('f_provedor').value.trim(),
  valor_mensal:parseFloat($('f_valor').value||0)||0,periodo:$('f_periodo').value.trim(),
  validade:$('f_validade').value,whatsapp:$('f_whats').value.trim(),entregas:$('f_entregas').value,
  observacao:$('f_obs').value.trim(),slug:$('f_slug').value.trim().toLowerCase(),
  cta_label:$('f_cta').value.trim(),titulo:$('f_titulo').value.trim(),subtitulo:$('f_subtitulo').value.trim(),
  cobranca:$('f_cobranca').value,status:$('f_status').value,ativo:$('f_ativo').value==='true'};
 if(!b.cliente_nome){msg('Informe o nome do cliente');return;}
 try{ var r=await api('POST','/api/comercial/proposta-lp/salvar',b);
  fecha(); msg('Proposta salva. Link: '+linkDe(r.slug),true); load();
 }catch(e){ msg('Erro ao salvar: '+e.message); }
}

async function excluir(id){
 if(!confirm('Excluir esta proposta? O link para de funcionar.'))return;
 try{ await api('POST','/api/comercial/proposta-lp/'+id+'/excluir'); msg('Excluida.',true); load(); }
 catch(e){ msg('Erro: '+e.message); }
}

function copiar(s){
 var u=linkDe(s);
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(u).then(function(){msg('Link copiado: '+u,true);},function(){prompt('Copie o link:',u);});}
 else{prompt('Copie o link:',u);}
}

async function load(){
 try{
  var d=await api('GET','/api/comercial/proposta-lp');
  PROPS=d.propostas||[]; PADRAO_ENTREGAS=d.entregas_padrao||[];
  var mo=$('modo'); mo.style.display='block';
  if(d.checkout_live){ mo.style.background='rgba(255,92,26,.10)'; mo.style.border='1px solid rgba(255,92,26,.45)'; mo.style.color='var(--ink)';
   mo.innerHTML='<b>Checkout REAL ligado.</b> Ao aceitar, o cliente gera cobranca de verdade no Asaas (1o vencimento em '+(d.venc_dias||3)+' dias).'; }
  else { mo.style.background='rgba(255,176,32,.10)'; mo.style.border='1px solid rgba(255,176,32,.40)'; mo.style.color='var(--ink)';
   mo.innerHTML='<b>Checkout em SIMULACAO.</b> O fluxo roda inteiro e grava o aceite, mas <b>nada e criado no Asaas</b>. Para liberar cobranca real: <code>PROPOSTA_CHECKOUT_LIVE=1</code> no .env + restart do backend.'; }
  var ativas=PROPS.filter(function(p){return p.ativo!==false;}).length;
  var aceitas=PROPS.filter(function(p){return p.status==='aceita';}).length;
  var soma=PROPS.reduce(function(a,p){return a+(parseFloat(p.valor_mensal)||0);},0);
  $('k_tot').textContent=PROPS.length; $('k_ativ').textContent=ativas; $('k_ace').textContent=aceitas;
  $('k_tic').textContent=PROPS.length?brl(soma/PROPS.length):'-';
  if(!PROPS.length){$('rows').innerHTML='<tr><td colspan="6" class="center">nenhuma proposta ainda - clique em "+ Nova Proposta"</td></tr>';return;}
  $('rows').innerHTML=PROPS.map(function(p){
   var cor=p.status==='aceita'?'var(--ok)':(p.status==='enviada'?'var(--accent)':'var(--muted)');
   var off=p.ativo===false?' <span style="color:var(--muted)">(desativada)</span>':'';
   var a=p.aceite||null, n=(p.aceites||[]).length, ac='';
   if(a){ ac='<div style="color:var(--muted);font-size:11px;margin-top:3px">'+esc(a.nome||'')
     +(a.simulado?' <span style="color:#FFB020">(simulado)</span>':'')
     +(a.url?' &middot; <a href="'+esc(a.url)+'" target="_blank" style="color:var(--accent)">cobranca</a>':'')
     +(n>1?' &middot; <b>'+n+' aceites</b>':'')+'</div>'; }
   var tipo=p.cobranca==='unica'?'unica':'mensal';
   return '<tr><td>'+esc(p.cliente_nome||'')+'</td><td>'+esc(p.provedor_nome||'-')+'</td>'
    +'<td>'+brl(p.valor_mensal)+' <span style="color:var(--muted)">'+esc(p.periodo||'/m\\u00eas')+'</span>'
    +'<div style="color:var(--muted);font-size:11px">'+tipo+'</div></td>'
    +'<td><a href="/p/'+encodeURIComponent(p.slug)+'" target="_blank" style="color:var(--accent);font-family:var(--mono)">/p/'+esc(p.slug)+'</a></td>'
    +'<td style="color:'+cor+'">'+esc(p.status||'rascunho')+off+ac+'</td>'
    +'<td style="text-align:right;white-space:nowrap">'
    +'<button onclick="copiar(\\''+esc(p.slug)+'\\')">copiar link</button> '
    +'<button onclick="editar(\\''+p.id+'\\')">editar</button> '
    +'<button onclick="excluir(\\''+p.id+'\\')">excluir</button></td></tr>';
  }).join('');
 }catch(e){ $('rows').innerHTML='<tr><td colspan="6" class="center">erro: '+esc(e.message)+'</td></tr>'; }
}
window.PAGE_INIT=load;
</script>
"""
