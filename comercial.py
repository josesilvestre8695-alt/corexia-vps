"""
Corexia — modulo COMERCIAL (opcao 3): telas proprias servidas pelo server.py,
sem tocar no SPA React (que nao tem fonte na maquina).

- Paginas em /comercial/*  (HTML a mao, MESMA cara do painel, reusam corexia_token)
- API em /api/comercial/*  (prefixo proprio; nao conflita com /api/functions do SPA)
- "Um menu so": o server.py injeta COMERCIAL_BRIDGE_JS no index.html do SPA (em tempo de
  resposta; nao altera o arquivo em disco) -> os itens comerciais do menu do SPA abrem estas telas.

Incluido pelo server.py: from comercial import router as _com_router; app.include_router(_com_router)
"""
import os, sqlite3, json, time, secrets, base64
from datetime import datetime, timedelta
import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "corexia.db")
load_dotenv(os.path.join(HERE, ".env"))
try:
    import asaas  # modulo Asaas (customer/subscription), ja testado
except Exception:
    asaas = None

ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE_ID", "")
ZAPI_TOKEN    = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT   = os.getenv("ZAPI_CLIENT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")   # o detector usa p/ ingerir o mapa de calor
# SEGURANCA $$: a assinatura so cria cobranca REAL no Asaas quando COMERCIAL_ASAAS_LIVE=1.
# Desligado (padrao) = modo teste: valida o fluxo inteiro sem gerar assinatura/cobranca real.
ASAAS_LIVE = os.getenv("COMERCIAL_ASAAS_LIVE", "0") == "1"

router = APIRouter()


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _num(n):
    d = "".join(c for c in str(n or "") if c.isdigit())
    return d if d.startswith("55") else "55" + d


def _zapi_send(numero, texto, inst=None, tok=None, cli=None):
    """Envia WhatsApp via Z-API (padrao Corexia ou do provedor, se passado)."""
    inst = inst or ZAPI_INSTANCE; tok = tok or ZAPI_TOKEN; cli = cli or ZAPI_CLIENT
    if not (inst and tok):
        return False, "zapi nao configurado"
    try:
        r = requests.post("https://api.z-api.io/instances/%s/token/%s/send-text" % (inst, tok),
                          timeout=25, headers={"Content-Type": "application/json", "Client-Token": cli or ""},
                          json={"phone": _num(numero), "message": texto})
        return r.ok, (r.text or "")[:150]
    except Exception as e:
        return False, str(e)[:150]

# (slug, label, titulo, roles_que_veem_no_menu)
# admin (Corexia): os "clientes" da Corexia SAO os provedores -> admin NAO tem aba Clientes.
# provedor: gerencia os clientes finais DELE -> tem Clientes, nao tem Provedor/Revenda nem Planos.
COMERCIAL_ITENS = [
    ("propostas",       "Propostas",         "Propostas Comerciais",  ("admin", "provedor")),
    ("proposta-lp",     "Proposta Personalizada", "Proposta Personalizada", ("admin",)),
    ("planos",          "Planos",            "Gerenciar Planos",      ("admin",)),
    ("clientes",        "Provedor/Revenda",  "Provedor/Revenda",      ("admin",)),
    ("analiticos",      "Controle de IA (Global)", "Controle de IA (Global)", ("admin",)),
    ("exclusoes",       "Exclusoes de Camera", "Exclusoes de Camera",   ("admin",)),
    ("demonstrador",    "Usuario Demonstrador", "Usuario Demonstrador",  ("admin",)),
    ("tester",          "Provedor Tester",   "Provedor/Revenda Tester", ("admin",)),
    ("chamados",        "Chamados",          "Chamados dos Provedores", ("admin",)),
    ("heatmap",         "Mapa de Calor",     "Mapa de Calor",         ("admin",)),
    ("faturas",         "Faturas",           "Faturas",               ("admin", "provedor")),
    ("contratos",       "Contratos",         "Contratos",             ("admin", "provedor")),
    ("vendedores",      "Vendedores",        "Vendedores",            ("admin", "provedor")),
    ("comissionamento", "Comissionamento",   "Comissionamento",       ("admin", "provedor")),
    ("contas-pagar",    "Contas a Pagar",    "Contas a Pagar",        ("admin", "provedor")),
]
_LABEL_BY_SLUG = {s: lbl for s, lbl, _t, _r in COMERCIAL_ITENS}

# ponte: textos EXATOS do menu do SPA -> minha rota. A aba "Provedor/Revenda" e servida em
# /comercial/clientes; tanto "Provedores" quanto "Clientes" do SPA abrem essa mesma pagina.
_SPA_MENU = {"Propostas": "propostas", "Planos": "planos", "Clientes": "clientes",
             "Provedores": "clientes", "Faturas": "faturas", "Contratos": "contratos",
             "Vendedores": "vendedores", "Comissionamento": "comissionamento", "Contas a Pagar": "contas-pagar",
             "Analíticos por Câmera": "analiticos", "Analiticos por Camera": "analiticos",
             "Mapa de Calor": "heatmap", "Mapa de calor": "heatmap", "Heatmap": "heatmap"}
_MAP = ", ".join('"%s":"/comercial/%s"' % (lbl, slug) for lbl, slug in _SPA_MENU.items())
# itens do menu do SPA a ESCONDER (comparacao sem acento, minusculo, texto EXATO)
_HIDE_MENU = ["cameras", "clientes", "contratacoes", "estatisticas", "visualizar cameras", "gravacoes",
              "central de alertas", "suporte", "mosaicos", "visualizar mosaicos", "rastreamento", "visitas",
              "configuracao de servicos", "config. servicos", "config servicos", "config. de servicos",
              "ponto eletronico", "agendamentos", "ordens de servico", "crm", "marketing"]
_HIDE_JS = "[" + ",".join('"%s"' % x for x in _HIDE_MENU) + "]"
# switcher fixo dos 3 paineis do provedor (Monitoramento / Operacional / Cliente)
_PANEL_SWITCHER_JS = """<script>/* corexia-switch */(function(){
 function build(){ if(document.getElementById('cx-sw'))return;
  var p=location.pathname; var cur=(p.indexOf('/monitoramento')===0)?'monit':((p.indexOf('/provedor')===0)?'op':((p.indexOf('/portal')===0)?'cli':'dash'));
  function mk(h,l,on){return '<a href="'+h+'" style="text-decoration:none;border-radius:999px;padding:5px 12px;font-weight:600;white-space:nowrap;'+(on?'background:#f97316;color:#1a1205':'color:#e8ecf2')+'">'+l+'</a>';}
  var d=document.createElement('div'); d.id='cx-sw';
  d.style.cssText='position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:2147483000;display:flex;gap:4px;align-items:center;flex-wrap:wrap;justify-content:center;max-width:96vw;background:#171a21;border:1px solid #2a2f3a;border-radius:999px;padding:5px 8px;font:12px system-ui,-apple-system,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.45)';
  d.innerHTML='<span style="color:#8b95a7;padding:0 6px 0 4px">Painel</span>'+mk('/','Dashboard Geral',cur==='dash')+mk('/monitoramento','Monitoramento',cur==='monit')+mk('/provedor','Operacional',cur==='op');
  document.body.appendChild(d); }
 function go(){ var p=location.pathname; if(p.indexOf('/provedor')===0||p.indexOf('/monitoramento')===0){build();return;} var t=localStorage.getItem('corexia_token'); if(!t)return;
  fetch('/api/comercial/ping',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.json();}).then(function(x){ if(x&&x.user&&x.user.role==='provedor')build(); }).catch(function(){}); }
 if(document.body)go(); else document.addEventListener('DOMContentLoaded',go);
})();</script>"""
# alerta chamativo p/ ADMIN quando ha pedidos de exclusao de camera pendentes
_ADMIN_ALERT_JS = """<script>/* corexia-adminalert */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 function chk(){ fetch('/api/comercial/exclusoes',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.ok?r.json():null;}).then(function(j){
   var e=document.getElementById('cx-alert');
   if(!j||!j.pendentes||!j.pendentes.length){ if(e&&e.parentNode)e.parentNode.removeChild(e); return; }
   if(!e){ e=document.createElement('a'); e.id='cx-alert'; e.href='/comercial/exclusoes';
     e.style.cssText='position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483001;background:#f87171;color:#1a0505;font:700 13px system-ui,sans-serif;padding:9px 16px;border-radius:999px;text-decoration:none;box-shadow:0 8px 30px rgba(0,0,0,.5);max-width:92vw'; document.body.appendChild(e); }
   var p=j.pendentes[0];
   e.textContent='\\u26A0 '+j.pendentes.length+' pedido(s) de exclusao de camera - '+(p.provedor_nome||'')+': '+(p.camera_nome||'')+' - clique p/ revisar';
 }).catch(function(){}); }
 chk(); setInterval(chk,30000);
})();</script>"""
_IA_HEALTH_JS = """<script>/* corexia-iahealth */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 function chk(){ fetch('/api/comercial/ia-health',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.ok?r.json():null;}).then(function(j){
   var e=document.getElementById('cx-iahealth');
   if(!j||j.overall==='ok'){ if(e&&e.parentNode)e.parentNode.removeChild(e); return; }
   var crit=j.overall!=='aviso';
   if(!e){ e=document.createElement('div'); e.id='cx-iahealth';
     e.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483002;font:700 13px system-ui,sans-serif;padding:10px 16px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,.4)'; document.body.appendChild(e); }
   e.style.background=crit?'#dc2626':'#f59e0b'; e.style.color=crit?'#fff':'#1a1205';
   var probs=(j.problemas||[]).map(function(p){return p.titulo;}).join('   |   ');
   e.textContent=(crit?'\\uD83D\\uDD34 IA COREXIA FORA DO NORMAL: ':'\\u26A0 IA COREXIA (aviso): ')+(probs||'verifique');
   e.title=(j.problemas||[]).map(function(p){return '- '+p.titulo+': '+p.detalhe;}).join('\\n');
 }).catch(function(){}); }
 chk(); setInterval(chk,30000);
})();</script>"""
_HIDE_PATHS = ["/cameras", "/mosaicos", "/visualizar-cameras", "/visualizar-mosaicos", "/gravacoes",
               "/clientes", "/estatisticas-alertas", "/rastreamento", "/visitas", "/alertas", "/suporte",
               "/contratacoes", "/config-servicos", "/ponto", "/agendamentos", "/ordens-servico", "/crm",
               "/marketing", "/financeiro", "/estoque", "/funcionarios", "/integracao-erp", "/fornecedores"]
_HIDE_PATHS_JS = "[" + ",".join('"%s"' % p for p in _HIDE_PATHS) + "]"


COMERCIAL_BRIDGE_JS = (
    "<script>/* corexia-bridge */(function(){var MAP={" + _MAP + "};"
    "document.addEventListener('click',function(e){var el=e.target;"
    "for(var i=0;i<6&&el;i++){var t=(el.textContent||'').trim();"
    "if(t.length<=24&&MAP[t]){e.preventDefault();e.stopPropagation();window.location.href=MAP[t];return;}"
    "el=el.parentElement;}},true);"
    # esconde itens de menu removidos (mantido escondido em cada re-render do React)
    "var HIDE=" + _HIDE_JS + ";var HP=" + _HIDE_PATHS_JS + ";"
    "function nrm(s){return (s||'').trim().toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');}"
    "function hideMenus(){"
    "var as=document.querySelectorAll('a[href]');"
    "for(var i=0;i<as.length;i++){var a=as[i];var h=a.getAttribute('href')||'';var p=h;"
    "try{p=new URL(h,location.origin).pathname;}catch(e){}"
    "if(p.length>1&&p.charAt(p.length-1)==='/')p=p.slice(0,-1);"
    "if(p&&HP.indexOf(p)>=0){(a.closest('li')||a).style.display='none';}}"
    "var ns=document.querySelectorAll('a,button,li,[role=menuitem]');"
    "for(var j=0;j<ns.length;j++){var el=ns[j];var t=nrm(el.textContent);"
    "if(t&&t.length<=28&&HIDE.indexOf(t)>=0){(el.closest('li')||el).style.display='none';}}}"
    "function relbl(){var ns=document.querySelectorAll('a,button,[role=menuitem],span,p,h1,h2,h3');"
    "for(var i=0;i<ns.length;i++){var el=ns[i];if(el.children.length)continue;var r=(el.textContent||'').trim();"
    "if(r==='Dashboard')el.textContent='Dashboard Geral';else if(r==='MONITORAMENTO')el.textContent='DASHBOARD GERAL';}}"
    "function hidePapel(){if(!window.__cxadmin)return;var ss=document.querySelectorAll('select');for(var i=0;i<ss.length;i++){var s=ss[i],ops=s.options,ha=false,hp=false;for(var j=0;j<ops.length;j++){var t=(ops[j].textContent||'').trim().toLowerCase();if(t==='admin')ha=true;if(t==='provedor'||t==='cliente')hp=true;}if(ha&&hp){for(var k=ops.length-1;k>=0;k--){var tt=(ops[k].textContent||'').trim().toLowerCase();if(tt==='provedor'||tt==='cliente'){ops[k].style.display='none';ops[k].disabled=true;}}var cur=((s.options[s.selectedIndex]||{}).textContent||'').trim().toLowerCase();if(cur!=='admin'){for(var m=0;m<ops.length;m++){if((ops[m].textContent||'').trim().toLowerCase()==='admin'){try{var setr=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;setr.call(s,ops[m].value);s.dispatchEvent(new Event('change',{bubbles:true}));}catch(e){s.selectedIndex=m;}break;}}}}}}"
    "var pend=false;function sched(){if(pend)return;pend=true;setTimeout(function(){pend=false;hideMenus();relbl();hidePapel();},80);}"
    "var _t0=localStorage.getItem('corexia_token');if(_t0){fetch('/api/comercial/ping',{headers:{'Authorization':'Bearer '+_t0}}).then(function(r){return r.json();}).then(function(x){window.__cxadmin=!!(x&&x.user&&x.user.role==='admin');if(window.__cxadmin)sched();}).catch(function(){});}"
    "hideMenus();relbl();hidePapel();try{var mo=new MutationObserver(sched);mo.observe(document.body||document.documentElement,{childList:true,subtree:true});}catch(e){}"
    "})();</script>"
) + _PANEL_SWITCHER_JS + _ADMIN_ALERT_JS + _IA_HEALTH_JS


# ---------- WHITE-LABEL (marca do provedor: cores via CSS vars + logo) ----------
def _hex_to_hsl(hx):
    hx = (hx or "").strip().lstrip("#")
    if len(hx) == 3:
        hx = "".join(c * 2 for c in hx)
    if len(hx) != 6:
        return ""
    try:
        r = int(hx[0:2], 16) / 255.0; g = int(hx[2:4], 16) / 255.0; b = int(hx[4:6], 16) / 255.0
    except Exception:
        return ""
    mx = max(r, g, b); mn = min(r, g, b); l = (mx + mn) / 2.0; d = mx - mn
    if d == 0:
        h = s = 0.0
    else:
        s = d / (1 - abs(2 * l - 1))
        if mx == r:
            h = ((g - b) / d) % 6
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h *= 60
        if h < 0:
            h += 360
    return "%d %d%% %d%%" % (round(h), round(s * 100), round(l * 100))


def _branding_de(prov):
    b = (prov or {}).get("branding") or {}
    return {"nome_marca": b.get("nome_marca") or (prov or {}).get("nome", ""),
            "cor": b.get("cor", ""), "cor_menu": b.get("cor_menu", ""), "logo": b.get("logo", ""),
            "cor_hsl": _hex_to_hsl(b.get("cor", "")), "menu_hsl": _hex_to_hsl(b.get("cor_menu", ""))}


def wl_style_for_host(host):
    """<style>+<script> de tema p/ um DOMINIO PROPRIO de provedor (Fase 2). '' se nao houver."""
    host = (host or "").split(":")[0].lower().strip()
    if not host or host in ("grupocorexia.com.br", "www.grupocorexia.com.br", "localhost", "127.0.0.1"):
        return ""
    try:
        c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='Provedor'").fetchall(); c.close()
    except Exception:
        return ""
    for r in rows:
        d = json.loads(r["data"])
        if (d.get("dominio") or "").lower().strip() == host:
            br = _branding_de(d); css = ""
            if br["cor_hsl"]:
                css += "--primary:%s;--ring:%s;--sidebar-primary:%s;" % (br["cor_hsl"], br["cor_hsl"], br["cor_hsl"])
            if br["menu_hsl"]:
                css += "--sidebar:%s;" % br["menu_hsl"]
            return ("<style id='wl-theme'>:root{%s}</style><script>window.__WL__=%s;</script>"
                    % (css, json.dumps({"logo": br["logo"], "nome": br["nome_marca"]})))
    return ""


WHITE_LABEL_JS = """<script>/* corexia-wl */(function(){
 function hexHsl(hx){hx=(hx||'').replace('#','');if(hx.length===3)hx=hx.split('').map(function(c){return c+c}).join('');if(hx.length!==6)return '';
  var r=parseInt(hx.substr(0,2),16)/255,g=parseInt(hx.substr(2,2),16)/255,b=parseInt(hx.substr(4,2),16)/255,mx=Math.max(r,g,b),mn=Math.min(r,g,b),l=(mx+mn)/2,d=mx-mn,h=0,s=0;
  if(d){s=d/(1-Math.abs(2*l-1));if(mx===r)h=((g-b)/d)%6;else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;if(h<0)h+=360;}
  return Math.round(h)+' '+Math.round(s*100)+'% '+Math.round(l*100)+'%';}
 function apply(b){ if(!b)return; var root=document.documentElement;
  var cor=b.cor_hsl||hexHsl(b.cor||''); if(cor){root.style.setProperty('--primary',cor);root.style.setProperty('--ring',cor);root.style.setProperty('--sidebar-primary',cor);}
  var menu=b.menu_hsl||hexHsl(b.cor_menu||''); if(menu){root.style.setProperty('--sidebar',menu);}
  if(b.nome){document.title=b.nome;}
  if(b.logo){var ims=document.querySelectorAll('img');for(var i=0;i<ims.length;i++){var s=(ims[i].getAttribute('src')||'').toLowerCase();if(s.indexOf('logo')>=0 && s.indexOf('banner')<0){if((ims[i].src||'').indexOf(b.logo)<0)ims[i].src=b.logo;}}}
 }
 var CUR=null,pend=false;
 function sched(){if(pend||!CUR)return;pend=true;setTimeout(function(){pend=false;apply(CUR);},120);}
 try{var mo=new MutationObserver(sched);mo.observe(document.documentElement,{childList:true,subtree:true});}catch(e){}
 if(window.__WL__){CUR={logo:window.__WL__.logo,nome:window.__WL__.nome};apply(CUR);}
 var done=false;
 function tryFetch(){ if(done)return; var t=localStorage.getItem('corexia_token'); if(!t)return;
  fetch('/api/comercial/branding/me',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.json();}).then(function(b){ if(b&&(b.cor||b.logo||b.cor_hsl)){done=true;CUR=b;apply(b);} }).catch(function(){}); }
 tryFetch(); setInterval(tryFetch,3000);
})();</script>"""


def _db():
    c = sqlite3.connect(DB, timeout=15); c.row_factory = sqlite3.Row; return c


def _current_user(req: Request):
    tok = (req.headers.get("authorization", "") or "").replace("Bearer ", "").strip()
    if not tok:
        return None
    c = _db()
    r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
    c.close()
    return dict(r) if (r and r["status"] == "ativo") else None


_SHELL_TPL = """<!doctype html><html lang="pt-BR" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Corexia - __TITULO__</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
 :root{--bg:hsl(222 18% 7%);--surface:hsl(222 15% 10%);--surface2:hsl(222 14% 14%);--border:hsl(222 13% 16%);--ink:hsl(210 20% 96%);--muted:hsl(215 14% 60%);--radius:.9rem;
  --accent:#f97316;--accent2:#ea580c;--ok:#34d399;--bad:#f87171;--mono:ui-monospace,Menlo,Consolas,monospace;
  --sans:Inter,ui-sans-serif,system-ui,'Segoe UI',Roboto,Arial,sans-serif}
 *{box-sizing:border-box}html,body{margin:0;height:100%}
 body{background:var(--bg);color:var(--ink);font-family:var(--sans);display:flex;min-height:100vh;letter-spacing:-.011em;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;background-image:radial-gradient(1100px 560px at 80% -10%,hsl(26 96% 54% / .08),transparent 60%),radial-gradient(820px 460px at -8% 2%,hsl(220 50% 60% / .10),transparent 55%);background-attachment:fixed}
 .side{width:264px;flex:none;background:hsl(222 16% 8%);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:16px 0}
 .side .logo{font-weight:800;color:var(--accent);font-size:20px;letter-spacing:1px;padding:6px 20px 16px}
 .side .sec{color:var(--muted);font-size:11px;font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;padding:14px 20px 6px}
 .side a.it,.side a.back{display:block;color:var(--ink);text-decoration:none;padding:10px 20px;font-size:14px;border-left:3px solid transparent}
 .side a.it{transition:background .14s,border-color .14s}.side a.it:hover{background:var(--surface2)}
 .side a.it.active{background:linear-gradient(90deg,rgba(249,115,22,.16),transparent);border-left-color:var(--accent);color:#fff;font-weight:600}
 .side a.back{color:var(--muted);margin-top:auto;border-top:1px solid var(--border)}
 .side a.back:hover{color:var(--ink)}
 .main{flex:1;min-width:0;display:flex;flex-direction:column}
 .top{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--border);background:linear-gradient(180deg,rgba(255,255,255,.02),transparent)}
 .top h1{font-size:20px;margin:0}.top .grow{flex:1}
 .content{padding:24px 26px 80px;max-width:1100px;width:100%}
 button,.btn{font-family:var(--sans);font-size:14px;border-radius:calc(var(--radius) - 4px);border:1px solid var(--border);background:var(--surface2);color:var(--ink);padding:9px 15px;cursor:pointer;transition:border-color .15s,background .15s,transform .1s}
 button:hover{border-color:var(--accent)}button:active,.btn:active{transform:translateY(1px)}.btn-primary{background:var(--accent);border-color:var(--accent);color:#1a1205;font-weight:700}.btn-primary:hover{background:var(--accent2)}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
 .kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;box-shadow:0 1px 2px rgba(0,0,0,.28)}
 .kpi .k{color:var(--muted);font-size:12px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.05em}
 .kpi .v{font-size:26px;font-weight:700;margin-top:6px}
 table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.28)}
 th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:middle}
 th{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--surface2)}
 tr:last-child td{border-bottom:none}.money{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:700}
 .pill{font-size:11px;font-family:var(--mono);padding:2px 8px;border-radius:999px;border:1px solid var(--border)}
 .pill.ok{color:var(--ok);border-color:rgba(52,211,153,.4)}.pill.off{color:var(--muted)}
 .act{color:var(--muted);cursor:pointer;background:none;border:none;padding:4px 8px;font-size:13px}.act:hover{color:var(--accent)}
 .msg{padding:10px 14px;border-radius:9px;margin-bottom:14px;font-size:14px;display:none}
 .msg.err{background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.35);color:var(--bad);display:block}
 .msg.ok{background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.35);color:var(--ok);display:block}
 .center{text-align:center;color:var(--muted);padding:50px}
 .ov{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;z-index:50}
 .ov.open{display:flex}.modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;width:100%;max-width:560px;padding:22px}
 .modal h2{margin:0 0 16px;font-size:18px}.fld{margin-bottom:13px}
 .fld label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.04em}
 .fld input,.fld select{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:calc(var(--radius) - 5px);color:var(--ink);padding:11px 13px;font-size:14px;transition:border-color .15s}
 .fld input:focus,.fld select:focus{outline:none;border-color:var(--accent)}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .modal .foot{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
 @media(max-width:720px){.side{width:60px}.side .it,.side .sec,.side .logo,.side .back{font-size:0;padding-left:0;text-align:center}}
</style></head><body>
<nav class="side"><div class="logo">COREXIA</div><div class="sec">Comercial</div>
__COMNAV__
<a class="back" href="/">&larr; Painel de vigilancia</a></nav>
<div class="main"><div class="top"><h1>__TITULO__</h1><div class="grow"></div><div style="display:flex;align-items:center;gap:12px"><span id="who" style="color:var(--muted);font-size:13px"></span><div id="avatar" style="width:34px;height:34px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ea580c);color:#1a1205;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;flex:none;box-shadow:0 2px 8px rgba(249,115,22,.3)">A</div></div></div>
<div class="content"><div id="authwarn" class="center" style="display:none">Faca login no painel primeiro. <a href="/" style="color:var(--accent)">Entrar</a>.</div>
<div id="app" style="display:none">__BODY__</div></div></div>
<script>
 var TOKEN=localStorage.getItem('corexia_token'); function $(id){return document.getElementById(id);}
 async function api(m,p,b){var r=await fetch(p,{method:m,headers:{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
   if(!r.ok){var e;try{e=(await r.json()).error}catch(x){} throw new Error(e||('HTTP '+r.status));} return r.status===204?null:r.json();}
 function brl(v){v=parseFloat(v||0);return 'R$ '+v.toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});}
 function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
 function msg(t,ok){var m=$('msg');if(!m)return;m.textContent=t;m.className='msg '+(ok?'ok':'err');if(ok)setTimeout(function(){m.className='msg'},3000);}
 (async function(){ if(!TOKEN){$('authwarn').style.display='block';return;}
   var role='';
   try{var p=await api('GET','/api/comercial/ping'); role=p.user.role; $('who').textContent=p.user.email+' - '+role; try{var _av=$('avatar'); if(_av)_av.textContent=((p.user.email||'A').charAt(0)||'A').toUpperCase();}catch(e){}}catch(e){$('authwarn').style.display='block';return;}
   document.querySelectorAll('[data-roles]').forEach(function(el){ if(((el.getAttribute('data-roles')||'').split(',').indexOf(role))<0) el.style.display='none'; });
   $('app').style.display='block'; if(window.PAGE_INIT) window.PAGE_INIT(); })();
</script></body></html>"""


def _shell(active, titulo, body):
    com_nav = "\n".join(
        '<a class="%s" data-roles="%s" href="/comercial/%s">%s</a>' % (("it active" if s == active else "it"), ",".join(r), s, l)
        for s, l, _t, r in COMERCIAL_ITENS)
    html = (_SHELL_TPL.replace("__TITULO__", titulo)
            .replace("__COMNAV__", com_nav)
            .replace("__BODY__", body))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ---------- paginas ----------
@router.get("/comercial")
def com_home(req: Request):
    cards = "".join(
        '<a class="kpi" data-roles="%s" style="text-decoration:none;color:inherit" href="/comercial/%s">'
        '<div class="k">%s</div><div class="v" style="font-size:15px;color:var(--accent)">abrir &rarr;</div></a>'
        % (",".join(r), s, l) for s, l, _t, r in COMERCIAL_ITENS)
    body = '<p style="color:var(--muted)">Console comercial - propostas, planos e cobranca.</p><div class="cards">%s</div>' % cards
    return _shell("", "Comercial", body)


@router.get("/comercial/planos")
def com_planos(req: Request):
    return _shell("planos", "Gerenciar Planos", _PLANOS_BODY)


@router.get("/comercial/propostas")
def com_propostas(req: Request):
    return _shell("propostas", "Propostas Comerciais", _PROPOSTAS_BODY)


@router.get("/comercial/faturas")
def com_faturas(req: Request):
    return _shell("faturas", "Faturas", _FATURAS_BODY)


@router.get("/comercial/vendedores")
def com_vendedores(req: Request):
    return _shell("vendedores", "Vendedores", _VENDEDORES_BODY)


@router.get("/comercial/comissionamento")
def com_comissionamento(req: Request):
    return _shell("comissionamento", "Comissionamento", _COMISSIONAMENTO_BODY)


@router.get("/comercial/contas-pagar")
def com_contas(req: Request):
    return _shell("contas-pagar", "Contas a Pagar", _CONTAS_BODY)


@router.get("/comercial/contratos")
def com_contratos(req: Request):
    return _shell("contratos", "Contratos", _CONTRATOS_BODY)


@router.get("/comercial/clientes")
def com_clientes(req: Request):
    return _shell("clientes", "Provedor/Revenda", _CLIENTES_BODY)



_REACT_SIDEBAR_CSS = '.rside{width:266px;padding:0;overflow-y:auto}.rlogo{display:flex;flex-direction:column;align-items:center;gap:7px;padding:20px 20px 16px;border-bottom:1px solid var(--border)}.rlogo img{width:40px;height:40px;object-fit:contain}.rlogo b{font-weight:800;font-size:17px;letter-spacing:3px;color:var(--ink)}.rsec{color:var(--muted);font-size:10.5px;font-family:var(--mono);letter-spacing:.13em;text-transform:uppercase;padding:16px 20px 6px}.rit{display:flex;align-items:center;gap:11px;color:var(--muted);text-decoration:none;padding:9px 20px;font-size:13.5px;border-left:3px solid transparent;transition:background .14s,color .14s,border-color .14s}.rit svg{width:18px;height:18px;flex:none}.rit:hover{background:var(--surface2);color:var(--ink)}.rit.active{background:linear-gradient(90deg,rgba(249,115,22,.9),rgba(249,115,22,.55));border-left-color:#fff;color:#1a1205;font-weight:700;border-radius:0 10px 10px 0;margin-right:10px}.ruser{margin-top:auto;display:flex;align-items:center;gap:10px;padding:13px 14px;border-top:1px solid var(--border)}.ravatar{width:38px;height:38px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ea580c);color:#1a1205;display:flex;align-items:center;justify-content:center;font-weight:800;flex:none;box-shadow:0 2px 8px rgba(249,115,22,.3)}.ruinfo{display:flex;flex-direction:column;min-width:0;flex:1;line-height:1.35}.runame{display:flex;align-items:center;gap:6px}.runame b{font-size:13px;color:var(--ink)}.rbadge{font-size:9px;font-weight:700;letter-spacing:.05em;color:var(--accent);border:1px solid rgba(249,115,22,.5);border-radius:5px;padding:1px 5px}.ruinfo span{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rlogout{color:var(--muted);text-decoration:none;font-size:17px;padding:5px 7px;border-radius:8px}.rlogout:hover{color:var(--bad);background:var(--surface2)}.rtop{display:flex;align-items:center;gap:10px}.ricon{width:38px;height:38px;border-radius:50%;background:var(--surface2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;color:var(--muted);cursor:pointer}.ricon svg{width:18px;height:18px}.rdot{position:absolute;top:9px;right:10px;width:7px;height:7px;border-radius:50%;background:var(--accent)}.top{background:var(--bg) !important}'
_REACT_NAV = '<nav class="side rside"><div class="rlogo"><img src="/brand/logo-icon.png" alt=""><b>COREXIA</b></div><div class="rsec">Monitoramento</div><a class="rit" href="/"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg><span>Dashboard Geral</span></a><a class="rit" href="/mapa"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg><span>Corexia Map</span></a><a class="rit" href="/propostas"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/></svg><span>Propostas</span></a><a class="rit" href="/planos"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg><span>Planos</span></a><a class="rit" href="/contratos"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/></svg><span>Contratos</span></a><a class="rit active" href="/comercial/analiticos"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 15l3-4 3 2 4-6"/></svg><span>Controle de IA (Global)</span></a><a class="rit" href="/faturas"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1z"/><path d="M8 7h8M8 11h8M8 15h5"/></svg><span>Faturas</span></a><a class="rit" href="/contas-pagar"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg><span>Contas a Pagar</span></a><a class="rit" href="/vendedores"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg><span>Vendedores</span></a><a class="rit" href="/comissionamento"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg><span>Comissionamento</span></a><a class="rit" href="/usuarios"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg><span>Gestão de Usuários</span></a><div class="rsec">Gestão Empresarial</div><a class="rit" href="/Provedores"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/></svg><span>Provedores</span></a><a class="rit" href="/portal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14L21 3"/></svg><span>Ver Portal do Cliente</span></a><div class="ruser"><div class="ravatar" id="avatar">A</div><div class="ruinfo"><div class="runame"><b id="who">Admin</b><span class="rbadge">COREXIA</span></div><span id="whomail">admin@corexia.com</span></div><a href="/" class="rlogout" title="Voltar ao painel">&#10162;</a></div></nav>'
_REACT_TOPRIGHT = '<div class="rtop"><div class="ricon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/></svg></div><div class="ricon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></div><div class="ricon" style="position:relative"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/></svg><span class="rdot"></span></div><div class="ravatar" style="width:34px;height:34px;font-size:14px">A</div></div>'
def _shell_react(titulo, body):
    html = _SHELL_TPL
    html = html.replace('<nav class="side"><div class="logo">COREXIA</div><div class="sec">Comercial</div>\n__COMNAV__\n<a class="back" href="/">&larr; Painel de vigilancia</a></nav>', _REACT_NAV)
    html = html.replace('<div style="display:flex;align-items:center;gap:12px"><span id="who" style="color:var(--muted);font-size:13px"></span><div id="avatar" style="width:34px;height:34px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ea580c);color:#1a1205;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;flex:none;box-shadow:0 2px 8px rgba(249,115,22,.3)">A</div></div>', _REACT_TOPRIGHT)
    html = html.replace('</head>', '<style>' + _REACT_SIDEBAR_CSS + '</style></head>')
    html = html.replace('__TITULO__', titulo).replace('__COMNAV__', '').replace('__BODY__', body)
    return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

@router.get("/comercial/analiticos")
def com_analiticos(req: Request):
    return _shell_react("Controle de IA (Global)", _ANALITICOS_BODY)


@router.get("/comercial/exclusoes")
def com_exclusoes(req: Request):
    return _shell("exclusoes", "Exclusoes de Camera", _EXCLUSOES_BODY)


@router.get("/comercial/demonstrador")
def com_demonstrador(req: Request):
    return _shell("demonstrador", "Usuario Demonstrador", _DEMO_BODY)


@router.get("/comercial/tester")
def com_tester(req: Request):
    return _shell("tester", "Provedor/Revenda Tester", _TESTER_BODY)


@router.get("/comercial/chamados")
def com_chamados(req: Request):
    return _shell("chamados", "Chamados dos Provedores", _CHAMADOS_ADMIN_BODY)


@router.get("/comercial/heatmap")
def com_heatmap(req: Request):
    return _shell("heatmap", "Mapa de Calor", _HEATMAP_BODY)


# ================= PORTAL DO PROVEDOR (server-rendered, escopado ao provedor logado) =================
_PROV_ITENS = [
    ("", "Dashboard", "Painel Operacional"),
    ("clientes", "Meus Clientes", "Meus Clientes"),
    ("propostas", "Propostas", "Propostas"),
    ("planos", "Planos", "Meus Planos"),
    ("faturas", "Cobranca", "Cobranca"),
    ("cameras", "Cameras e IA", "Cameras e Analiticos"),
    ("gravacoes", "Gravacoes", "Gravacoes em Nuvem"),
    ("alertas", "Alertas", "Alertas"),
    ("preferencias", "Preferencias", "Preferencias de Alerta"),
    ("plantao", "Plantao", "Plantao (WhatsApp)"),
    ("marca", "Minha Marca", "Minha Marca"),
    ("empresa", "Dados da Empresa", "Dados da Empresa"),
    ("vendedores", "Vendedores", "Vendedores Externos/Internos"),
    ("comissionamento", "Comissionamento", "Comissionamento"),
    ("ranking", "Ranking de Clientes", "Ranking de Clientes"),
    ("demonstrador", "Demonstrador", "Usuario Demonstrador"),
    ("gestao-usuarios", "Gestao de Usuarios", "Gestao de Usuarios"),
    ("chamados", "Chamados", "Suporte / Chamados"),
]


# injetor client-side: veste a casca do portal com a marca do provedor logado
_PROV_WL_JS = """<script>/* corexia-prov-wl */(function(){
 var t=localStorage.getItem('corexia_token'); if(!t)return;
 function h2(hx){hx=(hx||'').replace('#','');if(hx.length===3)hx=hx.split('').map(function(c){return c+c}).join('');return hx.length===6?[parseInt(hx.substr(0,2),16),parseInt(hx.substr(2,2),16),parseInt(hx.substr(4,2),16)]:null;}
 function rgba(hx,a){var c=h2(hx);return c?'rgba('+c[0]+','+c[1]+','+c[2]+','+a+')':'';}
 function dark(hx){var c=h2(hx);return c?((0.299*c[0]+0.587*c[1]+0.114*c[2])/255<0.6):false;}
 fetch('/api/comercial/branding/me',{headers:{'Authorization':'Bearer '+t}}).then(function(r){return r.json();}).then(function(b){
  if(!b)return; var cor=b.cor||'',menu=b.cor_menu||'',nome=b.nome_marca||'',logo=b.logo||''; var css='';
  if(h2(cor)){ css+=':root{--accent:'+cor+';--accent2:'+cor+';}'; css+='.side a.it.active{border-left-color:'+cor+';background:linear-gradient(90deg,'+rgba(cor,0.16)+',transparent);}'; css+='.btn-primary{color:'+(dark(cor)?'#fff':'#1a1205')+';}'; }
  if(h2(menu)){ css+='.side{background:'+menu+';}'; }
  if(css){ var s=document.getElementById('wl-prov'); if(!s){s=document.createElement('style');s.id='wl-prov';document.head.appendChild(s);} s.textContent=css; }
  var lg=document.querySelector('.side .logo'); if(lg){ if(logo){ lg.innerHTML='<img src="'+logo+'" alt="" style="max-width:100%;max-height:52px;display:block">'; } else if(nome){ lg.textContent=nome.toUpperCase(); } }
  if(nome){ document.title=document.title.replace('Corexia',nome); }
 }).catch(function(){});
})();</script>"""


def _prov_shell(active, titulo, body, req=None):
    perms = None
    if req is not None:
        try:
            _u = _current_user(req)
            if _u and _u.get("role") == "provedor" and _u.get("id"):
                _c = _db()
                _rr = _c.execute("SELECT menu_perms, equipe FROM users WHERE id=?", (_u.get("id"),)).fetchone()
                _c.close()
                if _rr and (_rr["equipe"] or 0) and _rr["menu_perms"]:
                    perms = set(json.loads(_rr["menu_perms"]))
        except Exception:
            perms = None
    itens = _PROV_ITENS
    if perms is not None:
        itens = [it for it in _PROV_ITENS if it[0] in perms]
        if active not in perms:
            body = ('<div class="center" style="padding:70px">'
                    '<div style="font-size:18px;color:var(--ink);margin-bottom:8px">Sem permissao de acesso</div>'
                    '<div style="color:var(--muted)">Voce nao tem acesso a este menu. Fale com o responsavel pela sua conta.</div></div>')
    nav = "\n".join('<a class="%s" href="/provedor%s">%s</a>'
                    % (("it active" if s == active else "it"), ("/" + s if s else ""), l)
                    for s, l, _t in itens)
    html = (_SHELL_TPL.replace("__TITULO__", titulo).replace("__COMNAV__", nav).replace("__BODY__", body)
            .replace('<div class="sec">Comercial</div>', '<div class="sec">Painel Operacional</div>')
            .replace("</body>", _PROV_WL_JS + _PANEL_SWITCHER_JS + "</body>"))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _prov_req(req):
    """(user, provedor_id) apenas se for um PROVEDOR logado (tem provedor_id)."""
    u = _current_user(req)
    if not u:
        return None, ""
    return u, (u.get("provedor_id") or "").strip()


@router.get("/provedor")
def prov_home(req: Request):
    return _prov_shell("", "Painel Operacional", _PROV_DASH_BODY, req)


@router.get("/provedor/clientes")
def prov_clientes_page(req: Request):
    return _prov_shell("clientes", "Meus Clientes", _PROV_CLIENTES_BODY, req)


@router.get("/provedor/planos")
def prov_planos_page(req: Request):
    return _prov_shell("planos", "Meus Planos", _PROV_PLANOS_BODY, req)


@router.get("/provedor/faturas")
def prov_faturas_page(req: Request):
    return _prov_shell("faturas", "Cobranca", _PROV_FATURAS_BODY, req)


@router.get("/provedor/marca")
def prov_marca_page(req: Request):
    return _prov_shell("marca", "Minha Marca", _PROV_MARCA_BODY, req)


@router.get("/provedor/cameras")
def prov_cameras_page(req: Request):
    return _prov_shell("cameras", "Cameras e Analiticos", _PROV_CAMERAS_BODY, req)


@router.get("/provedor/alertas")
def prov_alertas_page(req: Request):
    return _prov_shell("alertas", "Alertas", _PROV_ALERTAS_BODY, req)


@router.get("/provedor/preferencias")
def prov_preferencias_page(req: Request):
    return _prov_shell("preferencias", "Preferencias de Alerta", _PROV_PREF_BODY, req)


@router.get("/provedor/plantao")
def prov_plantao_page(req: Request):
    return _prov_shell("plantao", "Plantao (WhatsApp, req)", _PROV_PLANTAO_BODY)


def _monit_shell(body):
    nav = '<a class="it active" href="/monitoramento">Mural ao vivo</a>'
    html = (_SHELL_TPL.replace("__TITULO__", "Monitoramento").replace("__COMNAV__", nav).replace("__BODY__", body)
            .replace('<div class="sec">Comercial</div>', '<div class="sec">Monitoramento</div>')
            .replace("</body>", _PROV_WL_JS + _PANEL_SWITCHER_JS + "</body>"))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/monitoramento")
def monit_page(req: Request):
    return _monit_shell(_MONIT_BODY)


@router.get("/provedor/gravacoes")
def prov_gravacoes_page(req: Request):
    return _prov_shell("gravacoes", "Gravacoes em Nuvem", _PROV_GRAVACOES_BODY, req)


@router.get("/provedor/chamados")
def prov_chamados_page(req: Request):
    return _prov_shell("chamados", "Suporte / Chamados", _PROV_CHAMADOS_BODY, req)


_PROV_EMPRESA_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Estes dados aparecem como <b>CONTRATADA</b> nos contratos que voce gera para os seus clientes finais.</p>
<div class="fld"><label>Razao social *</label><input id="e_rs" placeholder="Ex.: SERV PROTECT LTDA"></div>
<div class="grid2">
 <div class="fld"><label>CNPJ *</label><input id="e_cnpj" placeholder="00.000.000/0000-00"></div>
 <div class="fld"><label>Nome fantasia (rodape do contrato)</label><input id="e_nf" placeholder="Ex.: VIGGIA SISTEMAS INTELIGENTE"></div>
</div>
<div class="grid3">
 <div class="fld"><label>CEP</label><input id="e_cep"></div>
 <div class="fld"><label>Cidade</label><input id="e_cid"></div>
 <div class="fld"><label>UF</label><input id="e_uf" maxlength="2"></div>
</div>
<div class="grid3">
 <div class="fld" style="flex:2"><label>Logradouro</label><input id="e_log"></div>
 <div class="fld"><label>Numero</label><input id="e_num"></div>
 <div class="fld"><label>Bairro</label><input id="e_bai"></div>
</div>
<div style="text-align:right;margin-top:8px"><button class="btn-primary" onclick="salvar()">Salvar</button></div>
<style>.fld{margin-bottom:10px}.fld label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.fld input{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 11px;font-size:14px}
.grid2{display:flex;gap:12px}.grid2>.fld{flex:1}.grid3{display:flex;gap:12px}.grid3>.fld{flex:1}
@media(max-width:560px){.grid2,.grid3{flex-direction:column;gap:0}}</style>
<script>
window.PAGE_INIT=load;
async function load(){ try{ var d=await api('GET','/api/comercial/prov/empresa'); $('e_rs').value=d.razao_social||''; $('e_cnpj').value=d.document_number||''; $('e_nf').value=d.nome_fantasia||''; $('e_cep').value=d.cep||''; $('e_cid').value=d.cidade||''; $('e_uf').value=d.uf||''; $('e_log').value=d.logradouro||''; $('e_num').value=d.numero||''; $('e_bai').value=d.bairro||''; }catch(e){ msg('Erro: '+e.message); } }
async function salvar(){ var b={razao_social:$('e_rs').value.trim(),document_number:$('e_cnpj').value.trim(),nome_fantasia:$('e_nf').value.trim(),cep:$('e_cep').value.trim(),cidade:$('e_cid').value.trim(),uf:$('e_uf').value.trim(),logradouro:$('e_log').value.trim(),numero:$('e_num').value.trim(),bairro:$('e_bai').value.trim()};
 if(!b.razao_social||!b.document_number){ msg('Informe razao social e CNPJ.'); return; }
 try{ await api('POST','/api/comercial/prov/empresa/salvar',b); msg('Dados salvos.',true); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


@router.get("/provedor/empresa")
def prov_empresa_page(req: Request):
    return _prov_shell("empresa", "Dados da Empresa", _PROV_EMPRESA_BODY, req)


@router.get("/api/comercial/prov/empresa")
def prov_empresa_get(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    pr = _get_ent("Provedor", pid) or {}
    emp = pr.get("empresa") or {}
    return {"nome": pr.get("nome", ""), "razao_social": emp.get("razao_social", ""),
            "document_number": emp.get("cnpj", "") or pr.get("document_number", ""),
            "nome_fantasia": emp.get("nome_fantasia", "") or pr.get("nome", ""),
            "cep": emp.get("cep", ""), "logradouro": emp.get("logradouro", ""),
            "numero": emp.get("numero", ""), "bairro": emp.get("bairro", ""),
            "cidade": emp.get("cidade", ""), "uf": emp.get("uf", "")}


@router.post("/api/comercial/prov/empresa/salvar")
async def prov_empresa_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    emp = {"razao_social": (b.get("razao_social") or "").strip(),
           "cnpj": (b.get("document_number") or "").strip(),
           "nome_fantasia": (b.get("nome_fantasia") or "").strip(),
           "cep": (b.get("cep") or "").strip(), "logradouro": (b.get("logradouro") or "").strip(),
           "numero": (b.get("numero") or "").strip(), "bairro": (b.get("bairro") or "").strip(),
           "cidade": (b.get("cidade") or "").strip(), "uf": (b.get("uf") or "").strip()}
    patch = {"empresa": emp}
    if emp["cnpj"]:
        patch["document_number"] = emp["cnpj"]
    _update_ent("Provedor", pid, patch)
    return {"success": True}


_PROV_PROPOSTAS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><div style="flex:1"></div><button class="btn-primary" onclick="novo()">+ Nova proposta</button></div>
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Crie a proposta do cliente, envie o codigo por WhatsApp para ele assinar, e gere o contrato (CPF ou CNPJ conforme o documento).</p>
<table><thead><tr><th>Cliente</th><th>Documento</th><th>Plano</th><th>Valor/mes</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:640px"><h2 id="mt">Nova proposta</h2><input type="hidden" id="p_id"><input type="hidden" id="p_cams">
 <div class="grid2">
  <div class="fld"><label>Nome do cliente *</label><input id="p_nome"></div>
  <div class="fld"><label>Plano *</label><select id="p_plano" onchange="onPlano()"></select></div>
 </div>
 <div class="grid3">
  <div class="fld"><label>Tipo Documento</label><select id="p_dt"><option value="cnpj">CNPJ</option><option value="cpf">CPF</option></select></div>
  <div class="fld" style="flex:2"><label>CPF / CNPJ *</label><input id="p_doc"></div>
 </div>
 <div class="grid3">
  <div class="fld"><label>WhatsApp * (com DDD)</label><input id="p_wa" placeholder="8199...."></div>
  <div class="fld"><label>E-mail</label><input id="p_email"></div>
  <div class="fld"><label>Telefone</label><input id="p_tel"></div>
 </div>
 <div class="grid3">
  <div class="fld"><label>CEP</label><input id="p_cep"></div>
  <div class="fld" style="flex:2"><label>Logradouro</label><input id="p_log"></div>
  <div class="fld"><label>Numero</label><input id="p_num"></div>
 </div>
 <div class="grid3">
  <div class="fld"><label>Bairro</label><input id="p_bai"></div>
  <div class="fld"><label>Cidade</label><input id="p_cid"></div>
  <div class="fld"><label>UF</label><input id="p_uf" maxlength="2"></div>
 </div>
 <div class="grid2">
  <div class="fld"><label>Valor mensal (R$)</label><input id="p_valor" type="number" step="0.01"></div>
  <div class="fld"><label>Contrato (meses)</label><input id="p_meses" type="number"></div>
 </div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar proposta</button></div></div></div>
<style>.fld{margin-bottom:10px}.fld label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.fld input,.fld select{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 11px;font-size:14px}
.grid2{display:flex;gap:12px}.grid2>.fld{flex:1}.grid3{display:flex;gap:12px}.grid3>.fld{flex:1}
@media(max-width:560px){.grid2,.grid3{flex-direction:column;gap:0}}</style>
<script>
var PROPS=[], PLANOS=[], EMP={}; window.PAGE_INIT=init;
function digs(s){ return (''+(s||'')).replace(/\D/g,''); }
function fmtDoc(raw){ var d=digs(raw); if(d.length===14) return d.slice(0,2)+'.'+d.slice(2,5)+'.'+d.slice(5,8)+'/'+d.slice(8,12)+'-'+d.slice(12,14); if(d.length===11) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9,11); return raw||''; }
var ST={aberta:['Aberta','off'],enviada:['Codigo enviado','off'],fechada:['Assinada','ok'],rascunho:['Rascunho','off']};
async function init(){ try{ EMP=await api('GET','/api/comercial/prov/empresa'); }catch(e){ EMP={}; }
 try{ PLANOS=(await api('GET','/api/comercial/prov/planos'))||[]; }catch(e){ PLANOS=[]; } load(); }
function fillPlanos(){ var s=$('p_plano'); s.innerHTML='<option value="">- selecione -</option>'+PLANOS.filter(function(p){return p.ativo!==false}).map(function(p){ return '<option value="'+p.id+'">'+esc(p.nome)+' - '+brl(p.valor)+'</option>'; }).join(''); }
function onPlano(){ var p=PLANOS.filter(function(x){return x.id===$('p_plano').value})[0]; if(!p)return;
 $('p_valor').value=(p.valor!=null?p.valor:''); $('p_meses').value=(p.contrato_meses||36); $('p_cams').value=(p.cameras||'');
 if(p.tipo_documento==='cnpj'||p.tipo_documento==='cpf') $('p_dt').value=p.tipo_documento; }
async function load(){ try{ PROPS=(await api('GET','/api/comercial/prov/propostas'))||[]; render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function render(){ $('rows').innerHTML=PROPS.map(function(p,i){ var st=ST[p.status||'aberta']||['?','off']; var dl=(p.document_type==='cpf'?'CPF':'CNPJ');
 var acts='<button class="act" style="color:var(--accent)" onclick="verC('+i+')">ver contrato</button>';
 if((p.status||'aberta')!=='fechada'){ acts='<button class="act" onclick="editar('+i+')">editar</button><button class="act" onclick="enviar('+i+')">enviar codigo</button><button class="act" style="color:var(--ok)" onclick="assinar('+i+')">assinar</button>'+acts; }
 return '<tr><td><b>'+esc(p.cliente_nome||'-')+'</b></td><td>'+dl+': '+esc(fmtDoc(p.document_number)||'-')+'</td><td>'+esc(p.plano_nome||'-')+'</td><td class="money">'+brl(p.valor_mensal)+'</td><td><span class="pill '+st[1]+'">'+st[0]+'</span></td><td style="text-align:right;white-space:nowrap">'+acts+'</td></tr>';
 }).join('')||'<tr><td colspan="6" class="center">Nenhuma proposta ainda. Clique em "Nova proposta".</td></tr>'; }
function novo(){ fillPlanos(); $('mt').textContent='Nova proposta'; ['p_id','p_nome','p_doc','p_wa','p_email','p_tel','p_cep','p_log','p_num','p_bai','p_cid','p_uf','p_valor','p_meses','p_cams'].forEach(function(x){$(x).value='';}); $('p_plano').value=''; $('p_dt').value='cnpj'; $('ov').classList.add('open'); }
function editar(i){ fillPlanos(); var p=PROPS[i]; if(!p)return; $('mt').textContent='Editar proposta'; $('p_id').value=p.id; $('p_nome').value=p.cliente_nome||''; $('p_plano').value=p.plano_id||''; $('p_dt').value=p.document_type||'cnpj'; $('p_doc').value=p.document_number||''; $('p_wa').value=p.whatsapp||''; $('p_email').value=p.email||''; $('p_tel').value=p.telefone||''; $('p_cep').value=p.cep||''; $('p_log').value=p.logradouro||''; $('p_num').value=p.numero||''; $('p_bai').value=p.bairro||''; $('p_cid').value=p.cidade||''; $('p_uf').value=p.uf||''; $('p_valor').value=(p.valor_mensal!=null?p.valor_mensal:''); $('p_meses').value=(p.contrato_meses||''); $('p_cams').value=(p.cameras||''); $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var pl=PLANOS.filter(function(x){return x.id===$('p_plano').value})[0];
 var b={id:$('p_id').value,cliente_nome:$('p_nome').value.trim(),plano_id:$('p_plano').value,plano_nome:(pl?pl.nome:''),cameras:parseInt($('p_cams').value||0)||0,document_type:$('p_dt').value,document_number:$('p_doc').value.trim(),whatsapp:$('p_wa').value.trim(),email:$('p_email').value.trim(),telefone:$('p_tel').value.trim(),cep:$('p_cep').value.trim(),logradouro:$('p_log').value.trim(),numero:$('p_num').value.trim(),bairro:$('p_bai').value.trim(),cidade:$('p_cid').value.trim(),uf:$('p_uf').value.trim(),valor_mensal:parseFloat($('p_valor').value||0)||0,contrato_meses:parseInt($('p_meses').value||0)||0};
 if(!b.cliente_nome){ msg('Informe o nome do cliente.'); return; }
 if(!b.document_number){ msg('Informe o CPF/CNPJ.'); return; }
 try{ await api('POST','/api/comercial/prov/propostas/salvar',b); fecha(); msg('Proposta salva.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function enviar(i){ var p=PROPS[i]; if(!p)return; if(!p.whatsapp){ msg('Proposta sem WhatsApp do cliente.'); return; }
 try{ var r=await api('POST','/api/comercial/propostas/'+p.id+'/enviar-codigo',{}); msg(r.enviado?'Codigo enviado no WhatsApp do cliente.':'Falha ao enviar (WhatsApp/Z-API): '+(r.info||''),!!r.enviado); load(); }catch(e){ msg('Erro: '+e.message); } }
async function assinar(i){ var p=PROPS[i]; if(!p)return; var code=prompt('Digite o codigo de 6 digitos que o cliente recebeu no WhatsApp:'); if(!code)return;
 try{ await api('POST','/api/comercial/propostas/'+p.id+'/assinar',{codigo:code.trim()}); msg('Assinado! Contrato pronto.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
function verC(i){ var p=PROPS[i]; if(!p)return; var w=window.open('','_blank'); if(!w){ msg('Permita pop-ups para abrir o contrato.'); return; } w.document.open(); w.document.write(contratoClienteHTML(p)); w.document.close(); }
function contratoClienteHTML(p){
 var pf=(p.document_type==='cpf'); var docLabel=pf?'CPF':'CNPJ'; var titulo=pf?'PESSOA FISICA':'PESSOA JURIDICA';
 var meses=parseInt(p.contrato_meses||0)||(pf?12:36);
 var nomeC=esc(p.cliente_nome||'________________'); var docC=esc(fmtDoc(p.document_number)||'________________');
 var epC=[]; if(p.logradouro)epC.push(p.logradouro+(p.numero?(', '+p.numero):'')); if(p.bairro)epC.push(p.bairro); if(p.cidade)epC.push(p.cidade+(p.uf?('/'+p.uf):'')); if(p.cep)epC.push('CEP '+p.cep);
 var endC=esc(epC.join(' - ')||'________________');
 var rs=esc(EMP.razao_social||'________________'); var cnpj=esc(fmtDoc(EMP.document_number)||'________________');
 var nf=esc(EMP.nome_fantasia||EMP.nome||'');
 var epE=[]; if(EMP.logradouro)epE.push(EMP.logradouro+(EMP.numero?(', '+EMP.numero):'')); if(EMP.bairro)epE.push(EMP.bairro); if(EMP.cidade)epE.push(EMP.cidade+(EMP.uf?('/'+EMP.uf):'')); if(EMP.cep)epE.push('CEP '+EMP.cep);
 var endE=esc(epE.join(' - '));
 var plano=esc(p.plano_nome||'-'); var valor=brl(p.valor_mensal); var ncam=(p.cameras||0);
 var assinada=(p.status==='fechada'||!!p.assinatura_id);
 var css='body{margin:0;background:#525659;font-family:Georgia,serif;color:#1a1a1a}'+
 '.bar{position:sticky;top:0;background:#171a21;padding:10px 16px;text-align:right;z-index:9}'+
 '.bar button{background:#f97316;border:none;color:#1a1205;font-weight:700;font-family:sans-serif;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:14px}'+
 '.doc{max-width:820px;margin:22px auto;background:#fff;padding:52px 60px;box-shadow:0 2px 20px rgba(0,0,0,.4);line-height:1.55;font-size:15px}'+
 '.logo{font-family:sans-serif;font-weight:800;letter-spacing:2px;color:#f97316;font-size:22px;text-align:center;margin-bottom:6px}'+
 'h1{font-size:20px;text-align:center;margin:6px 0 2px}.sub{text-align:center;font-size:12.5px;color:#555;margin:0 0 20px}'+
 'h2{font-size:14.5px;margin:18px 0 5px}p{margin:7px 0;text-align:justify}'+
 '.parties{background:#faf7f2;border-left:3px solid #f97316;padding:12px 15px;font-size:14px}'+
 '.sign{margin-top:36px}.sign .row{margin-top:30px;border-top:1px solid #333;padding-top:6px;font-size:14px}'+
 '.esign{margin-top:8px;padding:9px 11px;border:1px solid #16a34a;background:#f0fdf4;border-radius:6px;font-size:11px;font-family:sans-serif;color:#14532d;line-height:1.5}'+
 '.esign b{color:#15803d}.pend{margin-top:6px;font-size:11px;color:#9a6a00;font-family:sans-serif}'+
 '@media print{body{background:#fff}.bar{display:none}.doc{box-shadow:none;margin:0;max-width:none;padding:0}@page{margin:2cm}}';
 var b=[];
 b.push('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Contrato - '+nomeC+'</title><style>'+css+'</style></head><body>');
 b.push('<div class="bar"><button onclick="window.print()">Imprimir / Salvar PDF</button></div><div class="doc">');
 if(nf) b.push('<div class="logo">'+nf+'</div>');
 b.push('<h1>CONTRATO &ndash; '+titulo+'</h1>');
 b.push('<p class="sub">CONTRATO DE PRESTA&Ccedil;&Atilde;O DE SERVI&Ccedil;OS DE MONITORAMENTO COM COMODATO DE EQUIPAMENTOS</p>');
 b.push('<p class="parties"><b>CONTRATADA:</b> '+rs+' &mdash; CNPJ '+cnpj+(endE?(' &mdash; '+endE):'')+'.<br><b>CONTRATANTE:</b> '+nomeC+' &mdash; '+docLabel+' '+docC+(endC?(' &mdash; '+endC):'')+(p.whatsapp?(' &mdash; Tel.: '+esc(p.whatsapp)):'')+(p.email?(' &mdash; '+esc(p.email)):'')+'.</p>');
 b.push('<h2>CL&Aacute;USULA 1&ordf; &ndash; DO OBJETO</h2><p>O presente contrato tem como objeto a presta&ccedil;&atilde;o de servi&ccedil;os de monitoramento eletr&ocirc;nico por meio de TOTEM DE SEGURAN&Ccedil;A equipado com c&acirc;meras e tecnologia de Intelig&ecirc;ncia Artificial (IA), fornecidos em regime de comodato.</p>');
 b.push('<h2>CL&Aacute;USULA 2&ordf; &ndash; DO COMODATO</h2><p>Todos os equipamentos permanecem de propriedade exclusiva da CONTRATADA, n&atilde;o caracterizando venda, loca&ccedil;&atilde;o financeira ou cess&atilde;o definitiva.</p>');
 b.push('<h2>CL&Aacute;USULA 3&ordf; &ndash; DO PRAZO</h2><p>O presente contrato ter&aacute; vig&ecirc;ncia de '+meses+' ('+meses+') meses, contados a partir da data de ativa&ccedil;&atilde;o do sistema.</p>');
 b.push('<h2>CL&Aacute;USULA 4&ordf; &ndash; DO PLANO CONTRATADO</h2><p>Plano <b>'+plano+'</b>'+(ncam?(' ('+ncam+' c&acirc;meras)'):'')+', no valor mensal de <b>'+valor+'</b>, pelo prazo previsto na Cl&aacute;usula 3&ordf;.</p>');
 b.push('<h2>CL&Aacute;USULA 5&ordf; &ndash; DA INSTALA&Ccedil;&Atilde;O</h2><p>A instala&ccedil;&atilde;o ser&aacute; realizada em local indicado pelo CONTRATANTE, mediante condi&ccedil;&otilde;es t&eacute;cnicas adequadas.</p>');
 b.push('<h2>CL&Aacute;USULA 6&ordf; &ndash; DA MANUTEN&Ccedil;&Atilde;O</h2><p>A CONTRATADA ser&aacute; respons&aacute;vel pela manuten&ccedil;&atilde;o preventiva e corretiva durante a vig&ecirc;ncia contratual.</p>');
 b.push('<h2>CL&Aacute;USULA 7&ordf; &ndash; DAS RESPONSABILIDADES DO CONTRATANTE</h2><p>Zelar pelos equipamentos, n&atilde;o permitir interven&ccedil;&otilde;es de terceiros e comunicar imediatamente qualquer ocorr&ecirc;ncia.</p>');
 b.push('<h2>CL&Aacute;USULA 8&ordf; &ndash; DOS DANOS, FURTO OU VANDALISMO</h2><p>Em caso de dano, furto ou vandalismo, dever&aacute; ser apresentado boletim de ocorr&ecirc;ncia.</p>');
 b.push('<h2>CL&Aacute;USULA 9&ordf; &ndash; DAS IMAGENS E LGPD</h2><p>As imagens s&atilde;o sigilosas e somente ser&atilde;o disponibilizadas mediante ordem judicial ou solicita&ccedil;&atilde;o formal de &oacute;rg&atilde;os p&uacute;blicos, em conformidade com a Lei n&ordm; 13.709/2018 (LGPD).</p>');
 b.push('<h2>CL&Aacute;USULA 10&ordf; &ndash; DA INTERNET E CONECTIVIDADE</h2>'+
  '<p><b>10.1.</b> A Internet utilizada para o funcionamento do sistema &eacute; de responsabilidade exclusiva e obrigat&oacute;ria do CONTRATANTE, que se compromete a providenciar conex&atilde;o pr&oacute;pria e mant&ecirc;-la ativa durante toda a vig&ecirc;ncia.</p>'+
  '<p><b>10.2.</b> O CONTRATANTE reconhece que a velocidade e a qualidade da conex&atilde;o afetam diretamente o funcionamento das c&acirc;meras e do monitoramento, devendo manter velocidade adequada para transmiss&atilde;o cont&iacute;nua de v&iacute;deo.</p>'+
  '<p><b>10.3.</b> A CONTRATADA n&atilde;o se responsabiliza por instabilidades, queda de conex&atilde;o, baixa resolu&ccedil;&atilde;o, redu&ccedil;&atilde;o de frames ou travamentos oriundos da qualidade, velocidade insuficiente ou indisponibilidade da Internet do CONTRATANTE, atuando com o m&aacute;ximo esfor&ccedil;o e t&eacute;cnicas de otimiza&ccedil;&atilde;o para minimizar impactos.</p>');
 b.push('<h2>CL&Aacute;USULA 11&ordf; &ndash; DA INADIMPL&Ecirc;NCIA</h2><p>Atraso superior a 30 (trinta) dias autoriza a suspens&atilde;o do servi&ccedil;o e a retirada dos equipamentos.</p>');
 b.push('<h2>CL&Aacute;USULA 12&ordf; &ndash; DA RESCIS&Atilde;O</h2><p>A rescis&atilde;o antecipada implicar&aacute; multa equivalente a 40% (quarenta por cento) das mensalidades restantes.</p>');
 b.push('<h2>CL&Aacute;USULA 13&ordf; &ndash; DA DEVOLU&Ccedil;&Atilde;O</h2><p>Os equipamentos dever&atilde;o ser devolvidos em perfeito estado ao final do contrato.</p>');
 b.push('<h2>CL&Aacute;USULA 14&ordf; &ndash; DA ASSINATURA ELETR&Ocirc;NICA</h2><p>As partes reconhecem e aceitam a assinatura deste contrato por meio eletr&ocirc;nico, mediante c&oacute;digo de verifica&ccedil;&atilde;o de uso &uacute;nico enviado ao WhatsApp do CONTRATANTE, atribuindo-lhe plena validade jur&iacute;dica (art. 107 do C&oacute;digo Civil; MP 2.200-2/2001; Lei 14.063/2020), reconhecendo a trilha de auditoria (identificador, data/hora, WhatsApp, IP e resumo SHA-256) como prova da autoria e integridade.</p>');
 b.push('<h2>CL&Aacute;USULA 15&ordf; &ndash; DO FORO</h2><p>Fica eleito o foro da comarca da sede da CONTRATADA para dirimir quaisquer quest&otilde;es oriundas deste contrato.</p>');
 b.push('<div class="sign"><p>E, por estarem de acordo, assinam:</p>');
 b.push('<div class="row"><b>'+rs+' (CONTRATADA)</b><br>CNPJ '+cnpj+(nf?('<br>'+nf):'')+'</div>');
 if(assinada){ b.push('<div class="row"><b>CONTRATANTE: '+nomeC+'</b><br>'+docLabel+' '+docC+'<div class="esign"><b>&#10003; ASSINADO ELETRONICAMENTE</b><br>Assinado por '+esc(p.assinado_por_nome||p.cliente_nome||'')+' ('+docLabel+' '+docC+')<br>C&oacute;digo enviado ao WhatsApp '+esc(p.assinado_por||p.whatsapp||'-')+', validado em '+esc(p.assinado_em_local||p.assinada_em||'-')+'.<br>IP: '+esc(p.assinado_ip||'-')+' &middot; ID: '+esc(p.assinatura_id||'-')+'<br>SHA-256: '+esc((p.doc_hash||'').slice(0,48))+'&hellip;</div></div>'); }
 else { b.push('<div class="row"><b>CONTRATANTE: '+nomeC+'</b><br>'+docLabel+' '+docC+'<div class="pend">Aguardando assinatura eletr&ocirc;nica (c&oacute;digo por WhatsApp).</div></div>'); }
 b.push('</div></div></bo'+'dy></html>'); return b.join('');
}
</script>
"""


@router.get("/provedor/propostas")
def prov_propostas_page(req: Request):
    return _prov_shell("propostas", "Propostas", _PROV_PROPOSTAS_BODY, req)


@router.get("/api/comercial/prov/propostas")
def prov_propostas_list(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Proposta'").fetchall(); c.close()
    out = [dict(json.loads(r["data"]), id=r["id"]) for r in rows]
    out = [x for x in out if x.get("provedor_id") == pid]
    out.sort(key=lambda x: x.get("criado_em", ""), reverse=True)
    return out


@router.post("/api/comercial/prov/propostas/salvar")
async def prov_propostas_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    nome = (b.get("cliente_nome") or "").strip()
    if not nome:
        return JSONResponse({"error": "informe o nome do cliente"}, status_code=400)
    try:
        valor = round(float(b.get("valor_mensal") or 0), 2)
    except (TypeError, ValueError):
        valor = 0.0
    try:
        meses = int(b.get("contrato_meses") or 0)
    except (TypeError, ValueError):
        meses = 0
    try:
        cams = int(b.get("cameras") or 0)
    except (TypeError, ValueError):
        cams = 0
    data = {"provedor_id": pid, "cliente_nome": nome,
            "document_type": (b.get("document_type") or "cnpj"), "document_number": (b.get("document_number") or "").strip(),
            "whatsapp": (b.get("whatsapp") or b.get("telefone") or "").strip(), "telefone": (b.get("telefone") or "").strip(),
            "email": (b.get("email") or "").strip(),
            "cep": (b.get("cep") or "").strip(), "logradouro": (b.get("logradouro") or "").strip(),
            "numero": (b.get("numero") or "").strip(), "bairro": (b.get("bairro") or "").strip(),
            "cidade": (b.get("cidade") or "").strip(), "uf": (b.get("uf") or "").strip(),
            "plano_id": (b.get("plano_id") or ""), "plano_nome": (b.get("plano_nome") or ""),
            "cameras": cams, "valor_mensal": valor, "contrato_meses": meses}
    eid = (b.get("id") or "").strip()
    if eid:
        ex = _get_ent("Proposta", eid)
        if not ex or ex.get("provedor_id") != pid:
            return JSONResponse({"error": "proposta nao encontrada"}, status_code=403)
        data["status"] = ex.get("status", "aberta"); data["criado_em"] = ex.get("criado_em")
        for _k in ("assinatura_id", "assinado_em", "assinado_em_local", "assinado_ip", "doc_hash",
                   "assinado_por", "assinado_por_nome", "assinado_por_doc", "assinada_em"):
            if ex.get(_k):
                data[_k] = ex[_k]
        _update_ent("Proposta", eid, data)
    else:
        data["status"] = "aberta"; data["criado_em"] = _now_iso()
        eid = _create_ent("Proposta", data)
    return {"success": True, "id": eid}


@router.get("/provedor/vendedores")
def prov_vendedores_page(req: Request):
    return _prov_shell("vendedores", "Vendedores Externos/Internos", _VENDEDORES_BODY, req)


@router.get("/provedor/comissionamento")
def prov_comissionamento_page(req: Request):
    return _prov_shell("comissionamento", "Comissionamento", _COMISSIONAMENTO_BODY, req)


_PROV_RANKING_BODY = """
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Total de Clientes</div><div class="v" id="k_tot">-</div></div>
 <div class="kpi"><div class="k">Pontualidade Media</div><div class="v" id="k_pont" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Total Recebido</div><div class="v" id="k_rec" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Com Faturas Vencidas</div><div class="v" id="k_ven" style="color:var(--bad)">-</div></div></div>
<input id="q" placeholder="Buscar por nome ou email..." oninput="render()" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px;margin:6px 0 14px">
<div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Cliente</th><th>Score</th><th>Status</th><th>Pontualidade</th><th>Atraso Medio</th><th>Faturas Pagas</th><th>Vencidas</th><th>Total Pago</th><th>Valor Mensal</th></tr></thead>
<tbody id="rows"><tr><td colspan="10" class="center">carregando...</td></tr></tbody></table></div>
<script>
var RANK=[]; window.PAGE_INIT=loadR;
function band(s){ if(s>=80)return['Excelente','#22c55e']; if(s>=60)return['Bom','#eab308']; if(s>=40)return['Regular','#f97316']; return['Ruim','#ef4444']; }
function medal(i){ return i===0?'\\uD83E\\uDD47':i===1?'\\uD83E\\uDD48':i===2?'\\uD83E\\uDD49':('#'+(i+1)); }
async function loadR(){ try{ var d=await api('GET','/api/comercial/prov/ranking'); RANK=d.clientes||[];
 var r=d.resumo||{}; $('k_tot').textContent=r.total_clientes; $('k_pont').textContent=(r.pontualidade_media||0)+'%'; $('k_rec').textContent=brl(r.total_recebido); $('k_ven').textContent=r.com_vencidas;
 render(); }catch(e){ msg('Erro: '+e.message); } }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=RANK.filter(function(c){ return !q || (c.nome||'').toLowerCase().indexOf(q)>=0 || (c.email||'').toLowerCase().indexOf(q)>=0; });
 $('rows').innerHTML=arr.map(function(c){ var pos=RANK.indexOf(c); var b=band(c.score);
  return '<tr><td style="font-size:16px;white-space:nowrap">'+medal(pos)+'</td>'+
   '<td><b>'+esc(c.nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(c.email||'')+'</div></td>'+
   '<td style="font-weight:700;color:'+b[1]+'">'+c.score+'</td>'+
   '<td><span class="pill" style="background:'+b[1]+'22;color:'+b[1]+'">'+b[0]+'</span></td>'+
   '<td>'+c.pontualidade+'%</td><td>'+c.atraso_medio+' d</td><td>'+c.pagas+'/'+c.total_faturas+'</td>'+
   '<td>'+(c.vencidas>0?'<span style="color:var(--bad)">'+c.vencidas+'</span>':'0')+'</td>'+
   '<td class="money">'+brl(c.total_pago)+'</td><td class="money">'+brl(c.valor_mensal)+'</td></tr>'; }).join('')
   ||'<tr><td colspan="10" class="center">Nenhum cliente.</td></tr>'; }
</script>
"""


@router.get("/api/comercial/prov/ranking")
def prov_ranking(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    import math
    c = _db()
    clientes = [dict(json.loads(r["data"]), id=r["id"]) for r in c.execute("SELECT id,data FROM entities WHERE entity='Cliente'").fetchall()]
    fats = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Fatura'").fetchall()]
    c.close()
    clientes = [x for x in clientes if x.get("provedor_id") == pid]
    porcli = {}
    for f in fats:
        if f.get("provedor_id") == pid and f.get("cliente_id"):
            porcli.setdefault(f["cliente_id"], []).append(f)

    def _d(s):
        try:
            return datetime.strptime((s or "")[:10], "%Y-%m-%d")
        except Exception:
            return None

    rows = []; soma_pont = 0.0; n_pont = 0; total_receb = 0.0; com_venc = 0
    for cli in clientes:
        ff = porcli.get(cli["id"], [])
        pagas = [f for f in ff if f.get("status") == "paga"]
        vencidas = [f for f in ff if f.get("status") == "vencida"]
        total_pago = sum(float(f.get("valor", 0) or 0) for f in pagas)
        valor_venc = sum(float(f.get("valor", 0) or 0) for f in vencidas)
        valor_mensal = float(cli.get("valor_mensal", 0) or 0)
        no_prazo = 0; atrasos = []
        for f in pagas:
            dv = _d(f.get("vencimento")); dp = _d(f.get("pago_em"))
            if dv and dp:
                if dp <= dv:
                    no_prazo += 1
                else:
                    atrasos.append((dp - dv).days)
            else:
                no_prazo += 1
        tp = len(pagas)
        taxa = (no_prazo / tp * 100) if tp else 0.0
        if len(ff) == 0:
            b1 = 35.0
        elif tp == 0:
            b1 = 10.0
        else:
            b1 = taxa / 100 * 70
            atraso_medio = (sum(atrasos) / tp) if tp else 0
            b1 -= min(15, atraso_medio * 0.5)
            if atrasos and max(atrasos) > 30:
                b1 -= 5
            b1 = max(0, b1)
        b2 = min(15, valor_venc / (valor_mensal * 3) * 15) if (valor_venc > 0 and valor_mensal > 0) else 0.0
        b3 = min(30, math.log10(total_pago + 1) / 5 * 30)
        score = max(0, min(100, b1 - b2 + b3))
        atraso_medio_all = round((sum(atrasos) / tp), 1) if tp else 0
        rows.append({"cliente_id": cli["id"], "nome": cli.get("nome", ""), "email": cli.get("email", ""),
                     "score": round(score), "pontualidade": round(taxa), "atraso_medio": atraso_medio_all,
                     "pagas": tp, "total_faturas": len(ff), "vencidas": len(vencidas),
                     "total_pago": round(total_pago, 2), "valor_mensal": valor_mensal})
        if tp:
            soma_pont += taxa; n_pont += 1
        total_receb += total_pago
        if vencidas:
            com_venc += 1
    rows.sort(key=lambda x: (-x["score"], -x["total_pago"]))
    return {"clientes": rows, "resumo": {"total_clientes": len(clientes),
            "pontualidade_media": round(soma_pont / n_pont) if n_pont else 0,
            "total_recebido": round(total_receb, 2), "com_vencidas": com_venc}}


@router.get("/provedor/ranking")
def prov_ranking_page(req: Request):
    return _prov_shell("ranking", "Ranking de Clientes", _PROV_RANKING_BODY, req)


def _mtx_ready():
    """Stream_keys publicando agora (arquivo empurrado pelo storage; fresco < 6min)."""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediamtx_ready.json")
        d = json.load(open(p))
        if time.time() - float(d.get("ts", 0)) > 360:
            return set()
        return set(str(x) for x in d.get("ready", []))
    except Exception:
        return set()


@router.get("/api/comercial/prov/dashboard")
def prov_dashboard(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    cams = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Camera'").fetchall()]
    clientes = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Cliente'").fetchall()]
    fats = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Fatura'").fetchall()]
    try:
        desp = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Despesa'").fetchall()]
    except Exception:
        desp = []
    c.close()
    cams = [x for x in cams if x.get("provedor_id") == pid]
    clientes = [x for x in clientes if x.get("provedor_id") == pid]
    fats = [x for x in fats if x.get("provedor_id") == pid and x.get("cliente_id")]
    desp = [x for x in desp if x.get("provedor_id") == pid]
    cam_manut = sum(1 for x in cams if x.get("status") in ("maintenance", "manutencao"))
    _ready_ol = _mtx_ready()
    cam_online = sum(1 for x in cams if x.get("stream_key") and x.get("stream_key") in _ready_ol)
    ativos = sum(1 for x in clientes if (x.get("status") or "ativo") != "bloqueado")
    bloq = [{"nome": x.get("nome", ""), "motivo": x.get("block_reason") or "Inadimplencia"} for x in clientes if x.get("status") == "bloqueado"]
    a_receber = sum(float(f.get("valor", 0) or 0) for f in fats if f.get("status") == "pendente")
    a_pagar = sum(float(x.get("valor", 0) or 0) for x in desp if (x.get("status") or "pendente") == "pendente")
    pend = sum(1 for f in fats if f.get("status") == "pendente")
    pag = sum(1 for f in fats if f.get("status") == "paga")
    venc = sum(1 for f in fats if f.get("status") == "vencida")
    vencidas = [{"cliente": f.get("cliente_nome", ""), "vencimento": f.get("vencimento", ""), "valor": float(f.get("valor", 0) or 0)} for f in fats if f.get("status") == "vencida"]
    vencidas.sort(key=lambda v: v.get("vencimento") or "")
    total_venc = sum(v["valor"] for v in vencidas)

    def _mk(f):
        rm = (f.get("reference_month") or "")[:7]
        return rm or ((f.get("vencimento") or "")[:7] or None)

    now = datetime.now()

    def _addm(y, m, k):
        t = (y * 12 + (m - 1)) + k
        return (t // 12, t % 12 + 1)

    janelas = [_addm(now.year, now.month, i) for i in range(3)]
    jkeys = set("%04d-%02d" % (y, m) for (y, m) in janelas)
    meses = {}
    for f in fats:
        if f.get("status") not in ("pendente", "vencida"):
            continue
        mk = _mk(f)
        if not mk or mk not in jkeys:
            continue
        val = float(f.get("valor", 0) or 0)
        g = meses.setdefault(mk, {"total": 0.0, "count": 0, "vencido": 0.0})
        g["total"] += val
        g["count"] += 1
        if f.get("status") == "vencida":
            g["vencido"] += val
    MP = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    arm = []
    for (y, m) in janelas:
        mk = "%04d-%02d" % (y, m)
        g = meses.get(mk, {"total": 0.0, "count": 0, "vencido": 0.0})
        arm.append({"mes": mk, "label": MP[m - 1] + " " + str(y), "total": round(g["total"], 2), "count": g["count"], "vencido": round(g["vencido"], 2)})
    return {"cameras": len(cams), "cameras_online": cam_online, "cam_manutencao": cam_manut, "clientes": len(clientes), "ativos": ativos,
            "a_receber": round(a_receber, 2), "a_pagar": round(a_pagar, 2),
            "donut": {"pendentes": pend, "pagas": pag, "vencidas": venc},
            "vencidas": vencidas, "total_vencido": round(total_venc, 2), "bloqueados": bloq,
            "a_receber_mes": arm, "total_receber": round(sum(x["total"] for x in arm), 2)}


_PROV_DEMO_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 12px">Acesso temporario para <b>demonstracao comercial</b>: um login que enxerga SO as cameras escolhidas, por um periodo. Nao gera fatura, cobranca nem bloqueio por inadimplencia.</p>
<div class="cards">
 <div class="kpi"><div class="k">Acessos Ativos</div><div class="v" id="k_at" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Expirados</div><div class="v" id="k_ex" style="color:var(--bad)">-</div></div>
 <div class="kpi"><div class="k">Total Concedidos</div><div class="v" id="k_to">-</div></div></div>

<div style="margin:6px 0 16px"><button class="btn-primary" onclick="abreNovo()">+ Novo Acesso Demo</button></div>

<div class="card">
 <table class="tbl"><thead><tr><th>Usuario</th><th>Cameras</th><th>Expira</th><th>Status</th><th></th></tr></thead>
 <tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
</div>

<div class="ovd" id="ovd"><div class="ovdbox">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0;font-size:17px">Novo Acesso Demonstrador</h3><button class="act" onclick="fechaNovo()" style="font-size:16px">&times;</button></div>
 <p style="color:var(--muted);font-size:12.5px;margin:4px 0 14px">Cria um login temporario de visualizacao. O demonstrador ve apenas as cameras selecionadas ate a data de expiracao.</p>
 <div class="two">
  <div class="fld"><label>Nome do demonstrador</label><input id="d_nome" placeholder="Ex: Prospect Mercado Boa Vista"></div>
  <div class="fld"><label>E-mail (login)</label><input id="d_email" type="email" placeholder="prospect@exemplo.com"></div>
 </div>
 <div class="two">
  <div class="fld"><label>Senha</label><div style="display:flex;gap:8px"><input id="d_senha" style="flex:1"><button class="act" type="button" onclick="gerarSenha()">gerar</button></div></div>
  <div class="fld"><label>Duracao</label><select id="d_dur" onchange="calcExp()"><option value="7">7 dias</option><option value="30">30 dias</option><option value="90">3 meses</option><option value="180">6 meses</option><option value="365">12 meses</option></select><div id="d_exp" style="color:var(--muted);font-size:12px;margin-top:5px"></div></div>
 </div>
 <div class="fld"><label>Cameras <span id="d_cont" style="color:var(--accent)">(0/4)</span> &mdash; maximo 4</label>
  <input id="d_busca" placeholder="buscar por nome ou endereco..." oninput="renderCams()">
  <div id="camlist" style="max-height:240px;overflow:auto;border:1px solid var(--border);border-radius:9px;margin-top:6px"></div>
 </div>
 <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px"><button onclick="fechaNovo()">Cancelar</button><button class="btn-primary" onclick="conceder()">Conceder Acesso</button></div>
</div></div>

<style>
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
.tbl{width:100%;border-collapse:collapse}.tbl th{text-align:left;color:var(--muted);font-size:12px;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border)}.tbl td{padding:10px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:top}
.ovd{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:9999;padding:28px 16px;overflow:auto}.ovd.open{display:flex}
.ovdbox{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px;max-width:620px;width:100%}
.fld{margin-bottom:12px}.fld label{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}.fld input,.fld select{width:100%;padding:9px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);color:var(--ink)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:600px){.two{grid-template-columns:1fr}}
.camrow{display:flex;align-items:center;gap:9px;padding:8px 10px;border-bottom:1px solid var(--border);cursor:pointer}.camrow:hover{background:var(--surface2)}.camrow.sel{background:rgba(139,92,246,.14)}
.bdg{padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600}
</style>
<script>
window.PAGE_INIT=load;
var CAMS=[], SEL=[], ACC=[];
function load(){ carregaCams(); carregaLista(); }
async function carregaCams(){ try{ CAMS=(await api('GET','/api/entities/Camera'))||[]; }catch(e){ CAMS=[]; } }
async function carregaLista(){ try{ var r=await api('GET','/api/prov/demo/listar'); ACC=r.acessos||[]; render(); }catch(e){ msg('Erro: '+e.message); } }
function dtb(s){ if(!s)return '-'; var p=(''+s).slice(0,10).split('-'); return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s; }
function render(){ var at=0,ex=0; ACC.forEach(function(a){ if(a.status==='ativo')at++; else if(a.status==='expirado')ex++; });
 $('k_at').textContent=at; $('k_ex').textContent=ex; $('k_to').textContent=ACC.length;
 $('rows').innerHTML=ACC.map(function(a){
  var bd=a.status==='ativo'?'<span class="bdg" style="background:rgba(34,197,94,.15);color:#22c55e">Ativo</span>':(a.status==='revogado'?'<span class="bdg" style="background:rgba(148,163,184,.15);color:#94a3b8">Revogado</span>':'<span class="bdg" style="background:rgba(239,68,68,.15);color:#ef4444">Expirado</span>');
  var cn=(a.camera_nomes||[]).join(', ');
  return '<tr><td><b>'+esc(a.user_nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(a.user_email||'')+'</div></td>'+
   '<td>'+((a.cameras||[]).length)+' cam'+(cn?('<div style="color:var(--muted);font-size:11.5px">'+esc(cn)+'</div>'):'')+'</td>'+
   '<td>'+dtb(a.expira)+'</td><td>'+bd+'</td>'+
   '<td style="text-align:right">'+(a.status==='ativo'?'<button class="act" style="color:var(--bad)" data-rev="'+esc(a.id)+'">revogar</button>':'')+'</td></tr>';
 }).join('')||'<tr><td colspan="5" class="center">Nenhum acesso demo ainda.</td></tr>'; }
function abreNovo(){ $('d_nome').value=''; $('d_email').value=''; $('d_senha').value=''; $('d_dur').value='7'; $('d_busca').value=''; SEL=[]; calcExp(); renderCams(); $('ovd').classList.add('open'); }
function fechaNovo(){ $('ovd').classList.remove('open'); }
function gerarSenha(){ $('d_senha').value='Demo'+Math.floor(1000+Math.random()*9000); }
function calcExp(){ var d=parseInt($('d_dur').value||7); var t=new Date(); t.setDate(t.getDate()+d); $('d_exp').textContent='Expira em '+dtb(t.toISOString()); }
function renderCams(){ var q=($('d_busca').value||'').toLowerCase();
 var arr=CAMS.filter(function(c){ var nm=((c.nome||c.name||'')+' '+(c.endereco||c.local||'')).toLowerCase(); return !q||nm.indexOf(q)>=0; }).slice(0,60);
 $('camlist').innerHTML=arr.map(function(c){ var s=SEL.indexOf(c.id)>=0; return '<div class="camrow'+(s?' sel':'')+'" data-id="'+esc(c.id)+'"><input type="checkbox" '+(s?'checked':'')+' style="width:auto" tabindex="-1"><div><b>'+esc(c.nome||c.name||'camera')+'</b><div style="color:var(--muted);font-size:11px">'+esc(c.endereco||c.local||'')+'</div></div></div>'; }).join('')||'<div class="center" style="padding:16px;color:var(--muted)">Nenhuma camera encontrada.</div>';
 $('d_cont').textContent='('+SEL.length+'/4)'; }
document.addEventListener('click',function(e){
 var rv=(e.target&&e.target.getAttribute)?e.target.getAttribute('data-rev'):null;
 if(rv){ revogar(rv); return; }
 var row=e.target.closest?e.target.closest('.camrow'):null;
 if(row&&document.getElementById('camlist')&&document.getElementById('camlist').contains(row)){
  var id=row.getAttribute('data-id'); var i=SEL.indexOf(id);
  if(i>=0){ SEL.splice(i,1); } else { if(SEL.length>=4){ msg('Maximo de 4 cameras'); return; } SEL.push(id); }
  renderCams();
 }
});
async function conceder(){ var b={full_name:$('d_nome').value.trim(),email:$('d_email').value.trim(),password:$('d_senha').value,dias:parseInt($('d_dur').value||7),cameras:SEL};
 if(!b.email||(''+b.password).length<4){ msg('Informe e-mail e senha (min 4).'); return; }
 if(!SEL.length){ msg('Escolha de 1 a 4 cameras.'); return; }
 try{ var r=await api('POST','/api/prov/demo/criar',b); fechaNovo(); msg('Acesso concedido ate '+dtb(r.expira)+'.',true); carregaLista(); }catch(e){ msg('Erro: '+e.message); } }
async function revogar(id){ if(!confirm('Revogar este acesso demo? O login perde acesso imediatamente.'))return; try{ await api('POST','/api/prov/demo/'+id+'/revogar'); msg('Acesso revogado.',true); carregaLista(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


@router.get("/provedor/demonstrador")
def prov_demo_page(req: Request):
    return _prov_shell("demonstrador", "Usuario Demonstrador", _PROV_DEMO_BODY, req)


_PROV_EQUIPE_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 12px">Crie logins para sua equipe (financeiro, vendas, suporte) e defina exatamente quais menus cada um pode ver e acessar no painel.</p>
<div class="cards">
 <div class="kpi"><div class="k">Usuarios da Equipe</div><div class="v" id="k_to">-</div></div>
 <div class="kpi"><div class="k">Ativos</div><div class="v" id="k_at" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Bloqueados</div><div class="v" id="k_bl" style="color:var(--bad)">-</div></div></div>
<div style="margin:6px 0 16px"><button class="btn-primary" onclick="abreNovo()">+ Novo Usuario</button></div>
<div class="card">
 <table class="tbl"><thead><tr><th>Usuario</th><th>Menus liberados</th><th>Status</th><th></th></tr></thead>
 <tbody id="rows"><tr><td colspan="4" class="center">carregando...</td></tr></tbody></table>
</div>

<div class="ovd" id="ovnew"><div class="ovdbox" style="max-width:460px">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0;font-size:17px">Novo Usuario</h3><button class="act" onclick="fecha('ovnew')" style="font-size:16px">&times;</button></div>
 <div class="fld"><label>Nome</label><input id="n_nome"></div>
 <div class="fld"><label>E-mail (login)</label><input id="n_email" type="email"></div>
 <div class="fld"><label>Senha</label><div style="display:flex;gap:8px"><input id="n_senha" style="flex:1"><button class="act" type="button" onclick="genPw()">gerar</button></div></div>
 <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px"><button onclick="fecha('ovnew')">Cancelar</button><button class="btn-primary" onclick="criar()">Criar e definir permissoes</button></div>
</div></div>

<div class="ovd" id="ovperm"><div class="ovdbox" style="max-width:520px">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0;font-size:17px">Permissoes de Acesso aos Menus</h3><button class="act" onclick="fecha('ovperm')" style="font-size:16px">&times;</button></div>
 <div id="perm_nome" style="color:var(--muted);font-size:13px;margin:2px 0 12px"></div>
 <div id="permlist" style="max-height:340px;overflow:auto"></div>
 <div style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.35);border-radius:9px;padding:10px 12px;font-size:12px;color:var(--muted);margin-top:12px"><b style="color:#f59e0b">Atencao:</b> as permissoes definidas aqui controlam quais menus o usuario pode visualizar e acessar no sistema.</div>
 <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px"><button onclick="fecha('ovperm')">Cancelar</button><button class="btn-primary" onclick="salvarPerms()">Salvar Permissoes</button></div>
</div></div>

<style>
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
.tbl{width:100%;border-collapse:collapse}.tbl th{text-align:left;color:var(--muted);font-size:12px;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border)}.tbl td{padding:10px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
.ovd{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:9999;padding:28px 16px;overflow:auto}.ovd.open{display:flex}
.ovdbox{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px;width:100%}
.fld{margin-bottom:12px}.fld label{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}.fld input{width:100%;padding:9px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);color:var(--ink)}
.prow{display:flex;align-items:center;gap:10px;padding:9px 6px;border-bottom:1px solid var(--border);cursor:pointer}.prow:hover{background:var(--surface2)}
.bdg{padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600}
</style>
<script>
window.PAGE_INIT=load;
var USERS=[], MENUS=[], curUid=null;
function load(){ carregaMenus(); lista(); }
async function carregaMenus(){ try{ MENUS=(await api('GET','/api/comercial/prov/menus'))||[]; }catch(e){ MENUS=[]; } }
async function lista(){ try{ var r=await api('GET','/api/prov/equipe/listar'); USERS=r.usuarios||[]; render(); }catch(e){ msg('Erro: '+e.message); } }
function render(){ var to=0,at=0,bl=0; USERS.forEach(function(u){ if(u.owner)return; to++; if(u.status==='ativo')at++; else bl++; });
 $('k_to').textContent=to; $('k_at').textContent=at; $('k_bl').textContent=bl;
 $('rows').innerHTML=USERS.map(function(u){
  if(u.owner){ return '<tr><td><b>'+esc(u.full_name||u.email)+'</b> <span class="bdg" style="background:rgba(139,92,246,.15);color:#a78bfa">Dono</span><div style="color:var(--muted);font-size:12px">'+esc(u.email)+'</div></td><td style="color:var(--muted)">Acesso total</td><td><span class="bdg" style="background:rgba(34,197,94,.15);color:#22c55e">Ativo</span></td><td></td></tr>'; }
  var st=u.status==='ativo'?'<span class="bdg" style="background:rgba(34,197,94,.15);color:#22c55e">Ativo</span>':'<span class="bdg" style="background:rgba(239,68,68,.15);color:#ef4444">Bloqueado</span>';
  return '<tr><td><b>'+esc(u.full_name||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(u.email)+'</div></td>'+
   '<td>'+((u.menu_perms||[]).length)+' menu(s)</td><td>'+st+'</td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" data-perm="'+esc(u.id)+'">permissoes</button>'+
   '<button class="act" data-tog="'+esc(u.id)+'" style="color:'+(u.status==='ativo'?'var(--bad)':'var(--ok)')+'">'+(u.status==='ativo'?'bloquear':'ativar')+'</button>'+
   '<button class="act" data-del="'+esc(u.id)+'" style="color:var(--bad)">excluir</button></td></tr>';
 }).join('')||'<tr><td colspan="4" class="center">Nenhum usuario de equipe. Clique em Novo Usuario.</td></tr>'; }
function abreNovo(){ $('n_nome').value=''; $('n_email').value=''; $('n_senha').value=''; $('ovnew').classList.add('open'); }
function fecha(id){ $(id).classList.remove('open'); }
function genPw(){ $('n_senha').value='Eq'+Math.floor(100000+Math.random()*900000); }
async function criar(){ var b={full_name:$('n_nome').value.trim(),email:$('n_email').value.trim(),password:$('n_senha').value,menu_perms:['']};
 if(!b.full_name||!b.email||(''+b.password).length<4){ msg('Preencha nome, e-mail e senha (min 4).'); return; }
 try{ var r=await api('POST','/api/prov/equipe/criar',b); fecha('ovnew'); msg('Usuario criado. Agora defina as permissoes.',true); await lista(); abrePerm(r.id); }catch(e){ msg('Erro: '+e.message); } }
function abrePerm(uid){ var u=USERS.filter(function(x){return x.id===uid})[0]; if(!u)return; curUid=uid;
 $('perm_nome').textContent=(u.full_name||'')+' - '+(u.email||''); var sel=u.menu_perms||[];
 $('permlist').innerHTML=MENUS.map(function(m){ var c=sel.indexOf(m.slug)>=0; return '<label class="prow"><input type="checkbox" data-slug="'+esc(m.slug)+'" '+(c?'checked':'')+' style="width:auto"> '+esc(m.label)+'</label>'; }).join('');
 $('ovperm').classList.add('open'); }
async function salvarPerms(){ var ck=$('permlist').querySelectorAll('input[type=checkbox]'); var perms=[]; for(var i=0;i<ck.length;i++){ if(ck[i].checked)perms.push(ck[i].getAttribute('data-slug')); }
 try{ await api('POST','/api/prov/equipe/'+curUid+'/perms',{menu_perms:perms}); fecha('ovperm'); msg('Permissoes salvas.',true); lista(); }catch(e){ msg('Erro: '+e.message); } }
document.addEventListener('click',function(e){ var t=e.target; if(!t||!t.getAttribute)return;
 var p=t.getAttribute('data-perm'); if(p){ abrePerm(p); return; }
 var tg=t.getAttribute('data-tog'); if(tg){ toggle(tg); return; }
 var d=t.getAttribute('data-del'); if(d){ excluir(d); return; }
});
async function toggle(uid){ var u=USERS.filter(function(x){return x.id===uid})[0]; if(!u)return; var bloquear=(u.status==='ativo'); if(bloquear&&!confirm('Bloquear o acesso deste usuario?'))return;
 try{ await api('POST','/api/prov/equipe/'+uid+'/status',{bloquear:bloquear}); lista(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(uid){ if(!confirm('Excluir este usuario? O login sera removido.'))return; try{ await api('DELETE','/api/prov/equipe/'+uid); msg('Usuario excluido.',true); lista(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


@router.get("/api/comercial/prov/menus")
def prov_menus(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    return [{"slug": s, "label": l} for s, l, _t in _PROV_ITENS if s != "gestao-usuarios"]


@router.get("/provedor/gestao-usuarios")
def prov_equipe_page(req: Request):
    return _prov_shell("gestao-usuarios", "Gestao de Usuarios", _PROV_EQUIPE_BODY, req)


@router.get("/provedor/{slug}")
def prov_generico(slug: str, req: Request):
    titulo = next((t for s, l, t in _PROV_ITENS if s == slug), slug)
    body = ('<div class="center" style="padding:70px"><div style="font-size:18px;color:var(--ink);margin-bottom:8px">%s</div>'
            '<div>Em construcao - proxima entrega.</div></div>' % titulo)
    return _prov_shell(slug, titulo, body, req)


@router.get("/api/comercial/prov/resumo")
def prov_resumo(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    clientes = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Cliente'").fetchall()]
    fats = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='Fatura'").fetchall()]
    c.close()
    clientes = [x for x in clientes if x.get("provedor_id") == pid]
    fats = [x for x in fats if x.get("provedor_id") == pid]
    return {"clientes": len(clientes),
            "ativos": sum(1 for x in clientes if (x.get("status") or "ativo") != "bloqueado"),
            "bloqueados": sum(1 for x in clientes if x.get("status") == "bloqueado"),
            "a_receber": sum(float(f.get("valor", 0) or 0) for f in fats if f.get("status") == "pendente"),
            "vencido": sum(float(f.get("valor", 0) or 0) for f in fats if f.get("status") == "vencida"),
            "recebido": sum(float(f.get("valor", 0) or 0) for f in fats if f.get("status") == "paga")}


@router.get("/api/comercial/prov/clientes")
def prov_clientes_api(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Cliente'").fetchall(); c.close()
    out = [dict(json.loads(r["data"]), id=r["id"]) for r in rows]
    return [x for x in out if x.get("provedor_id") == pid]


@router.post("/api/comercial/prov/clientes/salvar")
async def prov_cliente_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    data = {"nome": (b.get("nome") or "").strip(), "document_type": b.get("document_type", "cnpj"),
            "document_number": (b.get("document_number") or "").strip(), "email": (b.get("email") or "").strip(),
            "telefone": (b.get("telefone") or "").strip(), "plano_id": b.get("plano_id", ""), "plano_nome": b.get("plano_nome", ""),
            "valor_mensal": float(b.get("valor_mensal", 0) or 0), "cep": (b.get("cep") or "").strip(), "numero": (b.get("numero") or "").strip(), "endereco": (b.get("endereco") or "").strip(), "bairro": (b.get("bairro") or "").strip(), "complemento": (b.get("complemento") or "").strip(), "cidade": (b.get("cidade") or "").strip(), "uf": (b.get("uf") or "").strip(), "provedor_id": pid, "status": b.get("status", "ativo")}
    if not data["nome"]:
        return JSONResponse({"error": "informe o nome"}, status_code=400)
    cid = (b.get("id") or "").strip()
    if cid:
        ex = _get_ent("Cliente", cid)
        if not ex or ex.get("provedor_id") != pid:
            return JSONResponse({"error": "sem permissao"}, status_code=403)
        _update_ent("Cliente", cid, data)
    else:
        data["criado_em"] = _now_iso(); _create_ent("Cliente", data)
    return {"success": True}


@router.post("/api/comercial/prov/clientes/{cid}/status")
async def prov_cliente_status(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("Cliente", cid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    if b.get("bloquear"):
        _update_ent("Cliente", cid, {"status": "bloqueado",
                    "block_reason": (b.get("motivo") or "Bloqueado manualmente pelo provedor."),
                    "bloqueio_auto": False, "payment_promise_active": False, "payment_promise_date": None})
        _cliente_set_acesso(cid, False)
        return {"success": True, "status": "bloqueado"}
    _update_ent("Cliente", cid, {"status": "ativo", "block_reason": None, "bloqueio_auto": False,
                "payment_promise_active": False, "payment_promise_date": None,
                "payment_promise_set_by": None, "payment_promise_set_at": None})
    _cliente_set_acesso(cid, True)
    return {"success": True, "status": "ativo"}


@router.post("/api/comercial/prov/clientes/{cid}/desbloquear")
async def prov_cliente_desbloquear(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("Cliente", cid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    modo = (b.get("modo") or "simples").strip()
    patch = {"status": "ativo", "block_reason": None, "bloqueio_auto": False}
    if modo == "promessa":
        data = (b.get("promessa_data") or "").strip()[:10]
        if not data:
            return JSONResponse({"error": "Data obrigatoria"}, status_code=400)
        try:
            dprom = datetime.strptime(data, "%Y-%m-%d").date()
        except Exception:
            return JSONResponse({"error": "Data invalida"}, status_code=400)
        if dprom <= datetime.now().date():
            return JSONResponse({"error": "A data da promessa deve ser futura"}, status_code=400)
        try:
            quem = u.get("full_name") or u.get("email") or "Provedor"
        except Exception:
            quem = "Provedor"
        patch.update({"payment_promise_active": True, "payment_promise_date": data,
                      "payment_promise_set_by": quem, "payment_promise_set_at": _now_iso()})
    else:
        patch.update({"payment_promise_active": False, "payment_promise_date": None,
                      "payment_promise_set_by": None, "payment_promise_set_at": None})
    _update_ent("Cliente", cid, patch)
    _cliente_set_acesso(cid, True)
    return {"success": True, "modo": modo, "promessa": patch.get("payment_promise_date")}


@router.delete("/api/comercial/prov/clientes/{cid}")
def prov_cliente_del(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("Cliente", cid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); c.execute("DELETE FROM entities WHERE entity='Cliente' AND id=?", (cid,)); c.commit(); c.close()
    return {"success": True}


@router.get("/api/comercial/prov/faturas")
def prov_faturas_api(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Fatura'").fetchall(); c.close()
    out = [dict(json.loads(r["data"]), id=r["id"]) for r in rows]
    return [x for x in out if x.get("provedor_id") == pid and x.get("cliente_id")]


@router.post("/api/comercial/prov/faturas/sync")
async def prov_faturas_sync(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if asaas is None:
        return JSONResponse({"error": "modulo asaas indisponivel"}, status_code=500)
    key = _cred(pid).get("asaas_api_key")
    if not key:
        return JSONResponse({"error": "configure a sua chave Asaas antes (fale com a Corexia / Credenciais)"}, status_code=400)
    corte = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    try:
        names = {}; off = 0
        while off < 3000:
            pg = asaas.list_customers_page(offset=off, limit=100, api_key=key)
            for cst in pg.get("data", []):
                names[cst["id"]] = cst.get("name") or cst.get("company") or ""
            if not pg.get("hasMore"):
                break
            off += 100
        n = 0; off = 0
        while off < 3000:
            pg = asaas.list_payments_page(offset=off, limit=100, api_key=key, extra={"dueDate[ge]": corte})
            for p in pg.get("data", []):
                _upsert_fatura(p, names.get(p.get("customer", ""), ""), provedor_id=pid)
                n += 1
            if not pg.get("hasMore"):
                break
            off += 100
    except Exception as e:
        return JSONResponse({"error": "Asaas: " + str(getattr(e, "body", e))[:200]}, status_code=400)
    return {"success": True, "sincronizadas": n}


# ---------- Cameras & IA do provedor: catalogo de modulos + planos ----------
# Modelo de cobranca da IA por camera/mes. IA Corexia = pacote unico (marca deteccoes num modal).
IA_MODULOS = [
    {"key": "corexia", "nome": "IA Corexia", "valor": 27.0, "pacote": True,
     "analiticos": [["arma_fogo", "Arma de fogo"], ["arma_branca", "Arma branca / faca"],
                    ["animal", "Animal"], ["intruso", "Zona de intrusao"],
                    ["toca_ninja", "Toca ninja / rosto coberto"], ["linha", "Linha virtual"],
                    ["pessoa", "Pessoa"]]},
    {"key": "fogo", "nome": "IA Fogo / Fumaca", "valor": 27.0, "analiticos": [["fogo", "Fogo / Fumaca"]]},
    {"key": "veiculos", "nome": "IA Veiculos", "valor": 27.0, "analiticos": [["veiculo", "Modelo de veiculo"]]},
    {"key": "epi", "nome": "IA EPI", "valor": 15.0, "analiticos": [["epi", "EPI"]]},
    {"key": "placa", "nome": "IA Placa / LPR", "valor": 77.0, "requer_entrada": True, "analiticos": [["placa", "Placa (LPR)"]]},
    {"key": "heatmap", "nome": "IA Mapa de calor", "valor": 47.0, "requer_zona": True, "analiticos": [["heatmap", "Mapa de calor"]]},
    {"key": "piscina", "nome": "IA Piscina / afogamento", "valor": 87.0, "requer_zona": True, "analiticos": [["piscina", "Piscina / afogamento"]]},
]
GRAV_TIERS = [{"dias": 1, "valor": 9.97}, {"dias": 3, "valor": 14.97}, {"dias": 5, "valor": 20.0},
              {"dias": 7, "valor": 24.97}, {"dias": 15, "valor": 39.97}, {"dias": 30, "valor": 69.97},
              {"dias": 60, "valor": 129.97}, {"dias": 90, "valor": 179.97}, {"dias": 366, "valor": 597.97}]


def _plano_do_prov(pid):
    prov = _get_ent("Provedor", pid) or {}
    plano = _get_ent("Plano", prov.get("plano_id", "")) if prov.get("plano_id") else None
    return prov, (plano or {})


def _ia_map():
    m = {}
    for mod in IA_MODULOS:
        for a in mod["analiticos"]:
            m[a[0]] = mod["key"]
    return m


def _modulos_ativos_cam(cfg):
    """Modulos de IA ativos numa camera, a partir da config granular (analiticos_padrao + horarios)."""
    if not cfg:
        return set()
    ativos = set(cfg.get("analiticos_padrao") or [])
    for h in (cfg.get("horarios") or []):
        ativos.update(h.get("analiticos") or [])
    mapa = _ia_map()
    return {mapa[a] for a in ativos if a in mapa}


def _grav_valor(dias):
    dias = int(dias or 0)
    if dias <= 0:
        return 0.0
    for t in GRAV_TIERS:
        if dias <= t["dias"]:
            return t["valor"]
    return GRAV_TIERS[-1]["valor"]


# ---------- Gravacao em nuvem (MediaMTX record) - Fase 4b ----------
MTX_API = os.getenv("MTX_API", "http://127.0.0.1:9997")
MTX_PLAYBACK = os.getenv("MTX_PLAYBACK", "http://127.0.0.1:9996")
REC_ENABLED = os.getenv("COREXIA_REC_ENABLED", "0") == "1"   # desligado ate ter storage dedicado


def _mediamtx_set_record(stream_key, dias):
    """Liga/desliga a gravacao nativa do MediaMTX (path cam/<key>), retencao = dias.
    Gated por COREXIA_REC_ENABLED: enquanto nao ha storage, e no-op (dias fica so p/ cobranca)."""
    if not stream_key or not REC_ENABLED:
        return False
    path = "cam/%s" % stream_key
    try:
        d = int(dias or 0)
        if d > 0:
            body = {"record": True, "recordDeleteAfter": "%dh" % (d * 24)}
            r = requests.post("%s/v3/config/paths/add/%s" % (MTX_API, path), json=body, timeout=8)
            if r.status_code != 200:
                requests.patch("%s/v3/config/paths/patch/%s" % (MTX_API, path), json=body, timeout=8)
        else:
            requests.delete("%s/v3/config/paths/delete/%s" % (MTX_API, path), timeout=8)
        return True
    except Exception as e:
        print("[rec] mediamtx set_record erro:", e)
        return False


# ---------- Cameras & IA do provedor (escopado por provedor_id da camera) ----------
@router.get("/api/comercial/prov/cameras")
def prov_cameras_api(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    prov, plano = _plano_do_prov(pid)
    gravacao = plano.get("gravacao", "")
    c = _db()
    cams = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall()
    cfgs_rows = c.execute("SELECT data FROM entities WHERE entity='ConfigAnalitico'").fetchall()
    c.close()
    cfgs = {}
    for r in cfgs_rows:
        d = json.loads(r["data"])
        if d.get("camera_id"):
            cfgs[d["camera_id"]] = {"ativo": d.get("ativo", True), "horarios": d.get("horarios", []),
                                    "analiticos_padrao": d.get("analiticos_padrao", []),
                                    "zonas_intrusao": d.get("zonas_intrusao", [])}
    mtx_online = _mtx_ready()
    out = []
    for r in cams:
        o = json.loads(r["data"])
        if o.get("provedor_id") != pid:
            continue
        try:
            lat = float(o.get("latitude")) if o.get("latitude") not in (None, "") else None
            lng = float(o.get("longitude")) if o.get("longitude") not in (None, "") else None
        except (TypeError, ValueError):
            lat = lng = None
        sk = o.get("stream_key", "")
        st = ("online" if sk in mtx_online else "offline") if sk else o.get("status", "")
        proto = (o.get("protocolo", "") or "rtmp")
        out.append({"id": r["id"], "nome": o.get("nome", ""), "status": st,
                    "cliente_nome": o.get("cliente_nome", ""),
                    "ia_placa": str(o.get("ia_placa")).lower() == "true",
                    "embed_url": o.get("embed_url", ""), "latitude": lat, "longitude": lng,
                    "cep": o.get("cep", ""), "endereco": o.get("endereco", "") or o.get("localizacao", ""),
                    "bairro": o.get("bairro", ""), "cidade": o.get("cidade", ""), "uf": o.get("uf", ""),
                    "stream_key": sk, "rtmp_ingest": o.get("rtmp_ingest", ""),
                    "usuario": o.get("usuario", ""), "protocolo": proto,
                    "grava_audio": bool(o.get("grava_audio")), "fuso": o.get("fuso", "America/Sao_Paulo"),
                    "publico": bool(o.get("publico", True)),
                    "rtsp_src": (o.get("rtsp_url", "") if proto == "rtsp" else ""),
                    "exclusao_pendente": bool(o.get("exclusao_pendente")),
                    "dias_gravacao": int(o.get("dias_gravacao", 0) or 0),
                    "config": cfgs.get(r["id"])})
    out.sort(key=lambda x: (x["nome"] or "").lower())
    return {"plano_id": prov.get("plano_id", ""), "plano_nome": prov.get("plano_nome", "") or plano.get("nome", ""),
            "gravacao": gravacao, "modulos": IA_MODULOS, "grav_tiers": GRAV_TIERS, "cameras": out}


def _prov_camera_own(pid, cid):
    cam = _get_ent("Camera", cid)
    return cam if (cam and cam.get("provedor_id") == pid) else None


@router.post("/api/comercial/prov/analiticos/salvar")
async def prov_analiticos_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cid = (b.get("camera_id") or "").strip()
    if not cid or not _prov_camera_own(pid, cid):
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    data = {"camera_id": cid, "camera_nome": b.get("camera_nome", ""), "ativo": bool(b.get("ativo", True)),
            "horarios": b.get("horarios", []) or [], "analiticos_padrao": b.get("analiticos_padrao", []) or [],
            "zonas_intrusao": b.get("zonas_intrusao", []) or []}
    c = _db()
    row = c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
    c.close()
    if row:
        _update_ent("ConfigAnalitico", row["id"], data)
    else:
        _create_ent("ConfigAnalitico", data)
    # Fase 4: liga o detector NVDEC nesta camera quando ha IA ativa (analisa o rtmp do MediaMTX).
    _ativos = set(data["analiticos_padrao"])
    for _h in data["horarios"]:
        _ativos.update(_h.get("analiticos") or [])
    _ia_on = bool(data["ativo"]) and len(_ativos) > 0
    _cam = _get_ent("Camera", cid) or {}
    _patch = {"ia_placa": ("placa" in _ativos)}
    if _ia_on:
        _patch["decode_engine"] = "nvdec"
    elif _cam.get("stream_key"):
        _patch["decode_engine"] = ""
    _update_ent("Camera", cid, _patch)
    return {"success": True}


@router.post("/api/comercial/prov/analiticos/limpar")
async def prov_analiticos_limpar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cid = (b.get("camera_id") or "").strip()
    if not cid or not _prov_camera_own(pid, cid):
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    c = _db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='ConfigAnalitico'").fetchall()
    n = 0
    for r in rows:
        if json.loads(r["data"]).get("camera_id") == cid:
            c.execute("DELETE FROM entities WHERE entity='ConfigAnalitico' AND id=?", (r["id"],)); n += 1
    c.commit(); c.close()
    # Fase 4: sem IA -> tira a camera MediaMTX do detector NVDEC.
    _cam = _get_ent("Camera", cid) or {}
    _patch = {"ia_placa": False}
    if _cam.get("stream_key"):
        _patch["decode_engine"] = ""
    _update_ent("Camera", cid, _patch)
    return {"success": True, "removidas": n}


@router.post("/api/comercial/prov/cameras/{cid}/gravacao")
async def prov_camera_gravacao(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if not _prov_camera_own(pid, cid):
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    _prov, plano = _plano_do_prov(pid)
    if plano.get("gravacao") != "cloud":
        return JSONResponse({"error": "gravacao em nuvem so no plano Cloud"}, status_code=400)
    b = await req.json()
    dias = int(b.get("dias", 0) or 0)
    _update_ent("Camera", cid, {"dias_gravacao": dias})
    cam2 = _get_ent("Camera", cid) or {}
    ativa = _mediamtx_set_record(cam2.get("stream_key"), dias)
    return {"success": True, "dias": dias, "valor": _grav_valor(dias), "gravacao_ativa": ativa}


@router.get("/api/comercial/prov/gravacoes")
def prov_gravacoes_list(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    prov, plano = _plano_do_prov(pid)
    c = _db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall()
    c.close()
    cams = []
    for r in rows:
        o = json.loads(r["data"])
        if o.get("provedor_id") != pid:
            continue
        dias = int(o.get("dias_gravacao", 0) or 0)
        cams.append({"id": r["id"], "nome": o.get("nome", ""), "dias_gravacao": dias,
                     "tem_key": bool(o.get("stream_key")), "valor": _grav_valor(dias)})
    cams.sort(key=lambda x: (-x["dias_gravacao"], (x["nome"] or "").lower()))
    return {"success": True, "plano": plano.get("gravacao", ""), "rec_ativa": REC_ENABLED, "cameras": cams}


@router.get("/api/comercial/prov/gravacoes/{cid}/segmentos")
def prov_gravacoes_segmentos(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cam = _prov_camera_own(pid, cid)
    if not cam:
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    key = cam.get("stream_key")
    if not key:
        return {"success": True, "segmentos": []}
    try:
        r = requests.get("%s/list?path=cam/%s" % (MTX_PLAYBACK, key), timeout=8)
        segs = r.json() if r.status_code == 200 else []
    except Exception as e:
        return {"success": True, "segmentos": [], "erro": str(e)}
    out = [{"start": s.get("start"), "duration": s.get("duration")} for s in (segs or [])]
    return {"success": True, "segmentos": out}


@router.get("/api/comercial/prov/gravacoes/{cid}/video")
def prov_gravacoes_video(cid: str, req: Request):
    tok = (req.query_params.get("token") or "").strip()
    u, pid = None, ""
    if tok:
        c = _db()
        r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
        c.close()
        u = dict(r) if (r and r["status"] == "ativo") else None
        pid = (u.get("provedor_id") or "").strip() if u else ""
    else:
        u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cam = _prov_camera_own(pid, cid)
    if not cam:
        return JSONResponse({"error": "sem acesso"}, status_code=403)
    key = cam.get("stream_key")
    start = req.query_params.get("start", "")
    dur = req.query_params.get("duration", "")
    if not key or not start:
        return JSONResponse({"error": "parametros"}, status_code=400)
    import urllib.parse
    from fastapi.responses import StreamingResponse
    url = "%s/get?path=%s&start=%s&duration=%s" % (MTX_PLAYBACK,
        urllib.parse.quote("cam/%s" % key, safe=""), urllib.parse.quote(start, safe=""),
        urllib.parse.quote(str(dur), safe=""))
    def _gen():
        with requests.get(url, stream=True, timeout=60) as rr:
            for chunk in rr.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
    return StreamingResponse(_gen(), media_type="video/mp4")


def _custo_provedor(pid):
    prov, plano = _plano_do_prov(pid)
    gravacao = plano.get("gravacao", "")
    c = _db()
    cfgs = {}
    for r in c.execute("SELECT data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
        d = json.loads(r["data"])
        if d.get("camera_id"):
            cfgs[d["camera_id"]] = d
    rows = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall()
    c.close()
    my = [(r["id"], _o) for r in rows for _o in [json.loads(r["data"])] if _o.get("provedor_id") == pid and _o.get("migracao_status") != "pendente_analitico"]
    ncams = len(my)
    if gravacao == "local":
        base = float(plano.get("base_mensal", 0) or 0)
        incl = int(plano.get("cameras_ao_vivo_incluidas", 0) or 0)
        extra_unit = float(plano.get("camera_extra_mensal", 0) or 0)
        n_extra = max(0, ncams - incl)
        painel = base + n_extra * extra_unit
        painel_desc = "Base R$ %.2f (ate %d cameras) + %d extra x R$ %.2f" % (base, incl, n_extra, extra_unit)
    elif gravacao == "cloud":
        unit = float(plano.get("camera_ao_vivo_mensal", 0) or 0)
        n_rec = sum(1 for _c, o in my if int(o.get("dias_gravacao", 0) or 0) > 0)
        n_live = ncams - n_rec
        painel = n_live * unit
        painel_desc = "%d cameras ao vivo x R$ %.2f (as %d com gravacao ja incluem o ao vivo no valor da gravacao)" % (n_live, unit, n_rec)
    else:
        painel = 0.0
        painel_desc = "(provedor sem plano definido - fale com a Corexia)"
    ia_valor = {m["key"]: m["valor"] for m in IA_MODULOS}
    ia_nome = {m["key"]: m["nome"] for m in IA_MODULOS}
    ia_count = {}
    for cid, _o in my:
        for mk in _modulos_ativos_cam(cfgs.get(cid)):
            ia_count[mk] = ia_count.get(mk, 0) + 1
    ia_detalhe = [{"modulo": ia_nome[k], "cameras": v, "valor_unit": ia_valor[k], "subtotal": round(v * ia_valor[k], 2)}
                  for k, v in ia_count.items()]
    ia_total = round(sum(x["subtotal"] for x in ia_detalhe), 2)
    grav_total = 0.0
    grav_cams = 0
    if gravacao == "cloud":
        for _cid, o in my:
            dv = _grav_valor(o.get("dias_gravacao", 0))
            if dv > 0:
                grav_cams += 1
                grav_total += dv
    grav_total = round(grav_total, 2)
    total = round(painel + ia_total + grav_total, 2)
    return {"plano_nome": prov.get("plano_nome", "") or plano.get("nome", ""), "gravacao": gravacao,
            "cameras": ncams, "painel": round(painel, 2), "painel_desc": painel_desc,
            "ia_total": ia_total, "ia_detalhe": ia_detalhe,
            "grav_total": grav_total, "grav_cams": grav_cams, "total": total}


@router.get("/api/comercial/prov/custo")
def prov_custo(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    return _custo_provedor(pid)


@router.get("/api/comercial/provedores/custos")
def provedores_custos(req: Request):
    u = _current_user(req)
    if not u or u.get("role") != "admin":
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    ids = [r["id"] for r in c.execute("SELECT id FROM entities WHERE entity='Provedor'").fetchall()]
    c.close()
    out = {}
    for _pid in ids:
        try:
            d = _custo_provedor(_pid)
            out[_pid] = {"total": d["total"], "painel": d["painel"], "ia_total": d["ia_total"],
                         "grav_total": d["grav_total"], "gravacao": d["gravacao"], "cameras": d["cameras"]}
        except Exception as _e:
            out[_pid] = {"total": 0, "erro": str(_e)[:80]}
    return {"custos": out}


@router.post("/api/comercial/provedores/{pid}/cobrar")
async def provedor_cobrar(pid: str, req: Request):
    # Cobranca REAL Nivel 1 (Corexia -> Provedor) a partir do custo calculado. Admin, confirmada no front.
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if asaas is None:
        return JSONResponse({"error": "modulo asaas indisponivel"}, status_code=500)
    prov = _get_ent("Provedor", pid)
    if not prov:
        return JSONResponse({"error": "provedor nao encontrado"}, status_code=404)
    b = await req.json()
    tipo = (b.get("tipo") or "avulsa").lower()
    forma = (b.get("forma") or "PIX").upper()
    try:
        valor = round(float(b.get("valor")), 2) if b.get("valor") not in (None, "") else float(_custo_provedor(pid)["total"])
    except (TypeError, ValueError):
        valor = float(_custo_provedor(pid)["total"])
    if valor <= 0:
        return JSONResponse({"error": "valor zerado - defina IA/gravacao ou informe um valor"}, status_code=400)
    doc = (prov.get("document_number") or "").strip()
    if not doc:
        return JSONResponse({"error": "cadastre o CPF/CNPJ do provedor (botao editar) antes de cobrar"}, status_code=400)
    desc = "Corexia - " + (prov.get("plano_nome", "") or "mensalidade")
    try:
        cid = prov.get("asaas_customer_id") or ""
        if (not cid) or cid.startswith("TESTE_"):
            cust = asaas.create_customer(name=prov.get("nome", ""), cpf_cnpj=doc,
                    email=prov.get("email", ""), phone=prov.get("telefone", ""), external_ref="provedor:" + pid)
            cid = cust["id"]
            _update_ent("Provedor", pid, {"asaas_customer_id": cid, "document_number": doc})
        if tipo == "assinatura":
            due = (datetime.now() + timedelta(days=int(b.get("vencimento_dias", 5) or 5))).strftime("%Y-%m-%d")
            sub = asaas.create_subscription(customer=cid, value=valor, next_due_date=due,
                    billing_type=forma, description=desc, external_ref="provedor:" + pid)
            _update_ent("Provedor", pid, {"asaas_subscription_id": sub["id"], "valor_mensal": valor})
            inv = ""
            try:
                pays = asaas.list_payments(subscription_id=sub["id"]).get("data", [])
                inv = pays[0].get("invoiceUrl", "") if pays else ""
            except Exception:
                pass
            return {"success": True, "tipo": "assinatura", "id": sub["id"], "valor": valor,
                    "status": "assinatura ativa", "invoiceUrl": inv}
        due = (datetime.now() + timedelta(days=int(b.get("vencimento_dias", 3) or 3))).strftime("%Y-%m-%d")
        pay = asaas.create_payment(customer=cid, value=valor, due_date=due, billing_type=forma,
                description=desc, external_ref="provedor:" + pid)
        out = {"success": True, "tipo": "avulsa", "id": pay.get("id"), "valor": valor,
               "status": pay.get("status", ""), "invoiceUrl": pay.get("invoiceUrl", "")}
        if forma == "PIX":
            try:
                out["pix"] = asaas.pix_qrcode(pay.get("id")).get("payload", "")
            except Exception:
                pass
        return out
    except Exception as e:
        return JSONResponse({"error": "Asaas: " + str(getattr(e, "body", e))[:250]}, status_code=400)


@router.get("/api/comercial/prov/planos")
def prov_planos(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='PlanoProvedor'").fetchall()
    c.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") == pid:
            d["id"] = r["id"]; out.append(d)
    out.sort(key=lambda x: (x.get("tipo", ""), x.get("nome", "")))
    return out


@router.post("/api/comercial/prov/planos/salvar")
async def prov_planos_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    nome = (b.get("nome") or "").strip()
    if not nome:
        return JSONResponse({"error": "informe o nome do plano"}, status_code=400)
    try:
        valor = round(float(b.get("valor") or 0), 2)
    except (TypeError, ValueError):
        valor = 0.0
    try:
        cams = int(b.get("cameras") or 0)
    except (TypeError, ValueError):
        cams = 0
    try:
        meses = int(b.get("contrato_meses") or 36)
    except (TypeError, ValueError):
        meses = 36
    recursos = [str(x).strip() for x in (b.get("recursos") or []) if str(x).strip()]
    data = {"provedor_id": pid, "nome": nome, "tipo": (b.get("tipo") or "outro"),
            "valor": valor, "cameras": cams, "ia": bool(b.get("ia", False)),
            "descricao": (b.get("descricao") or "").strip(), "ativo": bool(b.get("ativo", True)),
            "contrato_meses": meses, "tipo_documento": (b.get("tipo_documento") or "ambos"),
            "recursos": recursos, "inclui_monitoramento": bool(b.get("inclui_monitoramento", False)),
            "em_destaque": bool(b.get("em_destaque", False))}
    eid = (b.get("id") or "").strip()
    if eid:
        ex = _get_ent("PlanoProvedor", eid)
        if not ex or ex.get("provedor_id") != pid:
            return JSONResponse({"error": "plano nao encontrado"}, status_code=403)
        data["criado"] = ex.get("criado")
        _update_ent("PlanoProvedor", eid, data)
    else:
        data["criado"] = _now_iso()
        eid = _create_ent("PlanoProvedor", data)
    return {"success": True, "id": eid}


@router.delete("/api/comercial/prov/planos/{eid}")
async def prov_planos_delete(eid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("PlanoProvedor", eid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "plano nao encontrado"}, status_code=403)
    c = _db(); c.execute("DELETE FROM entities WHERE entity='PlanoProvedor' AND id=?", (eid,)); c.commit(); c.close()
    return {"success": True}


@router.post("/api/comercial/prov/planos/modelos")
async def prov_planos_modelos(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    tem = set()
    for r in c.execute("SELECT data FROM entities WHERE entity='PlanoProvedor'").fetchall():
        d = json.loads(r["data"])
        if d.get("provedor_id") == pid:
            tem.add(d.get("tipo"))
    c.close()
    modelos = [("totem3", "Totem 3 cameras", 3, False), ("totem4", "Totem 4 cameras", 4, False),
               ("avulsa", "Camera Avulsa / IA", 1, True)]
    n = 0
    for tipo, nome, cams, ia in modelos:
        if tipo in tem:
            continue
        _create_ent("PlanoProvedor", {"provedor_id": pid, "nome": nome, "tipo": tipo, "valor": 0.0,
                                      "cameras": cams, "ia": ia, "descricao": "", "ativo": True, "criado": _now_iso()})
        n += 1
    return {"success": True, "criados": n}


# ---------- Alertas do provedor (feed escopado por provedor_id) ----------
@router.get("/api/comercial/prov/alertas")
def prov_alertas(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    qp = req.query_params
    try:
        limit = min(max(1, int(qp.get("limit", "200") or 200)), 500)
    except ValueError:
        limit = 200
    where = ["entity='Alerta'", "json_extract(data,'$.provedor_id')=?"]
    args = [pid]
    for campo, chave in (("tipo", "$.tipo"), ("status", "$.status"),
                         ("camera_id", "$.camera_id"), ("cliente_id", "$.cliente_id")):
        v = (qp.get(campo, "") or "").strip()
        if v:
            where.append("json_extract(data,'%s')=?" % chave); args.append(v)
    sql = "SELECT id, data, created_date FROM entities WHERE " + " AND ".join(where) + " ORDER BY created_date DESC LIMIT ?"
    args.append(limit)
    c = _db(); rows = c.execute(sql, args).fetchall(); c.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append({"id": r["id"], "criado": r["created_date"], "camera_id": d.get("camera_id", ""),
                    "camera_nome": d.get("camera_nome", ""), "cliente_nome": d.get("cliente_nome", ""),
                    "tipo": d.get("tipo", ""), "descricao": d.get("descricao", ""),
                    "confianca": d.get("confianca", 0), "imagem_url": d.get("imagem_url", ""),
                    "status": d.get("status", "novo"), "whatsapp_enviado": bool(d.get("whatsapp_enviado"))})
    return out


@router.get("/api/comercial/prov/alertas/resumo")
def prov_alertas_resumo(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    hoje = datetime.now().strftime("%Y-%m-%d")
    c = _db()
    base = "SELECT COUNT(*) FROM entities WHERE entity='Alerta' AND json_extract(data,'$.provedor_id')=?"
    total = c.execute(base, (pid,)).fetchone()[0]
    novos = c.execute(base + " AND json_extract(data,'$.status')='novo'", (pid,)).fetchone()[0]
    hoje_n = c.execute(base + " AND substr(created_date,1,10)=?", (pid, hoje)).fetchone()[0]
    c.close()
    return {"total": total, "novos": novos, "hoje": hoje_n}


@router.post("/api/comercial/prov/alertas/marcar-vistos")
async def prov_alertas_marcar_vistos(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='Alerta' AND json_extract(data,'$.provedor_id')=? "
                     "AND json_extract(data,'$.status')='novo'", (pid,)).fetchall()
    n = 0
    for r in rows:
        d = json.loads(r["data"]); d["status"] = "visualizado"
        c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Alerta' AND id=?",
                  (json.dumps(d), _now_iso(), r["id"])); n += 1
    c.commit(); c.close()
    return {"success": True, "marcados": n}


@router.post("/api/comercial/prov/alertas/{aid}/status")
async def prov_alerta_status(aid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    al = _get_ent("Alerta", aid)
    if not al or al.get("provedor_id") != pid:
        return JSONResponse({"error": "alerta nao encontrado ou sem acesso"}, status_code=403)
    b = await req.json()
    novo = (b.get("status") or "").strip()
    if novo not in ("novo", "visualizado", "resolvido"):
        return JSONResponse({"error": "status invalido"}, status_code=400)
    _update_ent("Alerta", aid, {"status": novo})
    return {"success": True, "status": novo}


@router.get("/api/comercial/prov/alerta/{aid}/img")
def prov_alerta_img(aid: str, req: Request):
    # imagem do alerta escopada pela ENTIDADE (provedor_id do alerta), aceita ?t= p/ tag <img>
    tok = (req.query_params.get("t", "") or (req.headers.get("authorization", "") or "").replace("Bearer ", "")).strip()
    u = None
    if tok:
        c = _db(); r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone(); c.close()
        u = dict(r) if (r and r["status"] == "ativo") else None
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    pid = (u.get("provedor_id") or "").strip()
    al = _get_ent("Alerta", aid)
    if not (al and pid and al.get("provedor_id") == pid):
        return JSONResponse({"error": "sem acesso"}, status_code=403)
    name = (al.get("imagem_url", "") or "").rsplit("/", 1)[-1]
    if not name or "/" in name or ".." in name:
        return JSONResponse({"error": "sem imagem"}, status_code=404)
    imgdir = os.path.join(HERE, "alertas_img")
    p = os.path.join(imgdir, name)
    if not os.path.exists(p):
        return JSONResponse({"error": "nao encontrada"}, status_code=404)
    try:
        w = int(req.query_params.get("w", "0") or 0)
    except ValueError:
        w = 0
    if 0 < w <= 800:
        thumb = os.path.join(imgdir, ".thumb%d_%s" % (w, name))
        if not os.path.exists(thumb):
            try:
                from PIL import Image
                im = Image.open(p); im.thumbnail((w, w * 2)); im.convert("RGB").save(thumb, "JPEG", quality=72, optimize=True)
            except Exception:
                thumb = p
        return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- Plantao (NumeroPlantao): numeros que recebem TODOS os alertas do provedor ----------
@router.get("/api/comercial/prov/plantao")
def prov_plantao_list(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='NumeroPlantao'").fetchall(); c.close()
    out = [dict(json.loads(r["data"]), id=r["id"]) for r in rows]
    return [x for x in out if x.get("provedor_id") == pid]


@router.post("/api/comercial/prov/plantao/salvar")
async def prov_plantao_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    tel = "".join(ch for ch in (b.get("telefone") or "") if ch.isdigit())
    data = {"nome": (b.get("nome") or "").strip(), "telefone": tel, "ativo": bool(b.get("ativo", True)),
            "notas": (b.get("notas") or "").strip(), "provedor_id": pid}
    if not data["nome"] or not data["telefone"]:
        return JSONResponse({"error": "informe nome e WhatsApp"}, status_code=400)
    nid = (b.get("id") or "").strip()
    if nid:
        ex = _get_ent("NumeroPlantao", nid)
        if not ex or ex.get("provedor_id") != pid:
            return JSONResponse({"error": "sem permissao"}, status_code=403)
        _update_ent("NumeroPlantao", nid, data)
    else:
        data["criado_em"] = _now_iso(); _create_ent("NumeroPlantao", data)
    return {"success": True}


@router.post("/api/comercial/prov/plantao/{nid}/toggle")
async def prov_plantao_toggle(nid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("NumeroPlantao", nid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    novo = not ex.get("ativo", True)
    _update_ent("NumeroPlantao", nid, {"ativo": novo})
    return {"success": True, "ativo": novo}


@router.delete("/api/comercial/prov/plantao/{nid}")
def prov_plantao_del(nid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("NumeroPlantao", nid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); c.execute("DELETE FROM entities WHERE entity='NumeroPlantao' AND id=?", (nid,)); c.commit(); c.close()
    return {"success": True}


# ---------- Preferencias de Alerta por cliente (PreferenciaAlerta) ----------
@router.get("/api/comercial/prov/preferencias")
def prov_pref_list(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    cl = [dict(json.loads(r["data"]), id=r["id"]) for r in c.execute("SELECT id, data FROM entities WHERE entity='Cliente'").fetchall()]
    prefs = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='PreferenciaAlerta'").fetchall()]
    c.close()
    cl = [x for x in cl if x.get("provedor_id") == pid]
    pmap = {}
    for p in prefs:
        if p.get("provedor_id") == pid and p.get("cliente_id"):
            pmap[p["cliente_id"]] = p
    return [{"cliente_id": x["id"], "cliente_nome": x.get("nome", ""), "telefone": x.get("telefone", ""),
             "pref": pmap.get(x["id"])} for x in cl]


@router.post("/api/comercial/prov/preferencias/salvar")
async def prov_pref_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cid = (b.get("cliente_id") or "").strip()
    cli = _get_ent("Cliente", cid)
    if not cli or cli.get("provedor_id") != pid:
        return JSONResponse({"error": "cliente nao encontrado ou sem acesso"}, status_code=403)
    data = {"provedor_id": pid, "cliente_id": cid, "cliente_nome": cli.get("nome", ""),
            "tipos_permitidos": b.get("tipos_permitidos") or [],
            "hora_inicio": (b.get("hora_inicio") or "").strip(), "hora_fim": (b.get("hora_fim") or "").strip(),
            "dias_semana": [int(d) for d in (b.get("dias_semana") or []) if str(d).isdigit() or isinstance(d, int)],
            "notificar_whatsapp": bool(b.get("notificar_whatsapp", True)), "ativo": bool(b.get("ativo", True))}
    c = _db()
    row = c.execute("SELECT id FROM entities WHERE entity='PreferenciaAlerta' AND json_extract(data,'$.cliente_id')=?", (cid,)).fetchone()
    c.close()
    if row:
        _update_ent("PreferenciaAlerta", row["id"], data)
    else:
        _create_ent("PreferenciaAlerta", data)
    return {"success": True}


@router.get("/api/comercial/geocode")
def geocode_cep(req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    import requests as _rq
    cep = "".join(ch for ch in (req.query_params.get("cep", "") or "") if ch.isdigit())
    if len(cep) != 8:
        return JSONResponse({"error": "CEP invalido (8 digitos)"}, status_code=400)
    out = {"cep": cep, "logradouro": "", "bairro": "", "cidade": "", "uf": "", "latitude": None, "longitude": None}
    try:
        vc = _rq.get("https://viacep.com.br/ws/%s/json/" % cep, timeout=8).json()
        if vc.get("erro"):
            return JSONResponse({"error": "CEP nao encontrado"}, status_code=404)
        out["logradouro"] = vc.get("logradouro", "") or ""
        out["bairro"] = vc.get("bairro", "") or ""
        out["cidade"] = vc.get("localidade", "") or ""
        out["uf"] = vc.get("uf", "") or ""
    except Exception as e:
        return JSONResponse({"error": "ViaCEP: " + str(e)[:80]}, status_code=502)
    hdr = {"User-Agent": "CorexiaGeocoder/1.0 (contato@grupocorexia.com.br)"}
    try:
        q = ", ".join([x for x in [out["logradouro"], out["bairro"], out["cidade"], out["uf"], "Brasil"] if x])
        gj = _rq.get("https://nominatim.openstreetmap.org/search",
                     params={"format": "json", "limit": 1, "q": q}, headers=hdr, timeout=10).json()
        if not gj:
            gj = _rq.get("https://nominatim.openstreetmap.org/search",
                         params={"format": "json", "limit": 1, "postalcode": cep, "country": "Brazil"},
                         headers=hdr, timeout=10).json()
        if gj:
            out["latitude"] = float(gj[0]["lat"]); out["longitude"] = float(gj[0]["lon"])
    except Exception:
        pass
    return out


@router.post("/api/comercial/prov/cameras/{cid}/local")
async def prov_camera_local(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if not _prov_camera_own(pid, cid):
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    b = await req.json()
    patch = {}
    for k in ("cep", "endereco", "bairro", "cidade", "uf"):
        if k in b:
            patch[k] = (b.get(k) or "").strip()
    if "endereco" in patch:
        patch["localizacao"] = patch["endereco"]
    for k in ("latitude", "longitude"):
        v = b.get(k)
        if v in (None, ""):
            patch[k] = None
        else:
            try:
                patch[k] = float(v)
            except (TypeError, ValueError):
                patch[k] = None
    _update_ent("Camera", cid, patch)
    return {"success": True}


# streaming: host do ingest RTMP e base do embed (Corexia = servidor de stream via MediaMTX)
STREAM_INGEST_HOST = os.getenv("STREAM_INGEST_HOST", "181.191.109.137")
STREAM_EMBED_BASE = os.getenv("STREAM_EMBED_BASE", "https://www.grupocorexia.com.br/cam")


@router.post("/api/comercial/prov/cameras/criar")
async def prov_camera_criar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    nome = (b.get("nome") or "").strip()
    if not nome:
        return JSONResponse({"error": "informe o nome da camera"}, status_code=400)
    key = secrets.token_hex(8)
    protocolo = (b.get("protocolo") or "rtmp").lower()
    publico = bool(b.get("publico", True))
    rtmp_ingest = "rtmp://%s:1935/cam/%s" % (STREAM_INGEST_HOST, key)
    embed = "%s/%s/" % (STREAM_EMBED_BASE, key) if publico else ""
    try:
        lat = float(b.get("latitude")) if b.get("latitude") not in (None, "") else None
        lng = float(b.get("longitude")) if b.get("longitude") not in (None, "") else None
    except (TypeError, ValueError):
        lat = lng = None
    prov = _get_ent("Provedor", pid) or {}
    if prov.get("tester"):
        _lim = int(prov.get("limite_cameras", 3) or 3)
        _cc = _db()
        _n = sum(1 for _r in _cc.execute("SELECT data FROM entities WHERE entity='Camera'").fetchall()
                 if json.loads(_r["data"]).get("provedor_id") == pid)
        _cc.close()
        if _n >= _lim:
            return JSONResponse({"error": "Provedor de teste: limite de %d cameras atingido (reative o painel p/ adicionar mais)." % _lim}, status_code=400)
    data = {"nome": nome, "provedor_id": pid, "provedor_nome": prov.get("nome", ""),
            "cep": (b.get("cep") or "").strip(), "endereco": (b.get("endereco") or "").strip(),
            "localizacao": (b.get("endereco") or "").strip(), "bairro": (b.get("bairro") or "").strip(),
            "cidade": (b.get("cidade") or "").strip(), "uf": (b.get("uf") or "").strip(),
            "latitude": lat, "longitude": lng,
            "usuario": (b.get("usuario") or "").strip(), "senha": (b.get("senha") or ""),
            "protocolo": protocolo, "grava_audio": bool(b.get("grava_audio", False)),
            "fuso": (b.get("fuso") or "America/Sao_Paulo"), "publico": publico,
            "stream_key": key, "rtmp_ingest": rtmp_ingest, "embed_url": embed,
            "status": "offline", "criado_em": _now_iso()}
    if protocolo == "rtmp":
        data["rtsp_url"] = "rtmp://127.0.0.1:1935/cam/%s" % key   # fonte que a IA/restream le (MediaMTX)
    else:
        data["rtsp_url"] = (b.get("rtsp_url") or "").strip()
    _create_ent("Camera", data)
    return {"success": True, "rtmp_ingest": rtmp_ingest, "embed_url": embed, "stream_key": key}


@router.post("/api/comercial/prov/cameras/{cid}/editar")
async def prov_camera_editar(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cam = _prov_camera_own(pid, cid)
    if not cam:
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    b = await req.json()
    key = cam.get("stream_key", "")
    nome = (b.get("nome") or "").strip() or cam.get("nome", "")
    protocolo = (b.get("protocolo") or cam.get("protocolo") or "rtmp").lower()
    publico = bool(b.get("publico", cam.get("publico", True)))
    patch = {"nome": nome, "usuario": (b.get("usuario") or "").strip(),
             "protocolo": protocolo, "grava_audio": bool(b.get("grava_audio", cam.get("grava_audio", False))),
             "fuso": (b.get("fuso") or cam.get("fuso") or "America/Sao_Paulo"), "publico": publico}
    sn = b.get("senha", "")
    if sn != "":
        patch["senha"] = sn   # senha em branco = mantem a atual
    for k in ("cep", "endereco", "bairro", "cidade", "uf"):
        if k in b:
            patch[k] = (b.get(k) or "").strip()
    if "endereco" in patch:
        patch["localizacao"] = patch["endereco"]
    for k in ("latitude", "longitude"):
        if k in b:
            v = b.get(k)
            try:
                patch[k] = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                patch[k] = None
    if key:
        patch["embed_url"] = "%s/%s/" % (STREAM_EMBED_BASE, key) if publico else ""
    if protocolo == "rtmp" and key:
        patch["rtsp_url"] = "rtmp://127.0.0.1:1935/cam/%s" % key
    elif protocolo == "rtsp":
        patch["rtsp_url"] = (b.get("rtsp_url") or cam.get("rtsp_url", "")).strip()
    _update_ent("Camera", cid, patch)
    return {"success": True}


@router.get("/api/comercial/prov/mosaicos")
def prov_mosaicos(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cliente_id = (req.query_params.get("cliente_id") or "").strip()
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Mosaico'").fetchall(); c.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") == pid and (not cliente_id or d.get("cliente_id") == cliente_id):
            d["id"] = r["id"]; out.append(d)
    out.sort(key=lambda x: (x.get("nome") or "").lower())
    return out


def _prov_cliente_cam_ids(pid, cliente_id):
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall(); c.close()
    out = set()
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") == pid and d.get("cliente_id") == cliente_id:
            out.add(r["id"])
    return out


@router.post("/api/comercial/prov/mosaicos/salvar")
async def prov_mosaico_salvar(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cliente_id = (b.get("cliente_id") or "").strip()
    cli = _get_ent("Cliente", cliente_id)
    if not cli or cli.get("provedor_id") != pid:
        return JSONResponse({"error": "cliente nao encontrado"}, status_code=404)
    nome = (b.get("nome") or "").strip()
    if not nome:
        return JSONResponse({"error": "informe o nome do mosaico"}, status_code=400)
    valid = _prov_cliente_cam_ids(pid, cliente_id)
    cams = [str(x) for x in (b.get("cameras") or []) if str(x) in valid][:4]
    data = {"nome": nome, "cliente_id": cliente_id, "cliente_nome": cli.get("nome", ""),
            "provedor_id": pid, "cameras": cams, "ativo": bool(b.get("ativo", True))}
    eid = (b.get("id") or "").strip()
    if eid:
        ex = _get_ent("Mosaico", eid)
        if not ex or ex.get("provedor_id") != pid:
            return JSONResponse({"error": "mosaico nao encontrado"}, status_code=404)
        _update_ent("Mosaico", eid, data)
    else:
        eid = _create_ent("Mosaico", data)
    return {"success": True, "id": eid, "cameras": len(cams)}


@router.post("/api/comercial/prov/mosaicos/{mid}/toggle")
async def prov_mosaico_toggle(mid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("Mosaico", mid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "mosaico nao encontrado"}, status_code=404)
    cur = ex.get("ativo", True) is not False
    _update_ent("Mosaico", mid, {"ativo": (not cur)})
    return {"success": True}


@router.delete("/api/comercial/prov/mosaicos/{mid}")
async def prov_mosaico_del(mid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("Mosaico", mid)
    if not ex or ex.get("provedor_id") != pid:
        return JSONResponse({"error": "mosaico nao encontrado"}, status_code=404)
    c = _db(); c.execute("DELETE FROM entities WHERE entity='Mosaico' AND id=?", (mid,)); c.commit(); c.close()
    return {"success": True}


@router.get("/api/comercial/prov/cameras-atrela")
def prov_cameras_atrela(req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall(); c.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") != pid:
            continue
        out.append({"id": r["id"], "nome": d.get("nome", "") or r["id"],
                    "cliente_id": d.get("cliente_id", "") or "", "cliente_nome": d.get("cliente_nome", "") or ""})
    out.sort(key=lambda x: (x["nome"] or "").lower())
    return out


@router.post("/api/comercial/prov/clientes/{cliente_id}/cameras")
async def prov_cliente_cameras_set(cliente_id: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cli = _get_ent("Cliente", cliente_id)
    if not cli or cli.get("provedor_id") != pid:
        return JSONResponse({"error": "cliente nao encontrado"}, status_code=404)
    b = await req.json()
    ids = set(str(x) for x in (b.get("camera_ids") or []))
    nome_cli = cli.get("nome", "")
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall()
    changed = 0
    for r in rows:
        d = json.loads(r["data"])
        if d.get("provedor_id") != pid:
            continue
        cur = d.get("cliente_id") or ""
        if r["id"] in ids and cur != cliente_id:
            d["cliente_id"] = cliente_id; d["cliente_nome"] = nome_cli
            c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Camera' AND id=?", (json.dumps(d), _now_iso(), r["id"])); changed += 1
        elif r["id"] not in ids and cur == cliente_id:
            d["cliente_id"] = ""; d["cliente_nome"] = ""
            c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Camera' AND id=?", (json.dumps(d), _now_iso(), r["id"])); changed += 1
    c.commit(); c.close()
    return {"success": True, "alteradas": changed}


# ---------- Exclusao de camera com aprovacao da Corexia ----------
def _notificar_provedor(prov, assunto, corpo):
    """Avisa o provedor por WhatsApp (Z-API Corexia) + e-mail (Gmail do backup). Best-effort."""
    tel = "".join(ch for ch in (prov.get("telefone", "") or "") if ch.isdigit())
    if tel and not tel.startswith("55"):
        tel = "55" + tel
    inst = os.getenv("ZAPI_INSTANCE_ID", ""); tok = os.getenv("ZAPI_TOKEN", ""); cli = os.getenv("ZAPI_CLIENT_TOKEN", "")
    if tel and inst and tok:
        try:
            import requests as _rq
            _rq.post("https://api.z-api.io/instances/%s/token/%s/send-text" % (inst, tok),
                     json={"phone": tel, "message": corpo},
                     headers={"Content-Type": "application/json", "Client-Token": cli}, timeout=20)
        except Exception as e:
            print("[notif-wa]", str(e)[:100])
    dest = (prov.get("email", "") or "").strip()
    if dest:
        try:
            mc = {}
            for ln in open("/home/tvlan/.corexia_backup.env"):
                s = ln.strip()
                if "=" in s and not s.startswith("#"):
                    k, v = s.split("=", 1); mc[k] = v.strip().strip('"').strip("'")
            import smtplib, ssl
            from email.message import EmailMessage
            m = EmailMessage(); m["From"] = mc.get("MAIL_USER", ""); m["To"] = dest; m["Subject"] = assunto; m.set_content(corpo)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=40) as sv:
                sv.login(mc["MAIL_USER"], mc["MAIL_PASS"]); sv.send_message(m)
        except Exception as e:
            print("[notif-mail]", str(e)[:100])


@router.post("/api/comercial/prov/cameras/{cid}/excluir-solicitar")
async def prov_excluir_solicitar(cid: str, req: Request):
    u, pid = _prov_req(req)
    if not pid:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cam = _prov_camera_own(pid, cid)
    if not cam:
        return JSONResponse({"error": "camera nao encontrada ou sem acesso"}, status_code=403)
    b = await req.json()
    motivo = (b.get("motivo") or "").strip()
    if not motivo:
        return JSONResponse({"error": "informe o motivo da exclusao"}, status_code=400)
    c = _db()
    ja = c.execute("SELECT id FROM entities WHERE entity='ExclusaoCamera' AND json_extract(data,'$.camera_id')=? "
                   "AND json_extract(data,'$.status')='pendente'", (cid,)).fetchone()
    c.close()
    if ja:
        return JSONResponse({"error": "ja existe um pedido pendente para esta camera"}, status_code=400)
    prov = _get_ent("Provedor", pid) or {}
    _create_ent("ExclusaoCamera", {"camera_id": cid, "camera_nome": cam.get("nome", ""), "provedor_id": pid,
                                   "provedor_nome": prov.get("nome", ""), "cliente_nome": cam.get("cliente_nome", ""),
                                   "motivo": motivo, "status": "pendente", "criado_em": _now_iso()})
    _update_ent("Camera", cid, {"exclusao_pendente": True})
    return {"success": True}


_HEALTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ia_health.json")


@router.get("/api/comercial/ia-health")
def ia_health(req: Request):
    u = _current_user(req)
    if not u or u.get("role") != "admin":
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    try:
        with open(_HEALTH_FILE) as f:
            d = json.load(f)
    except Exception:
        return {"overall": "critico", "updated": None, "stale": True, "checks": [],
                "problemas": [{"titulo": "Watchdog da IA sem dados",
                               "detalhe": "ia_health.json ausente (o watchdog ainda nao rodou?)", "sev": "critico"}]}
    try:
        idade = (datetime.now() - datetime.strptime(d.get("updated", ""), "%Y-%m-%dT%H:%M:%S")).total_seconds()
        if idade > 300:
            d["stale"] = True
            d["overall"] = "critico"
            d.setdefault("problemas", []).insert(0, {"titulo": "Watchdog da IA parado",
                "detalhe": "sem atualizacao ha %d min (verifique o cron do ia_watchdog)" % int(idade / 60),
                "sev": "critico"})
    except Exception:
        pass
    return d


@router.get("/api/comercial/exclusoes")
def exclusoes_list(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    rows = c.execute("SELECT id, data, created_date FROM entities WHERE entity='ExclusaoCamera' ORDER BY created_date DESC LIMIT 200").fetchall()
    c.close()
    out = [dict(json.loads(r["data"]), id=r["id"], criado=r["created_date"]) for r in rows]
    return {"pendentes": [x for x in out if x.get("status") == "pendente"],
            "recentes": [x for x in out if x.get("status") != "pendente"][:30]}


@router.post("/api/comercial/exclusoes/{eid}/decidir")
async def exclusoes_decidir(eid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    sol = _get_ent("ExclusaoCamera", eid)
    if not sol or sol.get("status") != "pendente":
        return JSONResponse({"error": "pedido nao encontrado ou ja decidido"}, status_code=404)
    b = await req.json()
    aprovar = bool(b.get("aprovar"))
    resposta = (b.get("resposta") or "").strip()
    prov = _get_ent("Provedor", sol.get("provedor_id", "")) or {}
    cam_nome = sol.get("camera_nome", "")
    if aprovar:
        cid = sol.get("camera_id", "")
        c = _db()
        c.execute("DELETE FROM entities WHERE entity='Camera' AND id=?", (cid,))
        for r in c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchall():
            c.execute("DELETE FROM entities WHERE entity='ConfigAnalitico' AND id=?", (r["id"],))
        c.commit(); c.close()
        _update_ent("ExclusaoCamera", eid, {"status": "aprovada", "decidido_em": _now_iso(), "decidido_por": u["email"], "resposta": resposta})
        _notificar_provedor(prov, "Corexia: exclusao de camera APROVADA",
                            "Sua solicitacao de exclusao da camera '%s' foi APROVADA. A camera foi excluida e o valor sera retirado da sua proxima cobranca." % cam_nome)
    else:
        _update_ent("Camera", sol.get("camera_id", ""), {"exclusao_pendente": False})
        _update_ent("ExclusaoCamera", eid, {"status": "negada", "decidido_em": _now_iso(), "decidido_por": u["email"], "resposta": resposta})
        _notificar_provedor(prov, "Corexia: exclusao de camera NAO aprovada",
                            "Sua solicitacao de exclusao da camera '%s' NAO foi aprovada.%s Se ainda desejar excluir, refaca o processo pelo painel." % (cam_nome, (" Motivo: " + resposta) if resposta else ""))
    return {"success": True}


@router.get("/comercial/{slug}")
def com_generico(slug: str, req: Request):
    if slug not in _LABEL_BY_SLUG:
        return JSONResponse({"error": "modulo desconhecido"}, status_code=404)
    titulo = next((t for s, l, t, _r in COMERCIAL_ITENS if s == slug), slug)
    body = ('<div class="center" style="padding:70px"><div style="font-size:18px;color:var(--ink);margin-bottom:8px">%s</div>'
            '<div>Em construcao - proxima entrega.</div></div>' % titulo)
    return _shell(slug, titulo, body)


@router.get("/api/comercial/ping")
def com_ping(req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"ok": False, "error": "nao autenticado"}, status_code=401)
    return {"ok": True, "user": {"email": u["email"], "role": u["role"],
            "provedor_id": u["provedor_id"] or "", "cliente_id": u["cliente_id"] or ""},
            "asaas_live": ASAAS_LIVE}


# ---------- helpers de entidade (mesma tabela do server.py) ----------
def _get_ent(entity, eid):
    c = _db(); r = c.execute("SELECT data FROM entities WHERE entity=? AND id=?", (entity, eid)).fetchone(); c.close()
    return json.loads(r["data"]) if r else None


def _update_ent(entity, eid, patch):
    c = _db(); r = c.execute("SELECT data FROM entities WHERE entity=? AND id=?", (entity, eid)).fetchone()
    if not r:
        c.close(); return None
    d = json.loads(r["data"]); d.update(patch)
    c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity=? AND id=?", (json.dumps(d), _now_iso(), entity, eid))
    c.commit(); c.close(); return d


def _cliente_set_acesso(cid, ativar):
    """Gate REAL de acesso: propaga o status do Cliente para os LOGINS (users: master + sub-usuarios,
    todos role='cliente' com o mesmo cliente_id) e espelha nas entidades SubUser. Equivale ao
    'desbloquear/bloquear no Corexia' + cascata de sub-usuarios do Viggia. Ao suspender, derruba as
    sessoes ativas para efeito imediato."""
    st = "ativo" if ativar else "bloqueado"
    c = _db()
    c.execute("UPDATE users SET status=? WHERE cliente_id=? AND role='cliente'", (st, cid))
    if not ativar:
        c.execute("DELETE FROM sessions WHERE user_id IN "
                  "(SELECT id FROM users WHERE cliente_id=? AND role='cliente')", (cid,))
    for r in c.execute("SELECT id, data FROM entities WHERE entity='SubUser'").fetchall():
        d = json.loads(r["data"])
        if d.get("client_id") == cid:
            d["status"] = st
            c.execute("UPDATE entities SET data=? WHERE entity='SubUser' AND id=?", (json.dumps(d), r["id"]))
    c.commit(); c.close()


def _create_ent(entity, data):
    eid = secrets.token_hex(12); now = _now_iso()
    c = _db(); c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                         (entity, eid, json.dumps(data), now, now)); c.commit(); c.close()
    return eid


def _pode_mexer(u, p):
    """admin mexe em tudo; provedor so nas propostas dele."""
    return u["role"] == "admin" or (p.get("provedor_id") and p.get("provedor_id") == u["provedor_id"])


# ==================== CHAMADOS / ATENDIMENTO ====================
# Nivel B: PROVEDOR -> ADMIN COREXIA. Provedor abre chamado; plantao Corexia e avisado
# por WhatsApp (Z-API da Corexia); admin responde/conclui/reabre e o provedor e avisado.
_CHAMADO_TIPOS = ("suporte", "financeiro", "atendimento")


def _chamado_hora():
    try:
        return datetime.now().strftime("%d/%m/%Y %H:%M")
    except Exception:
        return _now_iso()


def _plantao_prov_nums(provedor_id):
    """Telefones ativos do plantao do PROVEDOR (equipe de monitoramento dele)."""
    if not provedor_id:
        return []
    c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='NumeroPlantao'").fetchall(); c.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        if d.get("provedor_id") == provedor_id and d.get("ativo", True) and (d.get("telefone") or "").strip():
            out.append(d["telefone"].strip())
    return out


def _prov_tem_zapi(provedor_id):
    """True se o provedor ativou a Z-API DELE (sem cair no fallback da Corexia)."""
    cr = _cred(provedor_id) if provedor_id else {}
    return bool(cr.get("zapi_ativa") and cr.get("zapi_instance_id") and cr.get("zapi_token"))


def _plantao_corexia_nums():
    """Telefones ativos do plantao Corexia (recebem os chamados dos provedores)."""
    c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='NumeroPlantao'").fetchall(); c.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        if d.get("provedor_id") == "__corexia__" and d.get("ativo", True) and (d.get("telefone") or "").strip():
            out.append(d["telefone"].strip())
    return out


# --- plantao Corexia: admin gerencia quem recebe os chamados dos provedores ---
@router.get("/api/comercial/plantao-corexia")
def plantao_corexia_list(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db(); rows = c.execute("SELECT id, data FROM entities WHERE entity='NumeroPlantao'").fetchall(); c.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except Exception:
            continue
        if d.get("provedor_id") == "__corexia__":
            d["id"] = r["id"]; out.append(d)
    out.sort(key=lambda x: x.get("nome", ""))
    return out


@router.post("/api/comercial/plantao-corexia/salvar")
async def plantao_corexia_salvar(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    nome = (b.get("nome") or "").strip(); tel = (b.get("telefone") or "").strip()
    if not nome or not tel:
        return JSONResponse({"error": "informe nome e telefone"}, status_code=400)
    data = {"provedor_id": "__corexia__", "nome": nome, "telefone": tel,
            "ativo": bool(b.get("ativo", True)), "notas": (b.get("notas") or "").strip()}
    eid = (b.get("id") or "").strip()
    if eid:
        ex = _get_ent("NumeroPlantao", eid)
        if not ex or ex.get("provedor_id") != "__corexia__":
            return JSONResponse({"error": "registro invalido"}, status_code=404)
        _update_ent("NumeroPlantao", eid, data)
    else:
        eid = _create_ent("NumeroPlantao", data)
    return {"success": True, "id": eid}


@router.delete("/api/comercial/plantao-corexia/{eid}")
async def plantao_corexia_delete(eid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    ex = _get_ent("NumeroPlantao", eid)
    if not ex or ex.get("provedor_id") != "__corexia__":
        return JSONResponse({"error": "registro invalido"}, status_code=404)
    c = _db(); c.execute("DELETE FROM entities WHERE entity='NumeroPlantao' AND id=?", (eid,)); c.commit(); c.close()
    return {"success": True}


# ---- credenciais do provedor (Asaas/Z-API): entidade ProvedorCred, INVISIVEL no CRUD generico ----
def _cred(provedor_id):
    return _get_ent("ProvedorCred", provedor_id) or {}


def _upsert_cred(provedor_id, patch):
    if _get_ent("ProvedorCred", provedor_id) is None:
        d = dict(patch); d["provedor_id"] = provedor_id; now = _now_iso()
        c = _db(); c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                             ("ProvedorCred", provedor_id, json.dumps(d), now, now)); c.commit(); c.close()
    else:
        _update_ent("ProvedorCred", provedor_id, patch)


def _zapi_do_provedor(provedor_id):
    """Z-API do provedor SE ele ativou a dele; senao a PADRAO da Corexia (bot oficial)."""
    cr = _cred(provedor_id) if provedor_id else {}
    if cr.get("zapi_ativa") and cr.get("zapi_instance_id") and cr.get("zapi_token"):
        return cr["zapi_instance_id"], cr["zapi_token"], cr.get("zapi_client_token", "")
    return ZAPI_INSTANCE, ZAPI_TOKEN, ZAPI_CLIENT


# ---------- fluxo de assinatura (2FA WhatsApp -> Asaas -> Cliente/Provedor) ----------
@router.post("/api/comercial/propostas/{pid}/enviar-codigo")
async def prop_enviar_codigo(pid: str, req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    p = _get_ent("Proposta", pid)
    if not p:
        return JSONResponse({"error": "proposta nao encontrada"}, status_code=404)
    if not _pode_mexer(u, p):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    whats = p.get("whatsapp") or p.get("cliente_telefone") or ""
    if not whats:
        return JSONResponse({"error": "proposta sem WhatsApp do cliente"}, status_code=400)
    code = "%06d" % secrets.randbelow(1000000)
    _update_ent("Proposta", pid, {"_sig_code": code, "_sig_exp": time.time() + 600, "status": "enviada"})
    txt = ("Corexia: seu codigo para ASSINAR a proposta e %s (valido por 10 min). "
           "Se voce nao solicitou, ignore." % code)
    _zi, _zt, _zc = _zapi_do_provedor(p.get("provedor_id", ""))   # z-api do provedor ou a padrao
    ok, info = _zapi_send(whats, txt, _zi, _zt, _zc)
    return {"success": bool(ok), "enviado": bool(ok), "info": info}


@router.post("/api/comercial/propostas/{pid}/assinar")
async def prop_assinar(pid: str, req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    b = await req.json()
    code = str(b.get("codigo", "")).strip()
    p = _get_ent("Proposta", pid)
    if not p:
        return JSONResponse({"error": "proposta nao encontrada"}, status_code=404)
    if not _pode_mexer(u, p):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if not p.get("_sig_code") or time.time() > float(p.get("_sig_exp", 0)):
        return JSONResponse({"error": "codigo expirado — reenvie"}, status_code=400)
    if code != p.get("_sig_code"):
        return JSONResponse({"error": "codigo invalido"}, status_code=400)

    nivel1 = (u["role"] == "admin")            # admin -> Corexia cobra Provedor (Nivel 1)
    alvo = "Provedor" if nivel1 else "Cliente"  # provedor -> cobra Cliente final (Nivel 2)
    api_key = None
    prov = None
    if not nivel1:
        prov = _get_ent("Provedor", u["provedor_id"]) or {}
        api_key = prov.get("asaas_api_key") or None

    cid = sid = None; modo = "teste"
    if ASAAS_LIVE:
        if asaas is None:
            return JSONResponse({"error": "modulo asaas indisponivel"}, status_code=500)
        if not nivel1 and not api_key and not (prov or {}).get("asaas_conta_grupo"):
            return JSONResponse({"error": "provedor sem asaas_api_key — configure antes de assinar"}, status_code=400)
        try:
            cust = asaas.create_customer(name=p.get("cliente_nome", ""), cpf_cnpj=p.get("document_number", ""),
                    email=p.get("email", ""), phone=p.get("whatsapp", ""),
                    external_ref="proposta:" + pid, api_key=api_key)
            cid = cust["id"]
            due = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
            maxp = int(p.get("contrato_meses") or (36 if p.get("document_type") == "cnpj" else 12))
            sub = asaas.create_subscription(customer=cid, value=p.get("valor_mensal", 0) or 0, next_due_date=due,
                    max_payments=maxp, description="Corexia - " + (p.get("plano_nome", "") or ""),
                    external_ref="proposta:" + pid, api_key=api_key)
            sid = sub["id"]; modo = "real"
        except Exception as e:
            body = getattr(e, "body", str(e))
            return JSONResponse({"error": "Asaas: " + str(body)[:200]}, status_code=400)
    else:
        cid = "TESTE_cus_" + secrets.token_hex(4)   # modo teste: NAO cria nada no Asaas
        sid = "TESTE_sub_" + secrets.token_hex(4)

    # cria a entidade alvo (Cliente/Provedor) com os dados + asaas_*
    alvo_data = {
        "nome": p.get("cliente_nome", ""), "email": p.get("email", ""), "telefone": p.get("whatsapp", ""),
        "document_type": p.get("document_type", ""), "document_number": p.get("document_number", ""),
        "endereco": {"cep": p.get("cep", ""), "cidade": p.get("cidade", ""), "logradouro": p.get("logradouro", ""),
                     "numero": p.get("numero", ""), "bairro": p.get("bairro", ""), "uf": p.get("uf", "")},
        "plano_id": p.get("plano_id", ""), "plano_nome": p.get("plano_nome", ""), "valor_mensal": p.get("valor_mensal", 0),
        "asaas_customer_id": cid, "asaas_subscription_id": sid, "status": "ativo",
        "origem_proposta": pid, "criado_em": _now_iso(),
    }
    if not nivel1:
        alvo_data["provedor_id"] = u["provedor_id"]
        alvo_data["provedor_nome"] = (prov or {}).get("nome", "")
    alvo_id = _create_ent(alvo, alvo_data)

    import hashlib as _hl
    _dt = datetime.now()
    _ass_utc = _dt.strftime("%Y-%m-%dT%H:%M:%S")
    _ass_loc = (_dt + timedelta(hours=-3)).strftime("%d/%m/%Y às %H:%M:%S") + " (horário de Brasília)"
    _ip = (req.client.host if req.client else "") or ""
    _sig_id = "CX-" + secrets.token_hex(6).upper()
    _code_hash = _hl.sha256(code.encode("utf-8")).hexdigest()
    _canon = "|".join([pid, p.get("cliente_nome", ""), str(p.get("document_number", "")),
                       p.get("plano_nome", ""), str(p.get("valor_mensal", "")),
                       p.get("cep", ""), p.get("cidade", ""), p.get("uf", ""), "CONTRATO_COREXIA_v1"])
    _doc_hash = _hl.sha256(_canon.encode("utf-8")).hexdigest()
    _trail = {"assinatura_id": _sig_id, "assinado_em": _ass_utc, "assinado_em_local": _ass_loc,
              "assinado_ip": _ip, "sig_code_hash": _code_hash, "doc_hash": _doc_hash,
              "assinado_por": p.get("whatsapp", ""), "assinado_por_nome": p.get("cliente_nome", ""),
              "assinado_por_doc": p.get("document_number", "")}
    patch = {"status": "fechada", "assinada_em": _ass_utc, "signature_modo": modo,
             "asaas_customer_id": cid, "asaas_subscription_id": sid, "_sig_code": None}
    patch.update(_trail)
    patch["provedor_novo_id" if nivel1 else "cliente_id"] = alvo_id
    _update_ent("Proposta", pid, patch)

    # amarra o contrato: se houver contrato(s) gerado(s) desta proposta, marca como assinado
    try:
        _c = _db()
        _rows = _c.execute("SELECT id, data FROM entities WHERE entity='Contrato'").fetchall()
        _c.close()
        for _r in _rows:
            if json.loads(_r["data"]).get("proposta_id") == pid:
                _update_ent("Contrato", _r["id"], dict(_trail, status="assinado", signature_modo=modo))
    except Exception:
        pass

    if p.get("whatsapp"):
        _zi, _zt, _zc = _zapi_do_provedor(p.get("provedor_id", ""))
        _zapi_send(p.get("whatsapp"), "Corexia: contrato ASSINADO com sucesso! Bem-vindo(a). "
                   "Sua assinatura mensal foi ativada" + ("." if modo == "real" else " (modo teste)."),
                   _zi, _zt, _zc)
    return {"success": True, "modo": modo, "alvo": alvo, "alvo_id": alvo_id,
            "asaas_customer_id": cid, "asaas_subscription_id": sid}


@router.post("/api/comercial/asaas/webhook")
async def asaas_webhook(req: Request):
    """Espelha cobrancas do Asaas em entidades Fatura. Sem auth de sessao (Asaas chama);
    valida por token opcional (COMERCIAL_ASAAS_WEBHOOK_TOKEN)."""
    tok_exig = os.getenv("COMERCIAL_ASAAS_WEBHOOK_TOKEN", "")
    if tok_exig and req.headers.get("asaas-access-token", "") != tok_exig:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        b = await req.json()
    except Exception:
        return JSONResponse({"error": "payload invalido"}, status_code=400)
    ev = b.get("event", ""); pay = b.get("payment", {}) or {}
    cust = pay.get("customer", ""); sub = pay.get("subscription", "")
    st_map = {"PAYMENT_RECEIVED": "paga", "PAYMENT_CONFIRMED": "paga",
              "PAYMENT_OVERDUE": "vencida", "PAYMENT_CREATED": "pendente", "PAYMENT_UPDATED": "pendente"}
    status = st_map.get(ev, "pendente")
    # acha o dono (Cliente/Provedor) por asaas_customer_id
    dono_id = dono_ent = ""
    for ent in ("Cliente", "Provedor"):
        c = _db(); rows = c.execute("SELECT id,data FROM entities WHERE entity=?", (ent,)).fetchall(); c.close()
        for r in rows:
            d = json.loads(r["data"])
            if d.get("asaas_customer_id") == cust and cust:
                dono_id, dono_ent = r["id"], ent; break
        if dono_id:
            break
    if not dono_id:
        return {"ignored": True}   # cliente nao e da Corexia (conta Asaas compartilhada) -> ignora
    # cria/atualiza a Fatura (por asaas payment id)
    pay_id = pay.get("id", "")
    c = _db(); ex = c.execute("SELECT id FROM entities WHERE entity='Fatura' AND json_extract(data,'$.asaas_payment_id')=?",
                              (pay_id,)).fetchone(); c.close()
    fdata = {"asaas_payment_id": pay_id, "asaas_customer_id": cust, "asaas_subscription_id": sub,
             "dono_id": dono_id, "dono_entidade": dono_ent, "valor": pay.get("value", 0),
             "vencimento": pay.get("dueDate", ""), "status": status, "evento": ev,
             "invoice_url": pay.get("invoiceUrl", "") or pay.get("bankSlipUrl", ""), "link_boleto": pay.get("invoiceUrl", "") or pay.get("bankSlipUrl", ""),
             "pago_em": pay.get("paymentDate", ""), "atualizado_em": _now_iso()}
    if dono_ent == "Cliente":
        cli = _get_ent("Cliente", dono_id) or {}
        fdata["cliente_id"] = dono_id; fdata["provedor_id"] = cli.get("provedor_id", "")
    elif dono_ent == "Provedor":
        fdata["provedor_id"] = dono_id
    if ex:
        _update_ent("Fatura", ex["id"], fdata)
    else:
        _create_ent("Fatura", fdata)
    print("[comercial] webhook asaas:", ev, "cust=", cust, "-> Fatura", status)
    return {"received": True}


# ---------- credenciais do Provedor/Revenda (Asaas + Z-API) ----------
@router.get("/api/comercial/provedores/{pid}/cred-status")
def prov_cred_status(pid: str, req: Request):
    u = _current_user(req)
    if not (u and (u["role"] == "admin" or u.get("provedor_id") == pid)):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cr = _cred(pid)
    def m(v): return ("****" + v[-4:]) if v else ""
    return {"asaas_configurado": bool(cr.get("asaas_api_key")), "asaas_mask": m(cr.get("asaas_api_key", "")),
            "zapi_ativa": bool(cr.get("zapi_ativa")), "zapi_configurado": bool(cr.get("zapi_token")),
            "zapi_instance_id": cr.get("zapi_instance_id", ""), "zapi_mask": m(cr.get("zapi_token", ""))}


@router.post("/api/comercial/provedores/{pid}/asaas")
async def prov_set_asaas(pid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "so a Corexia cadastra credencial (por ora)"}, status_code=403)
    b = await req.json(); key = (b.get("asaas_api_key") or "").strip()
    if not key:
        return JSONResponse({"error": "chave vazia"}, status_code=400)
    conta = ""
    if asaas is not None:
        try:
            acc = asaas.my_account(api_key=key)   # valida READ-ONLY
            conta = acc.get("name") or acc.get("companyName") or ""
        except Exception as e:
            return JSONResponse({"error": "chave Asaas invalida: " + str(getattr(e, "body", e))[:150]}, status_code=400)
    _upsert_cred(pid, {"asaas_api_key": key})
    return {"success": True, "conta": conta}


@router.post("/api/comercial/provedores/{pid}/zapi")
async def prov_set_zapi(pid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "so a Corexia cadastra credencial (por ora)"}, status_code=403)
    b = await req.json()
    patch = {"zapi_ativa": bool(b.get("zapi_ativa")),
             "zapi_instance_id": (b.get("zapi_instance_id") or "").strip(),
             "zapi_token": (b.get("zapi_token") or "").strip(),
             "zapi_client_token": (b.get("zapi_client_token") or "").strip()}
    _upsert_cred(pid, patch)
    teste = None
    num = (b.get("testar_numero") or "").strip()
    if num and patch["zapi_instance_id"] and patch["zapi_token"]:
        ok, _ = _zapi_send(num, "Corexia: teste da Z-API do provedor - conectada!",
                           patch["zapi_instance_id"], patch["zapi_token"], patch["zapi_client_token"])
        teste = bool(ok)
    return {"success": True, "teste_enviado": teste}


# ---------- Faturas (espelho do Asaas da Corexia; sync = READ-ONLY) ----------
_ASAAS_ST = {"RECEIVED": "paga", "CONFIRMED": "paga", "RECEIVED_IN_CASH": "paga",
             "OVERDUE": "vencida", "PENDING": "pendente", "AWAITING_RISK_ANALYSIS": "pendente",
             "REFUNDED": "cancelada", "DELETED": "cancelada", "CHARGEBACK_REQUESTED": "vencida"}


def _upsert_fatura(p, cliente_nome, provedor_id=None):
    pay_id = p.get("id", "")
    due = p.get("dueDate", "") or ""
    fdata = {"asaas_payment_id": pay_id, "asaas_customer_id": p.get("customer", ""),
             "asaas_subscription_id": p.get("subscription", ""), "cliente_nome": cliente_nome,
             "numero": str(p.get("invoiceNumber") or p.get("nossoNumero") or pay_id),
             "valor": p.get("value", 0), "vencimento": due, "reference_month": due[:7],
             "status": _ASAAS_ST.get(p.get("status", ""), "pendente"),
             "invoice_url": p.get("invoiceUrl", "") or p.get("bankSlipUrl", ""), "link_boleto": p.get("invoiceUrl", "") or p.get("bankSlipUrl", ""),
             "pago_em": p.get("paymentDate", "") or "", "fonte": "asaas_sync", "atualizado_em": _now_iso()}
    if provedor_id:
        fdata["provedor_id"] = provedor_id
    _cust = p.get("customer", "")
    if _cust:
        _cc = _db()
        _rr = _cc.execute("SELECT id, data FROM entities WHERE entity='Cliente' AND json_extract(data,'$.asaas_customer_id')=?", (_cust,)).fetchone()
        _rp = None if _rr else _cc.execute("SELECT id, data FROM entities WHERE entity='Provedor' AND json_extract(data,'$.asaas_customer_id')=?", (_cust,)).fetchone()
        _cc.close()
        if _rr:
            _cli = json.loads(_rr["data"])
            fdata["cliente_id"] = _rr["id"]
            if not fdata.get("provedor_id"):
                fdata["provedor_id"] = _cli.get("provedor_id", "")
            if not fdata.get("cliente_nome"):
                fdata["cliente_nome"] = _cli.get("nome", "")
        elif _rp:
            if not fdata.get("provedor_id"):
                fdata["provedor_id"] = _rp["id"]
            if not fdata.get("cliente_nome"):
                fdata["cliente_nome"] = json.loads(_rp["data"]).get("nome", "")
    c = _db(); ex = c.execute("SELECT id FROM entities WHERE entity='Fatura' AND json_extract(data,'$.asaas_payment_id')=?", (pay_id,)).fetchone(); c.close()
    if ex:
        _update_ent("Fatura", ex["id"], fdata)
    else:
        _create_ent("Fatura", fdata)


@router.post("/api/comercial/faturas/sync")
async def faturas_sync(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "so a Corexia sincroniza as faturas do Asaas"}, status_code=403)
    if asaas is None:
        return JSONResponse({"error": "modulo asaas indisponivel"}, status_code=500)
    corte = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    # SO clientes DA COREXIA (a conta Asaas e compartilhada com Viggia + outros produtos)
    corexia_custs = set()
    _cx = _db()
    for _ent in ("Cliente", "Provedor"):
        for _r in _cx.execute("SELECT data FROM entities WHERE entity=?", (_ent,)).fetchall():
            _cid = json.loads(_r["data"]).get("asaas_customer_id", "")
            if _cid and not str(_cid).startswith("TESTE_"):
                corexia_custs.add(_cid)
    _cx.close()
    try:
        names = {}; off = 0
        while off < 3000:
            pg = asaas.list_customers_page(offset=off, limit=100)
            for c in pg.get("data", []):
                names[c["id"]] = c.get("name") or c.get("company") or ""
            if not pg.get("hasMore"):
                break
            off += 100
        n = 0; off = 0
        while off < 3000:
            pg = asaas.list_payments_page(offset=off, limit=100, extra={"dueDate[ge]": corte})
            for p in pg.get("data", []):
                if p.get("customer", "") not in corexia_custs:
                    continue   # ignora faturas de outros produtos da conta
                _upsert_fatura(p, names.get(p.get("customer", ""), ""))
                n += 1
            if not pg.get("hasMore"):
                break
            off += 100
    except Exception as e:
        return JSONResponse({"error": "Asaas: " + str(getattr(e, "body", e))[:200]}, status_code=400)
    return {"success": True, "sincronizadas": n}


@router.post("/api/comercial/faturas/{fid}/marcar-paga")
async def fatura_marcar_paga(fid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] in ("admin", "provedor")):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    f = _get_ent("Fatura", fid)
    if not f:
        return JSONResponse({"error": "fatura nao encontrada"}, status_code=404)
    if u["role"] == "provedor" and f.get("provedor_id") != u["provedor_id"]:
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    _update_ent("Fatura", fid, {"status": "paga", "pago_em": _now_iso(), "baixa_manual": True})
    return {"success": True}


# ---------- Clientes (admin): provedores/revendas que assinaram o contrato de revenda ----------
@router.post("/api/comercial/clientes/{pid}/bloquear")
async def cliente_bloquear(pid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if not _get_ent("Provedor", pid):
        return JSONResponse({"error": "cliente (provedor) nao encontrado"}, status_code=404)
    _update_ent("Provedor", pid, {"status": "bloqueado", "bloqueado_em": _now_iso()})
    return {"success": True, "status": "bloqueado"}


@router.post("/api/comercial/clientes/{pid}/desbloquear")
async def cliente_desbloquear(pid: str, req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if not _get_ent("Provedor", pid):
        return JSONResponse({"error": "cliente (provedor) nao encontrado"}, status_code=404)
    _update_ent("Provedor", pid, {"status": "ativo", "bloqueado_em": None, "desbloqueado_em": _now_iso()})
    return {"success": True, "status": "ativo"}


@router.post("/api/comercial/clientes/{pid}/sincronizar")
async def cliente_sincronizar(pid: str, req: Request):
    """Sincroniza as faturas do Asaas deste cliente (provedor, Nivel 1 -> chave da Corexia).
    Se ainda nao existir assinatura real, CRIA a assinatura recorrente (maxPayments = tempo de
    contrato do plano) e entao puxa as faturas. SEGURANCA $$: so cria/cobra quando
    COMERCIAL_ASAAS_LIVE=1; em modo teste apenas informa o que seria feito, sem tocar no Asaas."""
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    prov = _get_ent("Provedor", pid)
    if not prov:
        return JSONResponse({"error": "cliente (provedor) nao encontrado"}, status_code=404)

    cid = prov.get("asaas_customer_id") or ""
    sid = prov.get("asaas_subscription_id") or ""
    tem_sub = bool(sid) and not sid.startswith("TESTE_")
    tem_cus = bool(cid) and not cid.startswith("TESTE_")

    # tempo de contrato -> maxPayments (do proposta de origem; senao default por tipo de doc)
    meses = int(prov.get("contrato_meses") or 0)
    if not meses and prov.get("origem_proposta"):
        meses = int((_get_ent("Proposta", prov["origem_proposta"]) or {}).get("contrato_meses") or 0)
    if not meses:
        meses = 36 if (prov.get("document_type") == "cnpj") else 12
    valor = float(prov.get("valor_mensal", 0) or 0)

    if not ASAAS_LIVE:
        return {"success": True, "modo": "teste", "assinatura_criada": False, "sincronizadas": 0,
                "info": ("Modo teste: nada foi criado/cobrado no Asaas. Ao ativar (COMERCIAL_ASAAS_LIVE=1), "
                         "sera criada assinatura de R$ %.2f/mes por %d meses e as faturas serao sincronizadas." % (valor, meses))}

    if asaas is None:
        return JSONResponse({"error": "modulo asaas indisponivel"}, status_code=500)
    criada = False
    try:
        if not tem_cus:
            cust = asaas.create_customer(name=prov.get("nome", ""), cpf_cnpj=prov.get("document_number", ""),
                    email=prov.get("email", ""), phone=prov.get("telefone", ""), external_ref="provedor:" + pid)
            cid = cust["id"]; tem_cus = True
        if not tem_sub:
            due = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
            sub = asaas.create_subscription(customer=cid, value=valor, next_due_date=due, max_payments=meses,
                    description="Corexia Revenda - " + (prov.get("plano_nome", "") or ""), external_ref="provedor:" + pid)
            sid = sub["id"]; criada = True
        _update_ent("Provedor", pid, {"asaas_customer_id": cid, "asaas_subscription_id": sid})
        n = 0
        for p in asaas.list_payments(customer_id=cid).get("data", []):
            _upsert_fatura(p, prov.get("nome", "")); n += 1
    except Exception as e:
        return JSONResponse({"error": "Asaas: " + str(getattr(e, "body", e))[:200]}, status_code=400)
    return {"success": True, "modo": "real", "assinatura_criada": criada, "sincronizadas": n}


# ---------- Analiticos por Camera (config que o detector le: entidade ConfigAnalitico) ----------
@router.get("/api/comercial/analiticos/cameras")
def analiticos_cameras(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    c = _db()
    cams = c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall()
    cfgs_rows = c.execute("SELECT id, data FROM entities WHERE entity='ConfigAnalitico'").fetchall()
    c.close()
    cfgs = {}
    for r in cfgs_rows:
        d = json.loads(r["data"])
        if d.get("camera_id"):
            cfgs[d["camera_id"]] = {"cfg_id": r["id"], "ativo": d.get("ativo", True),
                                    "horarios": d.get("horarios", []), "analiticos_padrao": d.get("analiticos_padrao", []),
                                    "zonas_intrusao": d.get("zonas_intrusao", [])}
    out = []
    for r in cams:
        o = json.loads(r["data"])
        out.append({"id": r["id"], "nome": o.get("nome", ""), "cliente_nome": o.get("cliente_nome", ""), "embed_url": o.get("embed_url", ""), "stream_url": (o.get("rtsp_url") or o.get("stream_url") or ""), "ia_placa": bool(o.get("ia_placa")),
                    "config": cfgs.get(r["id"])})
    out.sort(key=lambda x: (x["nome"] or "").lower())
    return out


_camstatus_cache = {"t": 0.0, "data": {}}
@router.get("/api/comercial/analiticos/status")
def analiticos_status(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    if _camstatus_cache["data"] and time.time() - _camstatus_cache["t"] < 25:
        return _camstatus_cache["data"]
    c = _db()
    cams = []
    for r in c.execute("SELECT id, data FROM entities WHERE entity='Camera'").fetchall():
        o = json.loads(r["data"]); uu = (o.get("rtsp_url") or o.get("stream_url") or ""); eb = "".join(_c for _c in (o.get("embed_url") or "") if ord(_c) >= 32).strip()
        if ".m3u8" in uu or eb:
            cams.append((r["id"], uu, eb))
    c.close()
    import concurrent.futures, urllib.request, re as _re2
    _LR = _re2.compile(r"let live = '([^']+)'")
    def _get(u):
        _le = None
        for _a in range(2):
            try:
                rq = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://analitico.grupocorexia.com.br/"})
                return urllib.request.urlopen(rq, timeout=7).read().decode("utf-8", "replace")
            except Exception as _e:
                _le = _e; time.sleep(0.3)
        raise _le
    def _chk(item):
        cid, url, eb = item
        try:
            u2 = url if ".m3u8" in url else ""
            if not u2 and eb:
                m = _LR.search(_get(eb))
                if m: u2 = m.group(1)
            if not u2:
                return cid, False
            return cid, (".ts" in _get(u2))
        except Exception:
            return cid, False
    out = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=14) as ex:
            for cid, ok in ex.map(_chk, cams):
                out[cid] = ok
    except Exception:
        pass
    _camstatus_cache.update(t=time.time(), data=out)
    return out


@router.post("/api/comercial/analiticos/salvar")
async def analiticos_salvar(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cid = (b.get("camera_id") or "").strip()
    if not cid:
        return JSONResponse({"error": "camera_id obrigatorio"}, status_code=400)
    data = {"camera_id": cid, "camera_nome": b.get("camera_nome", ""), "ativo": bool(b.get("ativo", True)),
            "horarios": b.get("horarios", []) or [], "analiticos_padrao": b.get("analiticos_padrao", []) or [],
            "zonas_intrusao": b.get("zonas_intrusao", []) or []}
    c = _db()
    row = c.execute("SELECT id FROM entities WHERE entity='ConfigAnalitico' AND json_extract(data,'$.camera_id')=?", (cid,)).fetchone()
    c.close()
    if row:
        _update_ent("ConfigAnalitico", row["id"], data)
    else:
        _create_ent("ConfigAnalitico", data)
    return {"success": True}


@router.post("/api/comercial/analiticos/limpar")
async def analiticos_limpar(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json()
    cid = (b.get("camera_id") or "").strip()
    c = _db()
    rows = c.execute("SELECT id, data FROM entities WHERE entity='ConfigAnalitico'").fetchall()
    ndel = 0
    for r in rows:
        if json.loads(r["data"]).get("camera_id") == cid:
            c.execute("DELETE FROM entities WHERE entity='ConfigAnalitico' AND id=?", (r["id"],)); ndel += 1
    c.commit(); c.close()
    return {"success": True, "removidas": ndel}


# ---------- White-label: marca por provedor (cores + logo + dominio) ----------
def _branding_pid(u, b):
    if u and u["role"] == "provedor":
        return (u.get("provedor_id") or "")
    return (b.get("provedor_id") or "").strip()   # admin escolhe o provedor


@router.get("/api/comercial/branding/me")
def branding_me(req: Request):
    u = _current_user(req)
    if not u:
        return {}
    pid = (u.get("provedor_id") or "").strip()   # provedor: propria; cliente: herda do provedor
    if not pid:
        return {}   # admin -> Corexia default
    prov = _get_ent("Provedor", pid)
    if not prov:
        return {}
    out = _branding_de(prov); out["dominio"] = prov.get("dominio", "")
    return out


@router.get("/api/comercial/branding/domain-ok")
def branding_domain_ok(req: Request):
    """Usado pelo Caddy (on-demand TLS): so emite certificado p/ dominio de provedor cadastrado.
    Declarado ANTES de /branding/{pid} p/ nao ser capturado pela rota dinamica."""
    dom = (req.query_params.get("domain", "") or "").lower().strip()
    if not dom:
        return JSONResponse({"ok": False}, status_code=400)
    if dom in ("grupocorexia.com.br", "www.grupocorexia.com.br"):
        return {"ok": True}
    try:
        c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='Provedor'").fetchall(); c.close()
    except Exception:
        return JSONResponse({"ok": False}, status_code=500)
    for r in rows:
        if (json.loads(r["data"]).get("dominio") or "").lower().strip() == dom:
            return {"ok": True}
    return JSONResponse({"ok": False}, status_code=404)


@router.get("/api/comercial/branding/{pid}")
def branding_get(pid: str, req: Request):
    u = _current_user(req)
    if not (u and (u["role"] == "admin" or u.get("provedor_id") == pid)):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    prov = _get_ent("Provedor", pid)
    if not prov:
        return JSONResponse({"error": "provedor nao encontrado"}, status_code=404)
    out = _branding_de(prov); out["dominio"] = prov.get("dominio", "")
    return out


@router.post("/api/comercial/branding/salvar")
async def branding_salvar(req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    b = await req.json(); pid = _branding_pid(u, b)
    if not (u["role"] == "admin" or (u["role"] == "provedor" and u.get("provedor_id") == pid)):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    prov = _get_ent("Provedor", pid)
    if not prov:
        return JSONResponse({"error": "provedor nao encontrado"}, status_code=404)
    br = prov.get("branding") or {}
    for k in ("nome_marca", "cor", "cor_menu", "logo"):
        if k in b:
            br[k] = (b.get(k) or "")
    patch = {"branding": br}
    if "dominio" in b:
        patch["dominio"] = (b.get("dominio") or "").strip().lower()
    _update_ent("Provedor", pid, patch)
    return {"success": True}


@router.post("/api/comercial/branding/logo")
async def branding_logo(req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    b = await req.json(); pid = _branding_pid(u, b)
    if not (u["role"] == "admin" or (u["role"] == "provedor" and u.get("provedor_id") == pid)):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    data = b.get("data", "")
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        return JSONResponse({"error": "imagem invalida"}, status_code=400)
    if not raw or len(raw) > 2 * 1024 * 1024:
        return JSONResponse({"error": "logo vazia ou > 2MB"}, status_code=400)
    braindir = os.path.join(HERE, "web", "brand")
    os.makedirs(braindir, exist_ok=True)
    fn = "prov_%s.png" % pid
    with open(os.path.join(braindir, fn), "wb") as f:
        f.write(raw)
    logo = "/brand/" + fn
    prov = _get_ent("Provedor", pid) or {}
    br = prov.get("branding") or {}; br["logo"] = logo
    _update_ent("Provedor", pid, {"branding": br})
    return {"success": True, "logo": logo}


# ---------- Mapa de Calor (heatmap): detector ingere grade por hora; tela consulta somado ----------
@router.post("/api/comercial/heatmap/ingest")
async def heatmap_ingest(req: Request):
    b = await req.json()
    if not WEBHOOK_SECRET or b.get("secret") != WEBHOOK_SECRET:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = (b.get("camera_id") or "").strip(); bucket = (b.get("bucket") or "").strip()
    gw = int(b.get("gw", 0)); gh = int(b.get("gh", 0)); grid = b.get("grid") or []
    if not (cid and bucket and gw and gh and len(grid) == gw * gh):
        return JSONResponse({"error": "payload invalido"}, status_code=400)
    eid = cid + "_" + bucket; now = _now_iso()
    c = _db()
    row = c.execute("SELECT data FROM entities WHERE entity='Heatmap' AND id=?", (eid,)).fetchone()
    if row:
        d = json.loads(row["data"])
        if d.get("gw") == gw and d.get("gh") == gh and len(d.get("grid", [])) == gw * gh:
            d["grid"] = [d["grid"][i] + grid[i] for i in range(gw * gh)]
        else:
            d = {"camera_id": cid, "bucket": bucket, "gw": gw, "gh": gh, "grid": grid}
        d["updated"] = now
        c.execute("UPDATE entities SET data=?, updated_date=? WHERE entity='Heatmap' AND id=?", (json.dumps(d), now, eid))
    else:
        d = {"camera_id": cid, "bucket": bucket, "gw": gw, "gh": gh, "grid": grid, "updated": now}
        c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
                  ("Heatmap", eid, json.dumps(d), now, now))
    c.commit(); c.close()
    return {"ok": True}


@router.get("/api/comercial/heatmap/grid")
def heatmap_grid(req: Request):
    u = _current_user(req)
    if not (u and u["role"] == "admin"):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    cid = req.query_params.get("camera_id", ""); de = req.query_params.get("de", ""); ate = req.query_params.get("ate", "")
    c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='Heatmap'").fetchall(); c.close()
    gw = gh = 0; acc = None; total = 0; nb = 0
    for r in rows:
        d = json.loads(r["data"])
        if d.get("camera_id") != cid:
            continue
        bk = d.get("bucket", "")
        if (de and bk < de) or (ate and bk > ate):
            continue
        g = d.get("grid", [])
        if not g:
            continue
        if acc is None:
            gw = d.get("gw", 0); gh = d.get("gh", 0)
            if not (gw and gh and len(g) == gw * gh):
                continue
            acc = [0] * (gw * gh)
        if d.get("gw") == gw and d.get("gh") == gh and len(g) == gw * gh:
            for i in range(gw * gh):
                acc[i] += g[i]
            total += sum(g); nb += 1
    if acc is None:
        return {"gw": 0, "gh": 0, "grid": [], "max": 0, "total": 0, "buckets": 0}
    return {"gw": gw, "gh": gh, "grid": acc, "max": max(acc) if acc else 0, "total": total, "buckets": nb}


# ---------- Comissionamento ----------
def _mine(u, d):
    return u["role"] == "admin" or (d.get("provedor_id") and d.get("provedor_id") == u.get("provedor_id"))


@router.get("/api/comercial/clientes-asaas")
def clientes_asaas(req: Request):
    """Clientes distintos que tem fatura (p/ o dropdown do vinculo de comissao)."""
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    c = _db(); rows = c.execute("SELECT data FROM entities WHERE entity='Fatura'").fetchall(); c.close()
    vistos = {}
    for r in rows:
        d = json.loads(r["data"])
        if not _mine(u, d):
            continue
        cid = d.get("asaas_customer_id") or d.get("cliente_id") or ""
        if cid and cid not in vistos:
            vistos[cid] = d.get("cliente_nome") or cid
    return sorted([{"id": k, "nome": v} for k, v in vistos.items()], key=lambda x: x["nome"].lower())


@router.get("/api/comercial/comissoes/receber")
def comissoes_receber(req: Request):
    u = _current_user(req)
    if not u:
        return JSONResponse({"error": "nao autenticado"}, status_code=401)
    c = _db()
    vincs = [dict(json.loads(r["data"]), id=r["id"]) for r in c.execute("SELECT id,data FROM entities WHERE entity='Comissao'").fetchall()]
    fats = [dict(json.loads(r["data"]), id=r["id"]) for r in c.execute("SELECT id,data FROM entities WHERE entity='Fatura'").fetchall()]
    pagtos = [json.loads(r["data"]) for r in c.execute("SELECT data FROM entities WHERE entity='CommissionPagto'").fetchall()]
    c.close()
    vincs = [v for v in vincs if _mine(u, v) and v.get("status", "ativo") == "ativo"]
    porcli = {}
    for f in fats:
        if not _mine(u, f) or f.get("status") != "paga":
            continue
        k = f.get("asaas_customer_id") or f.get("cliente_id") or ""
        porcli.setdefault(k, []).append(f)
    pagos = set((p.get("vendedor_id", ""), p.get("fatura_id", "")) for p in pagtos)
    grupos = {}
    tot_pend = tot_pago = 0.0
    for v in vincs:
        perc = float(v.get("valor", 0) or 0); tipo = v.get("tipo", "percentual")
        vid = v.get("vendedor_id", "")
        for f in porcli.get(v.get("cliente_id", ""), []):
            val = float(f.get("valor", 0) or 0)
            com = round(val * perc / 100.0, 2) if tipo == "percentual" else perc
            pago = (vid, f.get("id", "")) in pagos
            g = grupos.setdefault(vid, {"vendedor": v.get("vendedor_nome", ""), "pendente": 0.0, "pago": 0.0, "linhas": []})
            g["linhas"].append({"cliente": f.get("cliente_nome", ""), "fatura_id": f.get("id", ""),
                                "fatura_pago_em": f.get("pago_em", "") or f.get("vencimento", ""),
                                "fatura_valor": val, "percentual": perc, "tipo": tipo, "comissao": com,
                                "status": "pago" if pago else "pendente"})
            if pago:
                g["pago"] += com; tot_pago += com
            else:
                g["pendente"] += com; tot_pend += com
    out = [{"vendedor_id": k, "vendedor": g["vendedor"], "pendente": round(g["pendente"], 2),
            "pago": round(g["pago"], 2), "linhas": g["linhas"]} for k, g in grupos.items()]
    out.sort(key=lambda x: -x["pendente"])
    return {"grupos": out, "total_pendente": round(tot_pend, 2), "total_pago": round(tot_pago, 2),
            "vinculos_ativos": len(vincs)}


@router.post("/api/comercial/comissoes/pagar")
async def comissao_pagar(req: Request):
    u = _current_user(req)
    if not (u and u["role"] in ("admin", "provedor")):
        return JSONResponse({"error": "sem permissao"}, status_code=403)
    b = await req.json(); vid = b.get("vendedor_id", ""); fid = b.get("fatura_id", "")
    if not (vid and fid):
        return JSONResponse({"error": "dados incompletos"}, status_code=400)
    c = _db(); ex = c.execute("SELECT id FROM entities WHERE entity='CommissionPagto' "
                              "AND json_extract(data,'$.vendedor_id')=? AND json_extract(data,'$.fatura_id')=?",
                              (vid, fid)).fetchone(); c.close()
    if not ex:
        data = {"vendedor_id": vid, "fatura_id": fid, "pago_em": _now_iso(), "status": "pago"}
        if u["role"] == "provedor":
            data["provedor_id"] = u["provedor_id"]
        _create_ent("CommissionPagto", data)
    return {"success": True}


# ---------- corpo da tela de Planos (CRUD via /api/entities/Plano) ----------
_PLANOS_BODY = """
<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px"><div style="flex:1"></div>
 <button class="btn-primary" onclick="novo()">+ Novo Plano</button></div>
<div id="msg" class="msg"></div>
<div class="cards"><div class="kpi"><div class="k">Total de Planos</div><div class="v" id="k_total">-</div></div>
 <div class="kpi"><div class="k">Ativos</div><div class="v" id="k_ativos" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Nome</th><th>Tipo</th><th>Valor/mes</th><th>Cameras</th><th>Extra</th><th>Gravacao</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="8" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal"><h2 id="mtitle">Novo Plano</h2><input type="hidden" id="f_id">
 <div class="fld"><label>Nome</label><input id="f_nome" placeholder="Ex: Painel Local"></div>
 <div class="two"><div class="fld"><label>Tipo</label><select id="f_tipo"><option value="painel_local">Painel Local</option><option value="painel_cloud">Painel Cloud</option><option value="outro">Outro</option></select></div>
  <div class="fld"><label>Valor mensal (R$)</label><input id="f_valor" type="number" step="0.01" placeholder="797.00"></div></div>
 <div class="two"><div class="fld"><label>Cameras incluidas</label><input id="f_cams" type="number" placeholder="100"></div>
  <div class="fld"><label>Camera extra (R$)</label><input id="f_extra" type="number" step="0.01" placeholder="5.97"></div></div>
 <div class="two"><div class="fld"><label>Gravacao</label><select id="f_grav"><option value="local">Local</option><option value="cloud">Cloud</option><option value="nao">Sem</option></select></div>
  <div class="fld"><label>Contrato (meses)</label><input id="f_meses" type="number" placeholder="36"></div></div>
 <div class="two"><div class="fld"><label>Tipo doc</label><select id="f_doc"><option value="ambos">Ambos</option><option value="cnpj">CNPJ</option><option value="cpf">CPF</option></select></div>
  <div class="fld"><label>Status</label><select id="f_ativo"><option value="true">Ativo</option><option value="false">Inativo</option></select></div></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>
<script>
var PLANOS=[]; window.PAGE_INIT=load;
async function load(){ try{ PLANOS=await api('GET','/api/entities/Plano');
  $('k_total').textContent=PLANOS.length;
  $('k_ativos').textContent=PLANOS.filter(function(p){return p.ativo!==false&&p.ativo!=='false'}).length;
  $('rows').innerHTML=PLANOS.map(function(p){var on=(p.ativo!==false&&p.ativo!=='false');
   return '<tr><td><b>'+esc(p.nome||'-')+'</b></td><td><span class="pill">'+esc(p.tipo||'-')+'</span></td>'+
   '<td class="money">'+brl(p.valor_mensal)+'</td><td>'+(p.cameras_ao_vivo_incluidas!=null?p.cameras_ao_vivo_incluidas:'-')+'</td>'+
   '<td class="money">'+(p.camera_extra_mensal?brl(p.camera_extra_mensal):'-')+'</td><td>'+esc(p.gravacao||'-')+'</td>'+
   '<td><span class="pill '+(on?'ok':'off')+'">'+(on?'Ativo':'Inativo')+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="editar(\\''+p.id+'\\')">editar</button>'+
   '<button class="act" onclick="excluir(\\''+p.id+'\\')">excluir</button></td></tr>';}).join('')
   ||'<tr><td colspan="8" class="center">nenhum plano</td></tr>';
 }catch(e){ msg('Erro ao carregar: '+e.message); } }
function novo(){ $('mtitle').textContent='Novo Plano';['f_id','f_nome','f_valor','f_cams','f_extra'].forEach(function(i){$(i).value=''});
 $('f_tipo').value='painel_local';$('f_grav').value='local';$('f_meses').value='36';$('f_doc').value='ambos';$('f_ativo').value='true';$('ov').classList.add('open'); }
function editar(id){ var p=PLANOS.filter(function(x){return x.id===id})[0]; if(!p)return; $('mtitle').textContent='Editar Plano';$('f_id').value=p.id;
 $('f_nome').value=p.nome||'';$('f_tipo').value=p.tipo||'outro';$('f_valor').value=p.valor_mensal!=null?p.valor_mensal:'';$('f_cams').value=p.cameras_ao_vivo_incluidas!=null?p.cameras_ao_vivo_incluidas:'';
 $('f_extra').value=p.camera_extra_mensal!=null?p.camera_extra_mensal:'';$('f_grav').value=p.gravacao||'local';$('f_meses').value=p.contrato_meses!=null?p.contrato_meses:36;$('f_doc').value=p.tipo_doc||'ambos';
 $('f_ativo').value=(p.ativo!==false&&p.ativo!=='false')?'true':'false';$('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var v=parseFloat($('f_valor').value||0);
 var b={nome:$('f_nome').value.trim(),tipo:$('f_tipo').value,valor_mensal:v,value:v,
  cameras_ao_vivo_incluidas:parseInt($('f_cams').value||0)||0,camera_extra_mensal:parseFloat($('f_extra').value||0)||0,
  gravacao:$('f_grav').value,contrato_meses:parseInt($('f_meses').value||0)||0,tipo_doc:$('f_doc').value,ativo:$('f_ativo').value==='true'};
 if(!b.nome){msg('Informe o nome');return;}
 try{ var id=$('f_id').value; if(id){await api('PUT','/api/entities/Plano/'+id,b);}else{await api('POST','/api/entities/Plano',b);}
  fecha();msg('Plano salvo.',true);load(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function excluir(id){ if(!confirm('Excluir este plano?'))return;
 try{ await api('DELETE','/api/entities/Plano/'+id);msg('Excluido.',true);load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Propostas ----------
_PROPOSTAS_BODY = """
<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px"><div style="flex:1"></div>
 <button class="btn-primary" onclick="novo()">+ Nova Proposta</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Aguardando</div><div class="v" id="k_ag" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Enviadas</div><div class="v" id="k_env">-</div></div>
 <div class="kpi"><div class="k">Assinadas</div><div class="v" id="k_ass" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Fechadas</div><div class="v" id="k_fec" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Cliente</th><th>Plano</th><th>Cameras</th><th>Valor/mes</th><th>Consultor</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="7" class="center">carregando...</td></tr></tbody></table>

<div class="ov" id="ov"><div class="modal" style="max-width:640px"><h2 id="mtitle">Nova Proposta</h2><input type="hidden" id="p_id">
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:4px 0 10px">Dados do cliente</div>
 <div class="fld"><label>Nome / Razao social</label><input id="p_nome" placeholder="Nome do cliente"></div>
 <div class="two"><div class="fld"><label>Tipo doc</label><select id="p_doctype"><option value="cnpj">CNPJ</option><option value="cpf">CPF</option></select></div>
  <div class="fld"><label>Documento</label><input id="p_doc" placeholder="so numeros"></div></div>
 <div class="two"><div class="fld"><label>E-mail</label><input id="p_email" type="email" placeholder="cliente@email.com"></div>
  <div class="fld"><label>WhatsApp</label><input id="p_whats" placeholder="DDD + numero"></div></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:14px 0 10px">Endereco</div>
 <div class="two"><div class="fld"><label>CEP</label><input id="p_cep" onblur="buscaCep()" placeholder="so numeros"></div><div class="fld"><label>Cidade</label><input id="p_cidade"></div></div>
 <div class="two"><div class="fld"><label>Logradouro</label><input id="p_log"></div><div class="fld"><label>Numero</label><input id="p_num"></div></div>
 <div class="two"><div class="fld"><label>Bairro</label><input id="p_bairro"></div><div class="fld"><label>UF</label><input id="p_uf" maxlength="2"></div></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:14px 0 10px">Plano</div>
 <div class="fld"><label>Selecionar plano</label><select id="p_plano" onchange="onPlano()"><option value="">- selecione -</option></select></div>
 <div class="two"><div class="fld"><label>Qtd. cameras</label><input id="p_cams" type="number"></div>
  <div class="fld"><label>Valor mensal (R$)</label><input id="p_valor" type="number" step="0.01"></div></div>
 <div class="two"><div class="fld"><label>Consultor</label><input id="p_consultor"></div>
  <div class="fld"><label>Validade</label><input id="p_validade" type="date"></div></div>
 <div class="fld"><label>Observacoes</label><input id="p_obs"></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Criar Proposta</button></div></div></div>
<script>
var PROPS=[], PLANOS=[];
window.PAGE_INIT=initP;
async function initP(){ try{PLANOS=await api('GET','/api/entities/Plano');}catch(e){PLANOS=[];} fillPlanos(); load(); }
function fillPlanos(){ var s=$('p_plano'); if(!s)return;
 s.innerHTML='<option value="">- selecione -</option>'+PLANOS.map(function(p){return '<option value="'+p.id+'">'+esc(p.nome)+' ('+brl(p.valor_mensal)+')</option>';}).join(''); }
function onPlano(){ var p=PLANOS.filter(function(x){return x.id===$('p_plano').value})[0]; if(p){$('p_valor').value=(p.valor_mensal!=null?p.valor_mensal:'');} }
async function buscaCep(){ var cep=($('p_cep').value||'').replace(/\\D/g,''); if(cep.length!==8)return;
 try{ var r=await fetch('https://viacep.com.br/ws/'+cep+'/json/'); var d=await r.json();
  if(d.erro){ msg('CEP nao encontrado'); return; }
  $('p_log').value=d.logradouro||''; $('p_bairro').value=d.bairro||''; $('p_cidade').value=d.localidade||''; $('p_uf').value=d.uf||'';
  if($('p_num')) $('p_num').focus();
 }catch(e){ /* silencioso: deixa preencher a mao */ } }
function stInfo(s){ return {pendente:['Aguardando','off'],aprovada:['Aprovada','ok'],enviada:['Enviada','off'],assinada:['Assinada','ok'],fechada:['Fechada','ok'],cancelada:['Cancelada','off']}[s]||['?','off']; }
function rowP(p){ var st=p.status||'pendente'; var si=stInfo(st);
 var ac='<button class="act" onclick="ver(\\''+p.id+'\\')">ver</button>';
 if(st==='pendente') ac+='<button class="act" onclick="aprovar(\\''+p.id+'\\')">aprovar</button>';
 if(st==='aprovada'||st==='enviada') ac+='<button class="act" onclick="enviarCod(\\''+p.id+'\\')">enviar codigo</button>';
 if(st==='enviada') ac+='<button class="act" style="color:var(--ok)" onclick="assinar(\\''+p.id+'\\')">assinar</button>';
 ac+='<button class="act" onclick="excluir(\\''+p.id+'\\')">excluir</button>';
 return '<tr><td><b>'+esc(p.cliente_nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(p.email||'')+'</div></td>'+
  '<td>'+esc(p.plano_nome||'-')+'</td><td>'+(p.qtd_cameras!=null?p.qtd_cameras:'-')+'</td>'+
  '<td class="money">'+brl(p.valor_mensal)+'</td><td>'+esc(p.consultor||'-')+'</td>'+
  '<td><span class="pill '+si[1]+'">'+si[0]+'</span></td><td style="text-align:right;white-space:nowrap">'+ac+'</td></tr>'; }
async function load(){ try{ PROPS=await api('GET','/api/entities/Proposta');
  var by=function(s){return PROPS.filter(function(p){return (p.status||'pendente')===s}).length};
  $('k_ag').textContent=by('pendente'); $('k_env').textContent=by('enviada'); $('k_ass').textContent=by('assinada'); $('k_fec').textContent=by('fechada');
  $('rows').innerHTML=PROPS.map(rowP).join('')||'<tr><td colspan="7" class="center">Nenhuma proposta</td></tr>';
 }catch(e){ msg('Erro ao carregar: '+e.message); } }
function novo(){ $('mtitle').textContent='Nova Proposta';
 ['p_id','p_nome','p_doc','p_email','p_whats','p_cep','p_cidade','p_log','p_num','p_bairro','p_uf','p_cams','p_valor','p_consultor','p_validade','p_obs'].forEach(function(i){$(i).value='';});
 $('p_doctype').value='cnpj'; fillPlanos(); $('p_plano').value=''; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
function ver(id){ var p=PROPS.filter(function(x){return x.id===id})[0]; if(!p)return;
 alert('Proposta: '+(p.cliente_nome||'')+'\\nPlano: '+(p.plano_nome||'')+'\\nValor: '+brl(p.valor_mensal)+'\\nStatus: '+(p.status||'pendente')); }
async function salvar(){ var p=PLANOS.filter(function(x){return x.id===$('p_plano').value})[0]||{};
 var b={ cliente_nome:$('p_nome').value.trim(), document_type:$('p_doctype').value, document_number:$('p_doc').value.trim(),
  email:$('p_email').value.trim(), whatsapp:$('p_whats').value.trim(),
  cep:$('p_cep').value.trim(), cidade:$('p_cidade').value.trim(), logradouro:$('p_log').value.trim(),
  numero:$('p_num').value.trim(), bairro:$('p_bairro').value.trim(), uf:$('p_uf').value.trim().toUpperCase(),
  plano_id:$('p_plano').value, plano_nome:(p.nome||''), tipo_plano:(p.tipo||''), contrato_meses:(p.contrato_meses||0),
  qtd_cameras:parseInt($('p_cams').value||0)||0, valor_mensal:parseFloat($('p_valor').value||0)||0,
  consultor:$('p_consultor').value.trim(), validade:$('p_validade').value, observacoes:$('p_obs').value.trim(),
  status:'pendente' };
 if(!b.cliente_nome){msg('Informe o nome do cliente');return;}
 if(!b.plano_id){msg('Selecione um plano');return;}
 try{ var id=$('p_id').value; if(id){await api('PUT','/api/entities/Proposta/'+id,b);}else{await api('POST','/api/entities/Proposta',b);}
  fecha(); msg('Proposta criada.',true); load(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function enviarCod(id){ try{ var r=await api('POST','/api/comercial/propostas/'+id+'/enviar-codigo');
 if(r&&r.enviado){msg('Codigo enviado por WhatsApp ao cliente.',true);}else{msg('Nao enviou o codigo: '+((r&&r.info)||'verifique Z-API/numero'));} load(); }catch(e){ msg('Erro: '+e.message); } }
async function assinar(id){ var code=prompt('Codigo de 6 digitos que o cliente recebeu por WhatsApp:'); if(!code)return;
 try{ var r=await api('POST','/api/comercial/propostas/'+id+'/assinar',{codigo:code.trim()});
  msg('Assinada! ('+r.modo+') '+r.alvo+' criado + Asaas '+(r.modo==='real'?'ATIVADO':'(modo teste)')+'.',true); load(); }catch(e){ msg('Erro ao assinar: '+e.message); } }
async function aprovar(id){ try{ await api('PUT','/api/entities/Proposta/'+id,{status:'aprovada'}); msg('Proposta aprovada.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(id){ if(!confirm('Excluir esta proposta?'))return; try{ await api('DELETE','/api/entities/Proposta/'+id); msg('Excluida.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Faturas ----------
_FATURAS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
 <input id="q" placeholder="Buscar fatura (cliente/numero)..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <select id="fst" onchange="render()" style="background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px;font-size:14px">
  <option value="">Todos os status</option><option value="pendente">Pendente</option><option value="vencida">Vencida</option><option value="paga">Paga</option></select>
 <button id="btsync" onclick="sync()">Sincronizar Asaas</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">A Receber</div><div class="v" id="k_rec" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Vencidas</div><div class="v" id="k_ven" style="color:var(--bad)">-</div></div>
 <div class="kpi"><div class="k">Recebido</div><div class="v" id="k_pag" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Cliente</th><th>Numero</th><th>Vencimento</th><th>Valor</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>
<script>
var FAT=[]; window.PAGE_INIT=loadF;
function dt(s){ if(!s)return '-'; var p=(''+s).slice(0,10).split('-'); return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s; }
function stF(s){ return {pendente:['Pendente','var(--accent)'],vencida:['Vencida','var(--bad)'],paga:['Paga','var(--ok)'],cancelada:['Cancelada','var(--muted)']}[s]||['?','var(--muted)']; }
async function loadF(){ try{ FAT=(await api('GET','/api/entities/Fatura?limit=3000')).filter(function(f){return !f.cliente_id;}); kpis(); render(); }catch(e){ msg('Erro ao carregar: '+e.message); } }
function kpis(){ var s={pendente:0,vencida:0,paga:0};
 FAT.forEach(function(f){ var st=f.status||'pendente'; if(s[st]!=null) s[st]+=parseFloat(f.valor||0); });
 $('k_rec').textContent=brl(s.pendente); $('k_ven').textContent=brl(s.vencida); $('k_pag').textContent=brl(s.paga); }
function render(){ var q=($('q').value||'').toLowerCase(), fs=$('fst').value;
 var arr=FAT.filter(function(f){ if(fs&&(f.status||'pendente')!==fs)return false;
  if(q && ((''+(f.cliente_nome||'')+' '+(f.numero||'')).toLowerCase().indexOf(q)<0))return false; return true; });
 $('rows').innerHTML=arr.map(function(f){ var si=stF(f.status||'pendente');
  return '<tr><td><b>'+esc(f.cliente_nome||'-')+'</b><div style="color:var(--muted);font-size:12px">Ref: '+esc(f.reference_month||'')+'</div></td>'+
   '<td>'+esc(f.numero||'-')+'</td><td>'+dt(f.vencimento)+'</td><td class="money">'+brl(f.valor)+'</td>'+
   '<td><span class="pill" style="color:'+si[1]+'">'+si[0]+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap">'+
    (f.invoice_url?'<a class="act" href="'+esc(f.invoice_url)+'" target="_blank" rel="noopener">boleto</a>':'')+
    ((f.status!=='paga')?'<button class="act" style="color:var(--ok)" onclick="pagar(\\''+f.id+'\\')">marcar paga</button>':'')+
    '<button class="act" onclick="excluirF(\\''+f.id+'\\')">excluir</button></td></tr>';
 }).join('')||'<tr><td colspan="6" class="center">Nenhuma fatura. Clique em <b>Sincronizar Asaas</b>.</td></tr>';
}
async function sync(){ var b=$('btsync'); b.disabled=true; b.textContent='Sincronizando...'; msg('Buscando cobrancas no Asaas (pode levar alguns segundos)...',true);
 try{ var r=await api('POST','/api/comercial/faturas/sync'); msg((r.sincronizadas||0)+' faturas sincronizadas do Asaas.',true); await loadF(); }
 catch(e){ msg('Erro ao sincronizar: '+e.message); } finally{ b.disabled=false; b.textContent='Sincronizar Asaas'; } }
async function pagar(id){ if(!confirm('Marcar esta fatura como paga?'))return;
 try{ await api('POST','/api/comercial/faturas/'+id+'/marcar-paga'); msg('Marcada como paga.',true); loadF(); }catch(e){ msg('Erro: '+e.message); } }
async function excluirF(id){ if(!confirm('Excluir esta fatura (so aqui, nao no Asaas)?'))return;
 try{ await api('DELETE','/api/entities/Fatura/'+id); msg('Excluida.',true); loadF(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Vendedores (internos e externos) ----------
_VENDEDORES_BODY = """
<div style="display:flex;gap:12px;align-items:center;margin-bottom:16px"><div style="flex:1"></div>
 <button class="btn-primary" onclick="novoV()">+ Novo Vendedor</button></div>
<div id="msg" class="msg"></div>
<div class="cards"><div class="kpi"><div class="k">Total de Vendedores</div><div class="v" id="k_tot">-</div></div>
 <div class="kpi"><div class="k">Ativos</div><div class="v" id="k_at" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Inativos</div><div class="v" id="k_in">-</div></div></div>
<table><thead><tr><th>Nome</th><th>E-mail</th><th>Telefone</th><th>Tipo</th><th>R$/KM</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="7" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal"><h2 id="mtitle">Novo Vendedor</h2><input type="hidden" id="v_id">
 <div class="fld"><label>Nome *</label><input id="v_nome"></div>
 <div class="fld"><label>E-mail *</label><input id="v_email" type="email"></div>
 <div class="two"><div class="fld"><label>Telefone</label><input id="v_tel"></div>
  <div class="fld"><label>Tipo</label><select id="v_tipo"><option value="externo">Externo</option><option value="interno">Interno</option></select></div></div>
 <div class="two"><div class="fld"><label>Valor por KM (R$)</label><input id="v_km" type="number" step="0.01" placeholder="0.70"></div>
  <div class="fld"><label>Status</label><select id="v_status"><option value="ativo">Ativo</option><option value="inativo">Inativo</option></select></div></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvarV()">Criar</button></div></div></div>
<script>
var VEND=[]; window.PAGE_INIT=loadV;
async function loadV(){ try{ VEND=await api('GET','/api/entities/Vendedor?limit=1000');
 var at=VEND.filter(function(v){return (v.status||'ativo')==='ativo'}).length;
 $('k_tot').textContent=VEND.length; $('k_at').textContent=at; $('k_in').textContent=VEND.length-at;
 $('rows').innerHTML=VEND.map(function(v){ var on=(v.status||'ativo')==='ativo';
  return '<tr><td><b>'+esc(v.nome||'-')+'</b></td><td>'+esc(v.email||'-')+'</td><td>'+esc(v.telefone||'-')+'</td>'+
   '<td><span class="pill">'+esc(v.tipo||'externo')+'</span></td>'+
   '<td class="money">'+brl(v.valor_km||0)+'</td>'+
   '<td><span class="pill '+(on?'ok':'off')+'">'+(on?'Ativo':'Inativo')+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="editarV(\\''+v.id+'\\')">editar</button>'+
   '<button class="act" onclick="excluirV(\\''+v.id+'\\')">excluir</button></td></tr>'; }).join('')
   ||'<tr><td colspan="7" class="center">Nenhum vendedor. Cadastre o primeiro.</td></tr>';
 }catch(e){ msg('Erro: '+e.message); } }
function novoV(){ $('mtitle').textContent='Novo Vendedor'; ['v_id','v_nome','v_email','v_tel','v_km'].forEach(function(i){$(i).value='';}); $('v_tipo').value='externo'; $('v_status').value='ativo'; $('ov').classList.add('open'); }
function editarV(id){ var v=VEND.filter(function(x){return x.id===id})[0]; if(!v)return; $('mtitle').textContent='Editar Vendedor'; $('v_id').value=v.id;
 $('v_nome').value=v.nome||''; $('v_email').value=v.email||''; $('v_tel').value=v.telefone||''; $('v_tipo').value=v.tipo||'externo'; $('v_km').value=(v.valor_km!=null?v.valor_km:''); $('v_status').value=v.status||'ativo'; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvarV(){ var b={ nome:$('v_nome').value.trim(), email:$('v_email').value.trim(), telefone:$('v_tel').value.trim(), tipo:$('v_tipo').value, valor_km:parseFloat($('v_km').value||0)||0, status:$('v_status').value };
 if(!b.nome||!b.email){ msg('Nome e e-mail sao obrigatorios'); return; }
 try{ var id=$('v_id').value; if(id){await api('PUT','/api/entities/Vendedor/'+id,b);}else{await api('POST','/api/entities/Vendedor',b);}
  fecha(); msg('Vendedor salvo.',true); loadV(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function excluirV(id){ if(!confirm('Excluir este vendedor?'))return; try{ await api('DELETE','/api/entities/Vendedor/'+id); msg('Excluido.',true); loadV(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Comissionamento ----------
_COMISSIONAMENTO_BODY = """
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Total a Pagar (Pendente)</div><div class="v" id="k_pend" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Total Pago</div><div class="v" id="k_pago" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Vinculos Ativos</div><div class="v" id="k_viv">-</div></div></div>
<div style="display:flex;gap:8px;margin-bottom:16px">
 <button id="tab1" class="btn-primary" onclick="tab(1)">Configuracao de Comissoes</button>
 <button id="tab2" onclick="tab(2)">Comissoes a Receber</button></div>

<div id="pane1">
 <div style="display:flex;align-items:center;margin-bottom:12px"><h3 style="margin:0;font-size:16px">Vinculos Vendedor &rarr; Cliente</h3><div style="flex:1"></div>
  <button class="btn-primary" onclick="novoVinc()">+ Novo Vinculo</button></div>
 <table><thead><tr><th>Vendedor</th><th>Cliente</th><th>Tipo</th><th>Valor</th><th>Status</th><th></th></tr></thead>
 <tbody id="vrows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table></div>

<div id="pane2" style="display:none">
 <input id="q" placeholder="Buscar por vendedor ou cliente..." oninput="renderReceber()" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px;margin-bottom:14px">
 <div id="grupos"><div class="center">carregando...</div></div></div>

<div class="ov" id="ov"><div class="modal"><h2>Novo Vinculo de Comissao</h2><input type="hidden" id="c_id">
 <div class="fld"><label>Vendedor *</label><select id="c_vend"><option value="">- selecione -</option></select></div>
 <div class="fld"><label>Cliente *</label><select id="c_cli"><option value="">- selecione -</option></select></div>
 <div class="two"><div class="fld"><label>Tipo de Comissao *</label><select id="c_tipo"><option value="percentual">Percentual (%)</option><option value="fixo">Valor fixo (R$)</option></select></div>
  <div class="fld"><label>Valor *</label><input id="c_valor" type="number" step="0.01" placeholder="Ex: 10"></div></div>
 <div class="fld"><label>Status</label><select id="c_status"><option value="ativo">Ativo</option><option value="inativo">Inativo</option></select></div>
 <div class="fld"><label>Observacoes</label><input id="c_obs" placeholder="Opcional"></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvarVinc()">Salvar</button></div></div></div>
<script>
var VINC=[], VENDS=[], CLIS=[], RECEBER={grupos:[]};
window.PAGE_INIT=initC;
async function initC(){ await Promise.all([loadVinc(), loadDrops(), loadReceber()]); }
function tab(n){ $('pane1').style.display=n===1?'':'none'; $('pane2').style.display=n===2?'':'none';
 $('tab1').className=n===1?'btn-primary':''; $('tab2').className=n===2?'btn-primary':''; if(n===2) renderReceber(); }
async function loadDrops(){ try{ VENDS=await api('GET','/api/entities/Vendedor?limit=1000'); }catch(e){ VENDS=[]; }
 try{ CLIS=await api('GET','/api/comercial/clientes-asaas'); }catch(e){ CLIS=[]; }
 $('c_vend').innerHTML='<option value="">- selecione -</option>'+VENDS.map(function(v){return '<option value="'+v.id+'">'+esc(v.nome)+'</option>';}).join('');
 $('c_cli').innerHTML='<option value="">- selecione -</option>'+CLIS.map(function(c){return '<option value="'+c.id+'">'+esc(c.nome)+'</option>';}).join(''); }
async function loadVinc(){ try{ VINC=await api('GET','/api/entities/Comissao?limit=2000');
 $('vrows').innerHTML=VINC.map(function(v){ var on=(v.status||'ativo')==='ativo';
  return '<tr><td><b>'+esc(v.vendedor_nome||'-')+'</b></td><td>'+esc(v.cliente_nome||'-')+'</td>'+
   '<td>'+esc(v.tipo==='fixo'?'Fixo':'Percentual')+'</td><td class="money">'+(v.tipo==='fixo'?brl(v.valor):(v.valor+'%'))+'</td>'+
   '<td><span class="pill '+(on?'ok':'off')+'">'+(on?'Ativo':'Inativo')+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="editarVinc(\\''+v.id+'\\')">editar</button>'+
   '<button class="act" onclick="excluirVinc(\\''+v.id+'\\')">excluir</button></td></tr>'; }).join('')
   ||'<tr><td colspan="6" class="center">Nenhum vinculo. Clique em Novo Vinculo.</td></tr>';
 }catch(e){ msg('Erro: '+e.message); } }
async function loadReceber(){ try{ RECEBER=await api('GET','/api/comercial/comissoes/receber');
 $('k_pend').textContent=brl(RECEBER.total_pendente); $('k_pago').textContent=brl(RECEBER.total_pago); $('k_viv').textContent=RECEBER.vinculos_ativos;
 renderReceber(); }catch(e){ msg('Erro: '+e.message); } }
function renderReceber(){ var q=($('q')?$('q').value:'').toLowerCase();
 var gs=(RECEBER.grupos||[]).filter(function(g){ if(!q)return true; if((g.vendedor||'').toLowerCase().indexOf(q)>=0)return true;
   return g.linhas.some(function(l){return (l.cliente||'').toLowerCase().indexOf(q)>=0;}); });
 $('grupos').innerHTML=gs.map(function(g){
  var linhas=g.linhas.filter(function(l){ return !q || (l.cliente||'').toLowerCase().indexOf(q)>=0 || (g.vendedor||'').toLowerCase().indexOf(q)>=0; });
  var rows=linhas.map(function(l){ return '<tr><td>'+esc(l.cliente)+'</td><td>'+dtc(l.fatura_pago_em)+'</td><td class="money">'+brl(l.fatura_valor)+'</td>'+
    '<td style="font-size:12px;color:var(--muted)">'+(l.tipo==='fixo'?('Valor fixo = '+brl(l.comissao)):(brl(l.fatura_valor)+' &times; '+l.percentual+'% = '+brl(l.comissao)))+'</td><td class="money">'+brl(l.comissao)+'</td>'+
    '<td><span class="pill '+(l.status==='pago'?'ok':'off')+'">'+(l.status==='pago'?'Pago':'Pendente')+'</span></td>'+
    '<td style="text-align:right">'+(l.status==='pago'?'':'<button class="act" style="color:var(--ok)" onclick="pagarC(\\''+g.vendedor_id+'\\',\\''+l.fatura_id+'\\')">Pago</button>')+'</td></tr>'; }).join('');
  return '<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:14px;overflow:hidden">'+
   '<div style="display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border)"><b>'+esc(g.vendedor||'-')+'</b>'+
   '<span style="color:var(--muted);font-size:12px;margin-left:10px">'+g.linhas.length+' fatura(s)</span><div style="flex:1"></div>'+
   '<span style="color:var(--accent);font-family:var(--mono)">Pendente '+brl(g.pendente)+'</span>'+
   '<span style="color:var(--ok);font-family:var(--mono);margin-left:16px">Pago '+brl(g.pago)+'</span></div>'+
   '<table><thead><tr><th>Cliente</th><th>Fatura paga em</th><th>Valor fatura</th><th>C&aacute;lculo</th><th>Comiss&atilde;o</th><th>Status</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>';
 }).join('')||'<div class="center">Sem comissoes a receber. Crie vinculos e sincronize faturas pagas.</div>'; }
function dtc(s){ if(!s)return '-'; var p=(''+s).slice(0,10).split('-'); return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s; }
function novoVinc(){ $('c_id').value=''; $('c_vend').value=''; $('c_cli').value=''; $('c_tipo').value='percentual'; $('c_valor').value=''; $('c_status').value='ativo'; $('c_obs').value=''; $('ov').classList.add('open'); }
function editarVinc(id){ var v=VINC.filter(function(x){return x.id===id})[0]; if(!v)return; $('c_id').value=v.id; $('c_vend').value=v.vendedor_id||''; $('c_cli').value=v.cliente_id||'';
 $('c_tipo').value=v.tipo||'percentual'; $('c_valor').value=v.valor!=null?v.valor:''; $('c_status').value=v.status||'ativo'; $('c_obs').value=v.observacoes||''; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvarVinc(){ var vend=VENDS.filter(function(x){return x.id===$('c_vend').value})[0]||{};
 var cli=CLIS.filter(function(x){return x.id===$('c_cli').value})[0]||{};
 var b={ vendedor_id:$('c_vend').value, vendedor_nome:vend.nome||'', cliente_id:$('c_cli').value, cliente_nome:cli.nome||'',
  tipo:$('c_tipo').value, valor:parseFloat($('c_valor').value||0)||0, status:$('c_status').value, observacoes:$('c_obs').value.trim() };
 if(!b.vendedor_id){msg('Selecione o vendedor');return;} if(!b.cliente_id){msg('Selecione o cliente');return;}
 try{ var id=$('c_id').value; if(id){await api('PUT','/api/entities/Comissao/'+id,b);}else{await api('POST','/api/entities/Comissao',b);}
  fecha(); msg('Vinculo salvo.',true); loadVinc(); loadReceber(); }catch(e){ msg('Erro: '+e.message); } }
async function excluirVinc(id){ if(!confirm('Excluir este vinculo?'))return; try{ await api('DELETE','/api/entities/Comissao/'+id); msg('Excluido.',true); loadVinc(); loadReceber(); }catch(e){ msg('Erro: '+e.message); } }
async function pagarC(vid,fid){ if(!confirm('Marcar esta comissao como paga ao vendedor?'))return;
 try{ await api('POST','/api/comercial/comissoes/pagar',{vendedor_id:vid,fatura_id:fid}); msg('Comissao marcada como paga.',true); loadReceber(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Contas a Pagar (despesas) ----------
_CONTAS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
 <input id="q" placeholder="Buscar despesa..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <button class="btn-primary" onclick="novoD()">+ Nova Despesa</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">A Pagar</div><div class="v" id="k_ap" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Vencidas</div><div class="v" id="k_ve" style="color:var(--bad)">-</div></div>
 <div class="kpi"><div class="k">Pago</div><div class="v" id="k_pg" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Descricao</th><th>Categoria</th><th>Fornecedor</th><th>Vencimento</th><th>Valor</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="7" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:620px"><h2 id="mtitle">Nova Despesa</h2><input type="hidden" id="d_id">
 <div class="fld"><label>Descricao</label><input id="d_desc" placeholder="Descricao da despesa"></div>
 <div class="two"><div class="fld"><label>Categoria</label><select id="d_cat">
   <option>Equipamentos</option><option>Manutencao</option><option>Aluguel</option><option>Utilidades</option>
   <option>Salarios</option><option>Impostos</option><option>Fornecedores</option><option selected>Outros</option></select></div>
  <div class="fld"><label>Fornecedor</label><input id="d_forn" placeholder="Nome do fornecedor"></div></div>
 <div class="two"><div class="fld"><label>Valor (R$)</label><input id="d_valor" type="number" step="0.01"></div>
  <div class="fld"><label>Data de Vencimento</label><input id="d_venc" type="date"></div></div>
 <div class="two"><div class="fld"><label>Status</label><select id="d_status"><option value="pendente">Pendente</option><option value="pago">Pago</option></select></div>
  <div class="fld"><label>Recorrencia</label><select id="d_rec"><option value="unica">Unica</option><option value="mensal">Mensal</option><option value="anual">Anual</option></select></div></div>
 <div class="two"><div class="fld"><label>Metodo de Pagamento</label><select id="d_met"><option value="">Selecione</option><option>PIX</option><option>Boleto</option><option>Cartao</option><option>Dinheiro</option><option>Transferencia</option></select></div>
  <div class="fld"><label>Data do Pagamento</label><input id="d_dtpag" type="date"></div></div>
 <div class="fld"><label>Observacoes</label><input id="d_obs" placeholder="Observacoes sobre a despesa"></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvarD()">Criar</button></div></div></div>
<script>
var DESP=[]; window.PAGE_INIT=loadD;
function hoje(){ var d=new Date(); return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); }
function dtd(s){ if(!s)return '-'; var p=(''+s).slice(0,10).split('-'); return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s; }
function stD(d){ var st=d.status||'pendente'; if(st==='pago')return ['Pago','var(--ok)'];
 if((d.vencimento||'')<hoje())return ['Vencida','var(--bad)']; return ['Pendente','var(--accent)']; }
async function loadD(){ try{ DESP=await api('GET','/api/entities/Despesa?limit=2000'); kpis(); render(); }catch(e){ msg('Erro: '+e.message); } }
function kpis(){ var h=hoje(), a=0,v=0,p=0; DESP.forEach(function(d){ var val=parseFloat(d.valor||0);
  if((d.status||'pendente')==='pago'){p+=val;} else if((d.vencimento||'')<h){v+=val;} else {a+=val;} });
 $('k_ap').textContent=brl(a); $('k_ve').textContent=brl(v); $('k_pg').textContent=brl(p); }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=DESP.filter(function(d){ return !q || ((''+(d.descricao||'')+' '+(d.fornecedor||'')+' '+(d.categoria||'')).toLowerCase().indexOf(q)>=0); });
 $('rows').innerHTML=arr.map(function(d){ var si=stD(d);
  return '<tr><td><b>'+esc(d.descricao||'-')+'</b></td><td><span class="pill">'+esc(d.categoria||'-')+'</span></td>'+
   '<td>'+esc(d.fornecedor||'-')+'</td><td>'+dtd(d.vencimento)+'</td><td class="money">'+brl(d.valor)+'</td>'+
   '<td><span class="pill" style="color:'+si[1]+'">'+si[0]+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap">'+((d.status!=='pago')?'<button class="act" style="color:var(--ok)" onclick="pagarD(\\''+d.id+'\\')">pagar</button>':'')+
   '<button class="act" onclick="editarD(\\''+d.id+'\\')">editar</button><button class="act" onclick="excluirD(\\''+d.id+'\\')">excluir</button></td></tr>'; }).join('')
   ||'<tr><td colspan="7" class="center">Nenhuma despesa. Clique em Nova Despesa.</td></tr>'; }
function novoD(){ $('mtitle').textContent='Nova Despesa'; ['d_id','d_desc','d_forn','d_valor','d_dtpag','d_obs'].forEach(function(i){$(i).value='';});
 $('d_cat').value='Outros'; $('d_venc').value=hoje(); $('d_status').value='pendente'; $('d_rec').value='unica'; $('d_met').value=''; $('ov').classList.add('open'); }
function editarD(id){ var d=DESP.filter(function(x){return x.id===id})[0]; if(!d)return; $('mtitle').textContent='Editar Despesa'; $('d_id').value=d.id;
 $('d_desc').value=d.descricao||''; $('d_cat').value=d.categoria||'Outros'; $('d_forn').value=d.fornecedor||''; $('d_valor').value=d.valor!=null?d.valor:'';
 $('d_venc').value=(d.vencimento||'').slice(0,10); $('d_status').value=d.status||'pendente'; $('d_rec').value=d.recorrencia||'unica';
 $('d_met').value=d.metodo||''; $('d_dtpag').value=(d.data_pagamento||'').slice(0,10); $('d_obs').value=d.observacoes||''; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvarD(){ var b={ descricao:$('d_desc').value.trim(), categoria:$('d_cat').value, fornecedor:$('d_forn').value.trim(),
  valor:parseFloat($('d_valor').value||0)||0, vencimento:$('d_venc').value, status:$('d_status').value, recorrencia:$('d_rec').value,
  metodo:$('d_met').value, data_pagamento:$('d_dtpag').value, observacoes:$('d_obs').value.trim() };
 if(!b.descricao){ msg('Informe a descricao'); return; }
 try{ var id=$('d_id').value; if(id){await api('PUT','/api/entities/Despesa/'+id,b);}else{await api('POST','/api/entities/Despesa',b);}
  fecha(); msg('Despesa salva.',true); loadD(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function pagarD(id){ try{ await api('PUT','/api/entities/Despesa/'+id,{status:'pago',data_pagamento:hoje()}); msg('Marcada como paga.',true); loadD(); }catch(e){ msg('Erro: '+e.message); } }
async function excluirD(id){ if(!confirm('Excluir esta despesa?'))return; try{ await api('DELETE','/api/entities/Despesa/'+id); msg('Excluida.',true); loadD(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


# ---------- corpo da tela de Contratos (gera contrato-base preenchido a partir da proposta) ----------
_CONTRATOS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
 <input id="q" placeholder="Buscar contrato..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <button class="btn-primary" onclick="novoC()">+ Gerar contrato</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Rascunhos</div><div class="v" id="k_ra" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Enviados</div><div class="v" id="k_en">-</div></div>
 <div class="kpi"><div class="k">Assinados</div><div class="v" id="k_as" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Provedor / Cliente</th><th>Documento</th><th>Local / Data</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:560px"><h2>Gerar contrato</h2>
 <div class="fld"><label>Preencher contrato com a proposta</label><select id="g_prop" onchange="onProp()"><option value="">- selecione a proposta -</option></select></div>
 <div id="prev" style="display:none;background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:12px 14px;font-size:13px;margin-bottom:13px">
  <div style="color:var(--muted);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Sera preenchido no contrato</div>
  <div><b id="pv_nome">-</b></div><div id="pv_doc" style="color:var(--muted)">-</div></div>
 <div class="two"><div class="fld"><label>Local</label><input id="g_local" placeholder="Cidade/UF"></div>
  <div class="fld"><label>Data do contrato</label><input id="g_data" type="date"></div></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvarC()">Gerar contrato</button></div></div></div>
<script>
var CONTR=[], PROPS=[]; window.PAGE_INIT=initC;
var MESES=['janeiro','fevereiro','marco','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'];
function hojeC(){ var d=new Date(); return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); }
function digs(s){ return (''+(s||'')).replace(/\\D/g,''); }
function fmtDoc(raw){ var d=digs(raw);
 if(d.length===14) return d.slice(0,2)+'.'+d.slice(2,5)+'.'+d.slice(5,8)+'/'+d.slice(8,12)+'-'+d.slice(12,14);
 if(d.length===11) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9,11);
 return raw||''; }
function dataExtenso(iso){ var p=(''+(iso||'')).slice(0,10).split('-'); if(p.length!==3||!p[0]) return '';
 return p[2]+' de '+(MESES[parseInt(p[1],10)-1]||'')+' de '+p[0]; }
function stInfoC(s){ return {rascunho:['Rascunho','off'],enviado:['Enviado','off'],assinado:['Assinado','ok']}[s||'rascunho']||['?','off']; }
async function initC(){ try{ PROPS=await api('GET','/api/entities/Proposta'); }catch(e){ PROPS=[]; } fillProp(); loadC(); }
function fillProp(){ var s=$('g_prop'); if(!s)return;
 s.innerHTML='<option value="">- selecione a proposta -</option>'+PROPS.map(function(p){
  return '<option value="'+p.id+'">'+esc((p.cliente_nome||'(sem nome)')+' - '+(fmtDoc(p.document_number)||'sem doc'))+'</option>'; }).join(''); }
function onProp(){ var p=PROPS.filter(function(x){return x.id===$('g_prop').value})[0]; var pv=$('prev');
 if(!p){ pv.style.display='none'; return; }
 var lbl=digs(p.document_number).length===11?'CPF':'CNPJ';
 $('pv_nome').textContent=p.cliente_nome||'-'; $('pv_doc').textContent=lbl+': '+(fmtDoc(p.document_number)||'-');
 $('g_local').value=(p.cidade||'')+(p.uf?('/'+p.uf):''); pv.style.display='block'; }
async function loadC(){ try{ CONTR=await api('GET','/api/entities/Contrato?limit=2000'); kpisC(); render(); }catch(e){ msg('Erro: '+e.message); } }
function kpisC(){ var by=function(s){return CONTR.filter(function(c){return (c.status||'rascunho')===s}).length};
 $('k_ra').textContent=by('rascunho'); $('k_en').textContent=by('enviado'); $('k_as').textContent=by('assinado'); }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=CONTR.filter(function(c){ return !q || ((''+(c.cliente_nome||'')+' '+(c.document_number||'')+' '+(c.local||'')).toLowerCase().indexOf(q)>=0); });
 $('rows').innerHTML=arr.map(function(c){ var si=stInfoC(c.status); var lbl=digs(c.document_number).length===11?'CPF':'CNPJ';
  return '<tr><td><b>'+esc(c.cliente_nome||'-')+'</b></td><td>'+lbl+': '+esc(fmtDoc(c.document_number)||'-')+'</td>'+
   '<td>'+esc(c.local||'-')+'<div style="color:var(--muted);font-size:12px">'+esc(dataExtenso(c.data_iso)||'-')+'</div></td>'+
   '<td><span class="pill '+si[1]+'">'+si[0]+'</span></td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" style="color:var(--accent)" onclick="imprimir(\\''+c.id+'\\')">ver / PDF</button>'+
   ((c.status||'rascunho')==='rascunho'?'<button class="act" onclick="marcarEnv(\\''+c.id+'\\')">marcar enviado</button>':'')+
   '<button class="act" onclick="excluirC(\\''+c.id+'\\')">excluir</button></td></tr>'; }).join('')
   ||'<tr><td colspan="5" class="center">Nenhum contrato. Clique em Gerar contrato.</td></tr>'; }
function novoC(){ $('g_prop').value=''; $('prev').style.display='none'; $('g_local').value=''; $('g_data').value=hojeC(); $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvarC(){ var p=PROPS.filter(function(x){return x.id===$('g_prop').value})[0];
 if(!p){ msg('Selecione a proposta'); return; }
 var b={ proposta_id:p.id, cliente_nome:p.cliente_nome||'', document_type:p.document_type||'', document_number:p.document_number||'',
  cep:p.cep||'', cidade:p.cidade||'', logradouro:p.logradouro||'', numero:p.numero||'', bairro:p.bairro||'', uf:p.uf||'',
  email:p.email||'', whatsapp:p.whatsapp||'', plano_nome:p.plano_nome||'', valor_mensal:p.valor_mensal||0,
  local:$('g_local').value.trim(), data_iso:$('g_data').value||hojeC(), status:'rascunho' };
 try{ await api('POST','/api/entities/Contrato',b); fecha(); msg('Contrato gerado e preenchido.',true); loadC(); }catch(e){ msg('Erro ao gerar: '+e.message); } }
async function marcarEnv(id){ try{ await api('PUT','/api/entities/Contrato/'+id,{status:'enviado'}); msg('Marcado como enviado.',true); loadC(); }catch(e){ msg('Erro: '+e.message); } }
async function excluirC(id){ if(!confirm('Excluir este contrato?'))return; try{ await api('DELETE','/api/entities/Contrato/'+id); msg('Excluido.',true); loadC(); }catch(e){ msg('Erro: '+e.message); } }
function imprimir(id){ var c=CONTR.filter(function(x){return x.id===id})[0]; if(!c)return;
 var w=window.open('','_blank'); if(!w){ msg('Permita pop-ups para abrir o contrato'); return; }
 w.document.open(); w.document.write(contratoHTML(c)); w.document.close(); }
function contratoHTML(c){
 var digits=digs(c.document_number); var docLabel=digits.length===11?'CPF':'CNPJ';
 var nome=esc(c.cliente_nome||'PROVEDOR'), doc=esc(fmtDoc(c.document_number)||'____________________');
 var ep=[]; if(c.logradouro)ep.push(c.logradouro+(c.numero?(', '+c.numero):'')); if(c.bairro)ep.push(c.bairro);
 if(c.cidade)ep.push(c.cidade+(c.uf?('/'+c.uf):'')); if(c.cep)ep.push('CEP '+c.cep);
 var endereco=ep.length?(', com sede em '+esc(ep.join(' - '))):'';
 var local=esc(c.local||((c.cidade||'')+(c.uf?('/'+c.uf):''))||'________________');
 var dataTxt=esc(dataExtenso(c.data_iso)||'____ de ______________ de 20__');
 var css='body{margin:0;background:#525659;font-family:Georgia,\\'Times New Roman\\',serif;color:#1a1a1a}'+
  '.bar{position:sticky;top:0;background:#171a21;padding:10px 16px;text-align:right;z-index:9}'+
  '.bar button{background:#f97316;border:none;color:#1a1205;font-weight:700;font-family:sans-serif;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:14px}'+
  '.doc{max-width:820px;margin:22px auto;background:#fff;padding:56px 62px;box-shadow:0 2px 20px rgba(0,0,0,.4);line-height:1.55;font-size:15px}'+
  '.logo{font-family:sans-serif;font-weight:800;letter-spacing:3px;color:#f97316;font-size:26px;text-align:center;margin-bottom:6px}'+
  'h1{font-size:19px;text-align:center;line-height:1.3;margin:8px 0 4px}'+
  '.sub{text-align:center;font-size:13px;color:#444;margin:0 0 20px}'+
  'h2{font-size:15px;margin:20px 0 6px;border-bottom:1px solid #ddd;padding-bottom:3px}'+
  'p{margin:8px 0;text-align:justify}ul{margin:6px 0 6px 22px}li{margin:3px 0}'+
  '.parties{background:#faf7f2;border-left:3px solid #f97316;padding:12px 16px}'+
  '.sign{margin-top:38px}.sign .row{margin-top:30px;border-top:1px solid #333;padding-top:6px;font-size:14px}.esign-box{margin-top:9px;padding:9px 11px;border:1px solid #16a34a;background:#f0fdf4;border-radius:6px;font-size:11px;font-family:sans-serif;color:#14532d;line-height:1.55}.esign-box b{color:#15803d}.pend{margin-top:7px;font-size:11px;color:#9a6a00;font-family:sans-serif}'+
  '@media print{body{background:#fff}.bar{display:none}.doc{box-shadow:none;margin:0;max-width:none;padding:0}@page{margin:2cm}}';
 var b=[];
 b.push('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Contrato Corexia - '+nome+'</title><style>'+css+'</style></head><body>');
 b.push('<div class="bar"><button onclick="window.print()">Imprimir / Salvar PDF</button></div><div class="doc">');
 b.push('<div class="logo">COREXIA</div>');
 b.push('<h1>CONTRATO DE LICENCIAMENTO DE SOFTWARE, SERVIÇOS EM NUVEM E PARCERIA COMERCIAL (REVENDA)</h1>');
 b.push('<p class="sub"><b>COREXIA</b> &mdash; Razão Social: PRIME SERVIÇOS LTDA &mdash; CNPJ: 41.901.191/0001-40</p>');
 b.push('<p class="parties">Por este instrumento particular de licenciamento e parceria comercial, de um lado <b>PRIME SERVIÇOS LTDA</b>, inscrita no CNPJ sob o n&ordm; 41.901.191/0001-40, doravante denominada <b>COREXIA</b>; e de outro lado <b>'+nome+'</b>, inscrito(a) no '+docLabel+' sob o n&ordm; '+doc+endereco+', doravante denominado(a) <b>PROVEDOR</b>; tem entre si, na melhor forma de direito, justo e contratado o quanto segue:</p>');
 b.push('<h2>CLÁUSULA 1 &mdash; Objeto</h2><p>A COREXIA concede ao PROVEDOR uma licença não exclusiva de utilização da Plataforma COREXIA, destinada ao gerenciamento de câmeras, monitoramento em nuvem, Inteligência Artificial aplicada ao vídeo, gravação em nuvem, dashboards, usuários, clientes finais e demais funcionalidades contratadas.</p>');
 b.push('<h2>CLÁUSULA 2 &mdash; Licença White Label</h2><p>O PROVEDOR poderá utilizar a infraestrutura COREXIA com as seguintes personalizações:</p><ul><li>Utilizar sua própria marca;</li><li>Dominio próprio;</li><li>Personalizar logo;</li><li>Cores e identidade visual.</li></ul><p>Tudo utilizando a infraestrutura tecnológica da COREXIA.</p>');
 b.push('<h2>CLÁUSULA 3 &mdash; Prazo</h2><p>O presente contrato tem prazo de vigência de 12 (doze) meses, com renovação automatica.</p>');
 b.push('<h2>CLÁUSULA 4 &mdash; Planos e Valores</h2>'+
  '<p><b>COREXIA LOCAL</b></p><ul><li>Licença da plataforma: R$ 797,00/mes (inclui ate 100 câmeras).</li><li>Camera adicional: R$ 5,97/mes.</li></ul>'+
  '<p><b>INTELIGENCIA ARTIFICIAL (IA)</b></p><ul><li>IA Objetos: R$ 27,00 por câmera/mes (incluindo: arma de fogo, faca, toca ninja, linha virtual, invasão, pessoa caida, moto, capacete, celular).</li><li>IA EPI: R$ 15,00 por câmera/mes.</li><li>IA Veiculos: R$ 27,00 por câmera/mes.</li><li>IA Face: R$ 47,00 por câmera/mes.</li><li>IA Fogo e Fumaca: R$ 27,00 por câmera/mes.</li></ul>'+
  '<p><b>COREXIA CLOUD</b></p><ul><li>Transmissão ao vivo: R$ 5,97/mes.</li><li>Gravacao: 1 dia R$ 9,97 &middot; 3 dias R$ 14,97 &middot; 5 dias R$ 20,00 &middot; 7 dias R$ 24,97 &middot; 15 dias R$ 39,97 &middot; 30 dias R$ 69,97 &middot; 60 dias R$ 129,97 &middot; 90 dias R$ 179,97 &middot; 366 dias R$ 597,97 (valores mensais).</li></ul>');
 b.push('<h2>CLÁUSULA 5 &mdash; Da Inteligência Artificial</h2>'+
  '<p>A COREXIA disponibilizará os módulos de IA contratados. O PROVEDOR poderá habilitar ou desabilitar cada IA individualmente para cada câmera cadastrada. A cobrança ocorrerá mensalmente conforme a quantidade de câmeras utilizando cada recurso.</p>'+
  '<p>Os analíticos de IA são de natureza <b>probabilística</b> e constituem ferramenta de apoio a segurança: <b>não garantem</b> a detecção de todos os eventos, podendo gerar alertas falsos ou deixar de alertar, e <b>não substituem</b> a vigilância humana, o alarme monitorado ou serviços de pronta resposta.</p>');
 b.push('<h2>CLÁUSULA 6 &mdash; Do White Label e Relacionamento com o Cliente Final</h2><p>O cliente final não terá vínculo comercial com a COREXIA. A comercialização, cobrança e definição de preços são de inteira responsabilidade do PROVEDOR. A COREXIA fornece exclusivamente a infraestrutura tecnológica.</p>');
 b.push('<h2>CLÁUSULA 7 &mdash; Do Cloud (Gravacao em Nuvem)</h2><p>Caso o PROVEDOR utilize a gravação em nuvem da COREXIA, esta garante a disponibilidade da infraestrutura. No entanto, a COREXIA não se responsabiliza por perdas de gravação causadas por falhas de internet do cliente, desligamento de equipamentos, câmeras offline ou atos de sabotagem.</p>');
 b.push('<h2>CLÁUSULA 8 &mdash; Da Infraestrutura Local</h2><p>Quando for utilizada gravação local, toda a responsabilidade pela manutenção, segurança e operação do servidor e exclusiva do PROVEDOR.</p>');
 b.push('<h2>CLÁUSULA 9 &mdash; Lei Geral de Proteção de Dados (LGPD)</h2>'+
  '<p>As Partes cumprirão a Lei n&ordm; 13.709/2018 (LGPD). As imagens e dados captados pertencem ao PROVEDOR e a seus clientes finais, que atuam como <b>Controladores</b>; a COREXIA atua como <b>Operadora</b>, tratando os dados exclusivamente conforme as instruções das Partes e para a finalidade de segurança patrimonial e vigilância.</p>'+
  '<p>O PROVEDOR e responsável por garantir a base legal e a finalidade lícita da captação (inclusive a sinalização de ambiente monitorado) e por atender as solícitações dos titulares de dados, com apoio da COREXIA. A COREXIA adotara medidas técnicas e organizacionais de segurança, restringirá o acesso ao estritamente necessário e comunicará o PROVEDOR, sem demora injustificada, sobre incidentes de segurança relevantes.</p>'+
  '<p>As gravações serão retidas pelo período do plano contratado por câmera e, apos, poderáo ser eliminadas automaticamente. Encerrado o contrato, os dados serão disponibilizados para exportação por prazo razoável e, em seguida, eliminados, salvo obrigação legal de guarda.</p>');
 b.push('<h2>CLÁUSULA 10 &mdash; Atualizações</h2><p>O PROVEDOR terá direito a atualizacoes automáticas, novas funcionalidades e correções do sistema sem custo adicional.</p>');
 b.push('<h2>CLÁUSULA 11 &mdash; Suporte Técnico e Nível de Serviço (SLA)</h2>'+
  '<p>A COREXIA fornecerá suporte técnico de retaguarda (2&ordm; nível) ao PROVEDOR, cabendo a este o atendimento de 1&ordm; nível aos seus clientes finais, nos canais e horários definidos entre as Partes.</p>'+
  '<p>A COREXIA envidará os melhores esforços para manter a disponibilidade mensal da Plataforma em, no mínimo, 99,0%, apurada excluindo-se manutenções programadas, casos fortuitos ou de força maior e fatores externos. <b>Não integram o SLA</b>, por serem alheios a Plataforma: a conexão de internet do PROVEDOR ou dos clientes finais, a energia elétrica, a rede e o funcionamento fisico das câmeras e os serviços de terceiros (ex.: mensageria/WhatsApp e meios de pagamento).</p>');
 b.push('<h2>CLÁUSULA 12 &mdash; Pagamento</h2><p>O pagamento dos serviços contratados será realizado com periodicidade mensal.</p>');
 b.push('<h2>CLÁUSULA 13 &mdash; Inadimplência</h2><p>Em caso de atraso no pagamento, aplicar-se-ão as seguintes penalidades:</p><ul><li>10 dias de atraso: bloqueio total da plataforma;</li><li>60 dias de atraso: cancelamento definitivo do contrato.</li></ul>');
 b.push('<h2>CLÁUSULA 14 &mdash; Rescisão</h2><p>O contrato possui fidelidade de 12 meses. Em caso de rescisão antecipada por parte do PROVEDOR, incidirá multa equivalente a 30% das mensalidades vincendas.</p>');
 b.push('<h2>CLÁUSULA 15 &mdash; Sigilo</h2><p>O PROVEDOR obriga-se a manter absoluto sigilo sobre as informações técnicas e comerciais da COREXIA. E expressamente proibido copiar, reproduzir, comercializar ou desenvolver solução derivada baseada na arquitetura, funcionalidades ou propriedade intelectual da COREXIA.</p>');
 b.push('<h2>CLÁUSULA 16 &mdash; Não Concorrência Tecnológica e Propriedade Intelectual</h2><p>O PROVEDOR reconhece que a tecnologia da COREXIA e de propriedade exclusiva desta, sendo terminantemente proibido:</p><ul><li>Copiar a plataforma ou partes dela;</li><li>Contratar terceiros para reproduzi-la;</li><li>Utilizar técnicas de engenharia reversa;</li><li>Distribuir versões modificadas do sistema;</li><li>Sublicenciar o software sem a devida e expressa autorização.</li></ul>');
 b.push('<h2>CLÁUSULA 17 &mdash; Limitação de Responsabilidade</h2>'+
  '<p>Salvo dolo, a responsabilidade total da COREXIA por quaisquer perdas relacionadas a este contrato fica limitada ao valor efetivamente pago pelo PROVEDOR a COREXIA nos 3 (tres) meses anteriores ao evento, excluídos lucros cessantes e danos indiretos. A COREXIA não responderá por danos decorrentes de indisponibilidade de internet ou energia, falha ou má instalação das câmeras, uso indevido pelo PROVEDOR ou por seus clientes finais, ou eventos de força maior.</p>');
 b.push('<h2>CLÁUSULA 18 &mdash; Da Assinatura Eletrônica</h2>'+
  '<p>As Partes reconhecem e aceitam expressamente a celebração e a assinatura deste Contrato por meio eletrônico, mediante código de verificação de uso único enviado ao número de WhatsApp indicado pelo PROVEDOR (autenticação em duas etapas), atribuindo-lhe plena validade e eficácia jurídica, nos termos do art. 107 do Código Civil, da MP 2.200-2/2001 (art. 10, § 2º) e da Lei nº 14.063/2020.</p>'+
  '<p>As Partes reconhecem a trilha de auditoria gerada pela Plataforma &mdash; identificador da assinatura, data e hora, número de WhatsApp, endereço IP e resumo criptográfico (SHA-256) do documento &mdash; como prova idônea da autoria, do consentimento e da integridade deste instrumento.</p>');
 b.push('<div class="sign"><p>E, por estarem assim justas e contratadas, as partes assinam o presente instrumento.</p>');
 b.push('<p style="margin-top:20px">Local e Data: '+local+', '+dataTxt+'.</p>');
 b.push('<div class="row"><b>PRIME SERVIÇOS LTDA (COREXIA)</b><br>CNPJ: 41.901.191/0001-40<br><span style="font-size:11px;color:#555;font-family:sans-serif">Licenciante &mdash; aceite registrado pela Plataforma Corexia.</span></div>');
 if(c.status==='assinado'){ b.push('<div class="row"><b>PROVEDOR: '+nome+'</b><br>'+docLabel+': '+doc+'<div class="esign-box"><b>&#10003; ASSINADO ELETRONICAMENTE</b><br>Assinado por '+esc(c.assinado_por_nome||nome)+' ('+docLabel+' '+esc(fmtDoc(c.assinado_por_doc)||doc)+')<br>Código de verificação enviado ao WhatsApp '+esc(c.assinado_por||'-')+', validado em '+esc(c.assinado_em_local||c.assinado_em||'-')+'.<br>IP de origem: '+esc(c.assinado_ip||'-')+' &middot; ID da assinatura: '+esc(c.assinatura_id||'-')+'<br>Resumo do documento (SHA-256): '+esc((c.doc_hash||'').slice(0,48))+'&hellip;</div></div>'); } else { b.push('<div class="row"><b>PROVEDOR: '+nome+'</b><br>'+docLabel+': '+doc+'<div class="pend">Aguardando assinatura eletrônica (código de verificação por WhatsApp).</div></div>'); }
 b.push('</div></div></bo'+'dy></html>');
 return b.join(''); }
</script>
"""


# ---------- corpo da tela de Clientes (admin: provedores/revendas que assinaram o contrato) ----------
_CLIENTES_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
 <input id="q" placeholder="Buscar provedor/revenda..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <button onclick="loadCl()">Atualizar</button></div>
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:-4px 0 14px">Todos os provedores/revendas (assinados via Propostas ou cadastrados manualmente). Clique em <b>editar</b> para ajustar dados / WhatsApp / plano.</p>
<div class="cards">
 <div class="kpi"><div class="k">Provedores</div><div class="v" id="k_tot">-</div></div>
 <div class="kpi"><div class="k">Ativos</div><div class="v" id="k_at" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Bloqueados</div><div class="v" id="k_bl" style="color:var(--bad)">-</div></div></div>
<table><thead><tr><th>Provedor / Revenda</th><th>Plano</th><th>Documento</th><th>Valor/mes</th><th>Status</th><th>Asaas</th><th>Z-API</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="8" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:560px"><h2>Editar cliente</h2><input type="hidden" id="c_id">
 <div class="fld"><label>Nome / Razao social</label><input id="c_nome"></div>
 <div class="two"><div class="fld"><label>E-mail</label><input id="c_email" type="email"></div>
  <div class="fld"><label>Telefone / WhatsApp</label><input id="c_tel"></div></div>
 <div class="two"><div class="fld"><label>Documento</label><input id="c_doc"></div>
  <div class="fld"><label>Valor mensal (R$)</label><input id="c_valor" type="number" step="0.01"></div></div>
 <div class="fld"><label>Plano</label><select id="c_plano"><option value="">- selecione -</option></select></div>
 <div id="c_asaas" style="font-size:12px;color:var(--muted);font-family:var(--mono)"></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvarCl()">Salvar</button></div></div></div>
<div class="ov" id="ovc"><div class="modal" style="max-width:620px"><h2>Credenciais - <span id="kc_nome"></span></h2><input type="hidden" id="kc_id">
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:4px 0 8px">Asaas (cobranca do provedor aos clientes dele)</div>
 <div id="kc_asaas_st" style="color:var(--muted);font-size:13px;margin-bottom:8px"></div>
 <div class="fld"><label>Asaas API Key (cole a chave; nunca sera exibida de volta)</label><input id="kc_asaas" placeholder="$aact_..."></div>
 <div style="margin-bottom:16px"><button class="btn-primary" onclick="salvarAsaasCl()">Validar e salvar Asaas</button></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:6px 0 8px">Z-API (WhatsApp do provedor)</div>
 <div id="kc_zapi_st" style="color:var(--muted);font-size:13px;margin-bottom:8px"></div>
 <label style="display:flex;gap:8px;align-items:center;margin-bottom:10px;cursor:pointer"><input type="checkbox" id="kc_zativa" style="width:auto"> <span>Ativar Z-API PROPRIA (desligado = usa o WhatsApp oficial da Corexia)</span></label>
 <div class="two"><div class="fld"><label>Instance ID</label><input id="kc_zinst"></div><div class="fld"><label>Token</label><input id="kc_ztok" placeholder="cole o token"></div></div>
 <div class="two"><div class="fld"><label>Client-Token</label><input id="kc_zcli" placeholder="cole o client-token"></div><div class="fld"><label>Testar (num WhatsApp)</label><input id="kc_ztest" placeholder="opcional"></div></div>
 <div class="foot"><button onclick="document.getElementById('ovc').classList.remove('open')">Fechar</button><button class="btn-primary" onclick="salvarZapiCl()">Salvar Z-API</button></div></div></div>
<div class="ov" id="ovm"><div class="modal" style="max-width:560px"><h2>Marca (white-label) - <span id="m_nome_cab"></span></h2><input type="hidden" id="m_id">
 <div class="fld"><label>Nome da marca (titulo do painel)</label><input id="m_marca" oninput="mPrev()" placeholder="Ex.: Viggia Seguranca"></div>
 <div class="two">
  <div class="fld"><label>Cor da marca (botoes/destaque)</label><input id="m_cor" type="color" value="#f97316" oninput="mPrev()" style="height:40px;padding:2px;width:100%"></div>
  <div class="fld"><label>Cor do menu (fundo da barra)</label><input id="m_cormenu" type="color" value="#171a21" style="height:40px;padding:2px;width:100%"></div>
 </div>
 <div class="fld"><label>Logo (PNG/JPG ate 2MB)</label><input id="m_logofile" type="file" accept="image/*" onchange="mUpLogo()"><div id="m_logostat" style="font-size:12px;color:var(--muted);margin-top:4px"></div></div>
 <div class="fld"><label>Dominio proprio (opcional)</label><input id="m_dominio" placeholder="ex.: painel.viggia.com.br (vazio = usa o dominio compartilhado)"></div>
 <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Previa: <span id="m_prev" style="padding:3px 12px;border-radius:6px;color:#fff;font-weight:600">marca</span></div>
 <div class="foot"><button onclick="document.getElementById('ovm').classList.remove('open')">Cancelar</button><button class="btn-primary" onclick="salvarMarca()">Salvar marca</button></div></div></div>
<script>
var CLI=[], PLN=[], CRED={}, CUSTOS={}; window.PAGE_INIT=initCl;
function badge(on,txtOn,txtOff){ return '<span class="pill '+(on?'ok':'off')+'">'+(on?txtOn:txtOff)+'</span>'; }
function digs(s){ return (''+(s||'')).replace(/\\D/g,''); }
function fmtDoc(raw){ var d=digs(raw);
 if(d.length===14) return d.slice(0,2)+'.'+d.slice(2,5)+'.'+d.slice(5,8)+'/'+d.slice(8,12)+'-'+d.slice(12,14);
 if(d.length===11) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9,11);
 return raw||''; }
async function initCl(){ try{ PLN=await api('GET','/api/entities/Plano'); }catch(e){ PLN=[]; } loadCl(); }
async function loadCl(){ try{ var all=await api('GET','/api/entities/Provedor?limit=2000');
  CLI=all;  // mostra TODOS os provedores (assinados via proposta + cadastrados manualmente/seed)
  var sts=await Promise.all(CLI.map(function(p){return api('GET','/api/comercial/provedores/'+p.id+'/cred-status').catch(function(){return {}});}));
  CRED={}; CLI.forEach(function(p,i){CRED[p.id]=sts[i]||{};});
  try{ var _cc=await api('GET','/api/comercial/provedores/custos'); CUSTOS=(_cc&&_cc.custos)||{}; }catch(e){ CUSTOS={}; }
  kpisCl(); render(); }catch(e){ msg('Erro: '+e.message); } }
function kpisCl(){ var a=0,b=0; CLI.forEach(function(p){ if((p.status||'ativo')==='bloqueado')b++; else a++; });
 $('k_tot').textContent=CLI.length; $('k_at').textContent=a; $('k_bl').textContent=b; }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=CLI.filter(function(p){ return !q || ((''+(p.nome||'')+' '+(p.document_number||'')+' '+(p.plano_nome||'')).toLowerCase().indexOf(q)>=0); });
 $('rows').innerHTML=arr.map(function(p){ var bloq=(p.status||'ativo')==='bloqueado'; var cr=CRED[p.id]||{};
  var st=bloq?'<span class="pill" style="color:var(--bad)">Bloqueado</span>':'<span class="pill ok">Ativo</span>';
  var ac='<button class="act" style="color:var(--accent)" onclick="sync(\\''+p.id+'\\')">sincronizar Asaas</button>'+
   '<button class="act" style="color:var(--accent)" onclick="abrirCred(\\''+p.id+'\\')">credenciais</button>'+
   '<button class="act" onclick="marca(\\''+p.id+'\\')">marca</button>'+'<button class="act" style="color:var(--ok)" onclick="cobrar(\\''+p.id+'\\')">cobrar</button>'+
   '<button class="act" onclick="editar(\\''+p.id+'\\')">editar</button>'+
   (bloq?'<button class="act" style="color:var(--ok)" onclick="desbloquear(\\''+p.id+'\\')">desbloquear</button>'
        :'<button class="act" style="color:var(--bad)" onclick="bloquear(\\''+p.id+'\\')">bloquear</button>')+
   '<button class="act" onclick="excluir(\\''+p.id+'\\')">excluir</button>';
  return '<tr><td><b>'+esc(p.nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(p.email||'')+'</div></td>'+
   '<td>'+esc(p.plano_nome||'-')+'</td><td>'+esc(fmtDoc(p.document_number)||'-')+'</td>'+
   '<td class="money" title="painel '+brl((CUSTOS[p.id]||{}).painel||0)+' | IA '+brl((CUSTOS[p.id]||{}).ia_total||0)+' | grav '+brl((CUSTOS[p.id]||{}).grav_total||0)+'">'+brl((CUSTOS[p.id]&&CUSTOS[p.id].total!=null)?CUSTOS[p.id].total:p.valor_mensal)+'</td><td>'+st+'</td>'+
   '<td>'+badge(cr.asaas_configurado,'OK '+(cr.asaas_mask||''),'nao')+'</td>'+
   '<td>'+(cr.zapi_ativa?badge(true,'propria',''):'<span class="pill off">Corexia</span>')+'</td>'+
   '<td style="text-align:right;white-space:nowrap">'+ac+'</td></tr>'; }).join('')
   ||'<tr><td colspan="8" class="center">Nenhum provedor/revenda ainda. Assine um contrato na aba Propostas.</td></tr>'; }
function fillPln(sel){ var s=$('c_plano'); if(!s)return;
 s.innerHTML='<option value="">- selecione -</option>'+PLN.map(function(p){return '<option value="'+p.id+'"'+(p.id===sel?' selected':'')+'>'+esc(p.nome)+' ('+brl(p.valor_mensal)+')</option>';}).join(''); }
function editar(id){ var p=CLI.filter(function(x){return x.id===id})[0]; if(!p)return;
 $('c_id').value=p.id; $('c_nome').value=p.nome||''; $('c_email').value=p.email||''; $('c_tel').value=p.telefone||'';
 $('c_doc').value=p.document_number||''; $('c_valor').value=p.valor_mensal!=null?p.valor_mensal:''; fillPln(p.plano_id||'');
 var sub=p.asaas_subscription_id||''; var teste=(!sub||sub.indexOf('TESTE_')===0);
 $('c_asaas').textContent='Asaas: '+(teste?'assinatura ainda nao criada (use Sincronizar Asaas)':('assinatura '+sub));
 $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvarCl(){ var pl=PLN.filter(function(x){return x.id===$('c_plano').value})[0]||{};
 var b={ nome:$('c_nome').value.trim(), email:$('c_email').value.trim(), telefone:$('c_tel').value.trim(),
  document_number:$('c_doc').value.trim(), valor_mensal:parseFloat($('c_valor').value||0)||0,
  plano_id:$('c_plano').value, plano_nome:(pl.nome||'') };
 try{ await api('PUT','/api/entities/Provedor/'+$('c_id').value,b); fecha(); msg('Cliente atualizado.',true); loadCl(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function bloquear(id){ if(!confirm('Bloquear este cliente? A plataforma dele ficara bloqueada.'))return;
 try{ await api('POST','/api/comercial/clientes/'+id+'/bloquear'); msg('Cliente bloqueado.',true); loadCl(); }catch(e){ msg('Erro: '+e.message); } }
async function desbloquear(id){ try{ await api('POST','/api/comercial/clientes/'+id+'/desbloquear'); msg('Cliente desbloqueado.',true); loadCl(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(id){ if(!confirm('Excluir este cliente? (nao cancela a assinatura no Asaas)'))return;
 try{ await api('DELETE','/api/entities/Provedor/'+id); msg('Cliente excluido.',true); loadCl(); }catch(e){ msg('Erro: '+e.message); } }
async function sync(id){ try{ msg('Sincronizando com o Asaas...',true);
  var r=await api('POST','/api/comercial/clientes/'+id+'/sincronizar');
  if(r.modo==='teste'){ msg(r.info,true); }
  else { msg((r.assinatura_criada?'Assinatura criada no Asaas. ':'')+(r.sincronizadas||0)+' fatura(s) sincronizada(s).',true); }
  loadCl(); }catch(e){ msg('Erro ao sincronizar: '+e.message); } }
function abrirCred(id){ var p=CLI.filter(function(x){return x.id===id})[0]; if(!p)return; var cr=CRED[id]||{};
 $('kc_id').value=id; $('kc_nome').textContent=p.nome||''; $('kc_asaas').value='';
 $('kc_asaas_st').textContent=cr.asaas_configurado?('Asaas configurado ('+(cr.asaas_mask||'')+'). Para trocar, cole uma nova chave.'):'Asaas ainda NAO configurado (Nivel 2 - cobranca do provedor aos clientes dele).';
 $('kc_zativa').checked=!!cr.zapi_ativa; $('kc_zinst').value=cr.zapi_instance_id||''; $('kc_ztok').value=''; $('kc_zcli').value=''; $('kc_ztest').value='';
 $('kc_zapi_st').textContent=cr.zapi_configurado?('Z-API '+(cr.zapi_ativa?'ATIVA (propria)':'salva mas desativada')+' '+(cr.zapi_mask||'')):'Z-API propria nao configurada -> usa o WhatsApp oficial da Corexia.';
 $('ovc').classList.add('open'); }
async function salvarAsaasCl(){ var key=$('kc_asaas').value.trim(); if(!key){msg('Cole a chave Asaas');return;}
 try{ var r=await api('POST','/api/comercial/provedores/'+$('kc_id').value+'/asaas',{asaas_api_key:key});
  msg('Asaas validado e salvo'+(r.conta?(' ('+r.conta+')'):'')+'.',true); await credRefresh(); }catch(e){ msg('Erro: '+e.message); } }
async function salvarZapiCl(){ var b={ zapi_ativa:$('kc_zativa').checked, zapi_instance_id:$('kc_zinst').value.trim(),
  zapi_token:$('kc_ztok').value.trim(), zapi_client_token:$('kc_zcli').value.trim(), testar_numero:$('kc_ztest').value.trim() };
 try{ var r=await api('POST','/api/comercial/provedores/'+$('kc_id').value+'/zapi',b);
  msg('Z-API salva.'+(r.teste_enviado===true?' Teste enviado!':(r.teste_enviado===false?' (teste falhou)':'')),true); await credRefresh(); }catch(e){ msg('Erro: '+e.message); } }
async function credRefresh(){ var id=$('kc_id').value; try{ CRED[id]=await api('GET','/api/comercial/provedores/'+id+'/cred-status'); abrirCred(id); render(); }catch(e){} }
function mPrev(){ var el=$('m_prev'); if(!el)return; el.style.background=$('m_cor').value; el.textContent=$('m_marca').value||'marca'; }
async function marca(id){ var c=CLI.filter(function(x){return x.id===id})[0]; if(!c)return;
 $('m_id').value=id; $('m_nome_cab').textContent=c.nome||''; $('m_logostat').textContent='';
 try{ var b=await api('GET','/api/comercial/branding/'+id);
  $('m_marca').value=b.nome_marca||c.nome||''; $('m_cor').value=b.cor||'#f97316'; $('m_cormenu').value=b.cor_menu||'#171a21'; $('m_dominio').value=b.dominio||'';
  if(b.logo)$('m_logostat').textContent='Logo atual: '+b.logo;
 }catch(e){ $('m_marca').value=c.nome||''; }
 mPrev(); $('ovm').classList.add('open'); }
async function mUpLogo(){ var f=$('m_logofile').files[0]; if(!f)return; if(f.size>2*1024*1024){msg('Logo maior que 2MB');return;}
 var rd=new FileReader(); rd.onload=async function(){ try{ var r=await api('POST','/api/comercial/branding/logo',{provedor_id:$('m_id').value,data:rd.result}); $('m_logostat').textContent='Logo enviada: '+r.logo; msg('Logo enviada.',true);}catch(e){msg('Erro na logo: '+e.message);} }; rd.readAsDataURL(f); }
async function salvarMarca(){ var b={ provedor_id:$('m_id').value, nome_marca:$('m_marca').value.trim(), cor:$('m_cor').value, cor_menu:$('m_cormenu').value, dominio:$('m_dominio').value.trim().toLowerCase() };
 try{ await api('POST','/api/comercial/branding/salvar',b); $('ovm').classList.remove('open'); msg('Marca salva. (Dominio proprio: o provedor precisa apontar o DNS dele pro nosso IP)',true); loadCl(); }catch(e){ msg('Erro ao salvar marca: '+e.message); } }
function cobModal(){ var o=document.getElementById('ovcob'); if(o)return o;
 o=document.createElement('div'); o.className='ov'; o.id='ovcob';
 o.innerHTML='<div class="modal" style="max-width:520px"><h2>Gerar cobranca - <span id="cob_nome"></span></h2>'+
  '<div class="fld"><label>Valor (R$)</label><input id="cob_valor" type="number" step="0.01"></div>'+
  '<div class="fld"><label>Tipo</label><select id="cob_tipo"><option value="avulsa">Avulsa (uma vez)</option><option value="assinatura">Assinatura mensal (recorrente)</option></select></div>'+
  '<div class="fld"><label>Forma</label><select id="cob_forma"><option value="PIX">PIX</option><option value="BOLETO">Boleto</option></select></div>'+
  '<div id="cob_msg" class="msg"></div><div id="cob_result" style="display:none;font-size:13px;margin-top:8px"></div>'+
  '<div class="foot"><button onclick="fechaCob()">Fechar</button><button class="btn-primary" id="cob_go" onclick="dispararCobranca()">Gerar cobranca</button></div></div>';
 document.body.appendChild(o); return o; }
function cobrar(id){ var p=CLI.filter(function(x){return x.id===id})[0]; if(!p)return; cobModal();
 window.__cobpid=id; $('cob_nome').textContent=p.nome||'';
 var cu=(CUSTOS[id]||{}); $('cob_valor').value=(cu.total!=null?cu.total:(p.valor_mensal||0));
 $('cob_tipo').value='avulsa'; $('cob_forma').value='PIX';
 $('cob_msg').className='msg'; $('cob_msg').textContent=''; $('cob_result').style.display='none'; $('cob_result').innerHTML='';
 document.getElementById('ovcob').classList.add('open'); }
function fechaCob(){ var o=document.getElementById('ovcob'); if(o)o.classList.remove('open'); }
function cobMsg(t,ok){ var m=$('cob_msg'); if(m){ m.textContent=t; m.className='msg '+(ok?'ok':'err'); } }
async function dispararCobranca(){
 var id=window.__cobpid, nome=$('cob_nome').textContent, valor=parseFloat($('cob_valor').value||0)||0, tipo=$('cob_tipo').value, forma=$('cob_forma').value;
 if(valor<=0){ cobMsg('Informe um valor maior que zero.'); return; }
 if(!confirm('Confirma gerar cobranca REAL de R$ '+valor.toFixed(2)+' ('+tipo+' / '+forma+') para '+nome+'?')) return;
 var btn=$('cob_go'); btn.disabled=true; cobMsg('Gerando no Asaas...',true);
 try{
  var r=await api('POST','/api/comercial/provedores/'+id+'/cobrar',{valor:valor,tipo:tipo,forma:forma});
  cobMsg('Cobranca criada!',true);
  var h='<b>Status:</b> '+esc(r.status||'-')+' &middot; <b>R$ '+(r.valor||valor)+'</b>';
  if(r.invoiceUrl) h+='<div style="margin-top:6px"><a href="'+esc(r.invoiceUrl)+'" target="_blank" style="color:var(--accent);font-weight:600">Abrir fatura / link de pagamento</a></div>';
  if(r.pix) h+='<div style="margin-top:8px"><b>PIX copia-e-cola:</b><br><textarea readonly onclick="this.select()" style="width:100%;height:60px;font-family:monospace;font-size:11px;background:var(--surface2);color:var(--ink);border:1px solid var(--border);border-radius:8px;padding:6px;margin-top:4px">'+esc(r.pix)+'</textarea></div>';
  $('cob_result').innerHTML=h; $('cob_result').style.display='block'; loadCl();
 }catch(e){ cobMsg('Erro: '+e.message); }
 btn.disabled=false;
}
</script>
"""


# ---------- corpo da tela de Analiticos por Camera (config que o detector le) ----------
_ANALITICOS_BODY = '\n<script src="/hls.min.js"></script>\n<div class="anx-head">\n <p class="anx-sub">Configure quais analíticos de IA rodam em cada câmera e em quais horários. <b>Sem config = não roda</b> (câmera fica off). Muda vale em ~2 min.</p>\n <div class="anx-actions">\n  <input id="q" placeholder="Buscar câmera ou cliente..." oninput="deb()" class="anx-search">\n  <button onclick="loadA()">Atualizar</button>\n </div>\n</div>\n<div id="msg" class="msg"></div>\n<div class="anx-toolbar">\n <div class="anx-filters">\n  <span class="anx-chip2 on" data-f="all" onclick="setF(this)">Todas</span>\n  <span class="anx-chip2" data-f="online" onclick="setF(this)"><i class="anx-d on"></i>Online</span>\n  <span class="anx-chip2" data-f="offline" onclick="setF(this)"><i class="anx-d off"></i>Offline</span>\n  <span class="anx-chip2" data-f="yes" onclick="setF(this)">Configuradas</span>\n  <span class="anx-chip2" data-f="no" onclick="setF(this)">Sem config</span>\n </div>\n <div class="anx-right">\n  <select id="cli" onchange="render()" class="anx-select"><option value="">Todos os clientes</option></select>\n  <span id="cnt" class="anx-cnt"></span>\n </div>\n</div>\n<div id="rows" class="anx-grid"><div class="anx-loading">carregando...</div></div>\n\n<div class="ov" id="ov"><div class="modal" style="max-width:640px"><h2>Analíticos - <span id="a_nome"></span></h2><input type="hidden" id="a_id">\n <label style="display:flex;gap:8px;align-items:center;margin-bottom:12px;cursor:pointer"><input type="checkbox" id="a_ativo" style="width:auto" checked> <span>Ativo (desmarcado = nada roda nesta câmera)</span></label>\n <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:4px 0 8px">Analíticos</div>\n <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;margin-bottom:8px">\n  <label class="ck"><input type="checkbox" id="a_fogo"> Fogo / Fumaça</label>\n  <label class="ck"><input type="checkbox" id="a_arma_fogo"> Arma de fogo</label>\n  <label class="ck"><input type="checkbox" id="a_arma_branca"> Arma branca (faca)</label>\n  <label class="ck"><input type="checkbox" id="a_placa"> Placa (LPR)</label>\n  <label class="ck"><input type="checkbox" id="a_pessoa"> Pessoa</label>\n  <label class="ck"><input type="checkbox" id="a_veiculo"> Veículo</label>\n  <label class="ck"><input type="checkbox" id="a_animal"> Animal</label>\n  <label class="ck"><input type="checkbox" id="a_epi"> EPI (sem capacete/luva/óculos/máscara/calçado)</label>\n  <label class="ck"><input type="checkbox" id="a_intruso"> Zona de intrusão (desenhe em "zonas")</label>\n  <label class="ck"><input type="checkbox" id="a_linha"> Linha virtual (desenhe em "zonas")</label>\n  <label class="ck"><input type="checkbox" id="a_heatmap"> Mapa de calor (área opcional em "zonas")</label>\n  <label class="ck"><input type="checkbox" id="a_toca_ninja"> Toca ninja / rosto coberto (Gemini)</label>\n  <label class="ck"><input type="checkbox" id="a_piscina"> Piscina/afogamento - AUXÍLIO (desenhe a água em "zonas")</label>\n </div>\n <div id="a_placa_warn" style="display:none;color:var(--muted);font-size:12px;margin-bottom:8px">Obs: Placa só dispara em câmera marcada como "de entrada" (ia_placa) no cadastro.</div>\n <label style="display:flex;gap:8px;align-items:center;margin:10px 0;cursor:pointer"><input type="checkbox" id="a_sched" style="width:auto" onchange="toggleSched()"> <span>Restringir por horário (senão roda 24h)</span></label>\n <div id="schedbox" style="display:none">\n  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px" id="diasbox">\n   <label class="ck"><input type="checkbox" id="a_d1" checked> Seg</label><label class="ck"><input type="checkbox" id="a_d2" checked> Ter</label>\n   <label class="ck"><input type="checkbox" id="a_d3" checked> Qua</label><label class="ck"><input type="checkbox" id="a_d4" checked> Qui</label>\n   <label class="ck"><input type="checkbox" id="a_d5" checked> Sex</label><label class="ck"><input type="checkbox" id="a_d6"> Sáb</label>\n   <label class="ck"><input type="checkbox" id="a_d7"> Dom</label></div>\n  <div class="two"><div class="fld"><label>Hora início</label><input id="a_ini" type="time" value="08:00"></div>\n   <div class="fld"><label>Hora fim</label><input id="a_fim" type="time" value="18:00"></div></div>\n </div>\n <div class="foot"><button onclick="fecha()">Cancelar</button><button id="a_limpar" onclick="limpar()" style="color:var(--bad)">Limpar config</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>\n\n<div class="ov" id="ovz"><div class="modal" style="max-width:820px"><h2>Zonas - <span id="z_nome"></span></h2><input type="hidden" id="z_id">\n <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">\n  <button id="z_mzona" class="btn-primary" onclick="zMode(\'zona\')">Zona (polígono)</button>\n  <button id="z_mlinha" onclick="zMode(\'linha\')">Linha</button>\n  <button id="z_mheat" onclick="zMode(\'heatmap\')">Área (mapa calor)</button>\n  <button id="z_magua" onclick="zMode(\'agua\')">Água (piscina)</button>\n  <button onclick="zNova()">Finalizar forma</button>\n  <button onclick="zUndo()">Desfazer ponto</button>\n  <button onclick="zClear()" style="color:var(--bad)">Limpar tudo</button></div>\n <div style="color:var(--muted);font-size:12px;margin-bottom:8px">Clique na imagem p/ marcar os pontos. <b>Zona</b>: 3+ pontos e "Finalizar forma". <b>Linha</b>: 2 pontos (fecha sozinha). Dispara quando uma <b>pessoa</b> entra na zona / pisa na linha.</div>\n <canvas id="z_cv" width="780" height="439" style="max-width:100%;border:1px solid var(--border);border-radius:10px;cursor:crosshair;display:block"></canvas>\n <div id="z_list" style="font-size:12px;color:var(--muted);margin-top:8px"></div>\n <div class="foot"><button onclick="zFecha()">Cancelar</button><button class="btn-primary" onclick="zSalvar()">Salvar zonas</button></div></div></div>\n\n<div class="ov" id="ovv"><div class="modal anx-vmodal" style="max-width:1000px;padding:0">\n <div class="anx-vhd"><b id="v_nome"></b><button onclick="vFecha()" class="anx-vx" aria-label="Fechar">×</button></div>\n <div class="anx-vbody" id="v_body"></div></div></div>\n\n<style>\n.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}\n.anx-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:14px}\n.anx-sub{color:var(--muted);font-size:13.5px;max-width:640px;margin:0;line-height:1.5}\n.anx-actions{display:flex;gap:8px;align-items:center}\n.anx-search{background:var(--surface2);border:1px solid var(--border);border-radius:calc(var(--radius) - 4px);color:var(--ink);padding:10px 13px;font-size:14px;min-width:230px}\n.anx-search:focus{outline:none;border-color:var(--accent)}\n.anx-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}\n.anx-filters{display:flex;gap:7px;flex-wrap:wrap}\n.anx-chip2{font-size:12px;padding:6px 13px;border-radius:999px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;user-select:none;transition:all .14s}\n.anx-chip2.on{background:linear-gradient(180deg,rgba(249,115,22,.18),rgba(249,115,22,.08));border-color:var(--accent);color:#ffd6b0}\n.anx-cnt{color:var(--muted);font-size:12px;font-family:var(--mono)}\n.anx-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}\n.anx-select{background:var(--surface2);border:1px solid var(--border);border-radius:calc(var(--radius) - 4px);color:var(--ink);padding:9px 12px;font-size:13px;max-width:230px;cursor:pointer}\n.anx-select:focus{outline:none;border-color:var(--accent)}\n.anx-d{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--muted);vertical-align:middle;margin-right:6px}\n.anx-d.on{background:#34d399}.anx-d.off{background:#f87171}\n.anx-stat{position:absolute;top:8px;right:8px;font-size:10px;font-weight:700;padding:3px 8px 3px 7px;border-radius:999px;background:rgba(10,12,16,.82);border:1px solid var(--border);color:var(--muted);backdrop-filter:blur(4px);display:flex;align-items:center;gap:5px}\n.anx-stat::before{content:\'\';width:6px;height:6px;border-radius:50%;background:currentColor;display:inline-block}\n.anx-stat.on{color:#34d399;border-color:rgba(52,211,153,.5)}\n.anx-stat.off{color:#f87171;border-color:rgba(248,113,113,.5)}\n.anx-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}\n.anx-loading{grid-column:1/-1;text-align:center;color:var(--muted);padding:50px}\n.anx-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;display:flex;flex-direction:column;transition:border-color .15s,transform .1s}\n.anx-card:hover{border-color:rgba(249,115,22,.4);transform:translateY(-2px)}\n.anx-thumb{position:relative;padding-top:56.25%;background:#0b0d12;cursor:pointer}\n.anx-play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:34px;color:#fff;background:rgba(0,0,0,.22);opacity:0;transition:opacity .15s;text-shadow:0 2px 10px rgba(0,0,0,.7);pointer-events:none}\n.anx-thumb:hover .anx-play{opacity:1}\n.anx-vmodal{width:100%;overflow:hidden}\n.anx-vhd{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 18px;border-bottom:1px solid var(--border)}\n.anx-vhd b{font-size:15px}\n.anx-vx{background:none;border:none;color:var(--muted);font-size:24px;cursor:pointer;line-height:1;padding:0 4px}\n.anx-vbody{position:relative;width:100%;aspect-ratio:16/9;background:#000}\n.anx-vbody iframe{position:absolute;inset:0;width:100%;height:100%;border:0}\n.anx-vempty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px;padding:20px;text-align:center}\n.anx-thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}\n.anx-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}\n.anx-badge{position:absolute;top:8px;left:8px;font-size:10px;font-weight:700;padding:3px 9px;border-radius:999px;background:rgba(10,12,16,.8);border:1px solid var(--border);color:var(--muted);backdrop-filter:blur(4px)}\n.anx-badge.ok{color:var(--ok);border-color:rgba(52,211,153,.5)}\n.anx-body{padding:11px 13px 13px;display:flex;flex-direction:column;gap:7px;flex:1}\n.anx-name{font-weight:700;font-size:14px;line-height:1.25;display:flex;align-items:center;gap:6px;flex-wrap:wrap}\n.anx-cli{color:var(--muted);font-size:12px;margin-top:-3px}\n.anx-tag{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:2px 6px;border-radius:5px;background:var(--surface2);border:1px solid var(--border);color:var(--muted)}\n.anx-chips{display:flex;flex-wrap:wrap;gap:4px;min-height:4px}\n.anx-chip{font-size:10px;padding:2px 7px}\n.anx-win{color:var(--muted);font-size:11px;font-family:var(--mono);min-height:2px}\n.anx-muted{color:var(--muted);font-size:12px}\n.anx-btns{display:flex;gap:6px;margin-top:auto;padding-top:4px}\n.anx-mini{font-size:12px;padding:7px 10px;border-radius:calc(var(--radius) - 5px);flex:1}\n.anx-del{color:var(--bad);flex:none;padding:7px 9px}\n@media(max-width:640px){.anx-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}.anx-search{min-width:150px}}\n</style>\n<script>\nvar CAMS=[], FILT=\'all\', _dt=null, statusMap={}, _sweepRun=false, _sweepQ=[]; window.PAGE_INIT=loadA;\nfunction deb(){ if(_dt)clearTimeout(_dt); _dt=setTimeout(render,180); }\nvar VALS=["fogo","arma_fogo","arma_branca","placa","pessoa","veiculo","animal","epi","intruso","linha","heatmap","toca_ninja","piscina"];\nvar ROTULO={fogo:"Fogo",arma_fogo:"Arma fogo",arma_branca:"Arma branca",faca:"Arma branca",placa:"Placa",pessoa:"Pessoa",veiculo:"Veículo",animal:"Animal",epi:"EPI",arma:"Arma",movimento:"Movimento",intruso:"Zona intrusão",linha:"Linha virtual",heatmap:"Mapa calor",toca_ninja:"Toca ninja",piscina:"Piscina",aglomeracao:"Aglomeração"};\nvar DIAS={1:"Seg",2:"Ter",3:"Qua",4:"Qui",5:"Sex",6:"Sáb",7:"Dom"};\nfunction ativosDe(cfg){ if(!cfg) return []; var s={};\n (cfg.analiticos_padrao||[]).forEach(function(a){s[a]=1});\n (cfg.horarios||[]).forEach(function(h){(h.analiticos||[]).forEach(function(a){s[a]=1})});\n return Object.keys(s); }\nfunction janelaDe(cfg){ if(!cfg||!(cfg.horarios||[]).length) return \'24h\';\n var h=cfg.horarios[0]; var ds=(h.dias||[]).map(function(d){return DIAS[d]||d}).join(\',\');\n return (ds||\'todos\')+\' \'+(h.hora_inicio||\'00:00\')+\'-\'+(h.hora_fim||\'23:59\'); }\nfunction setF(el){ document.querySelectorAll(\'.anx-chip2\').forEach(function(c){c.classList.remove(\'on\');}); el.classList.add(\'on\'); FILT=el.getAttribute(\'data-f\'); applyFilter(); }\nasync function loadA(){ try{ CAMS=await api(\'GET\',\'/api/comercial/analiticos/cameras\'); fillClientes(); render(); loadStatus(); }catch(e){ msg(\'Erro: \'+e.message); } }\nfunction loadStatus(){ api(\'GET\',\'/api/comercial/analiticos/status\').then(function(st){ CAMS.forEach(function(c){ if(st[c.id]!==undefined) cxStat(c.id, st[c.id]?1:0); }); }).catch(function(){}); }\nfunction fillClientes(){ var set={}; CAMS.forEach(function(c){ if(c.cliente_nome)set[c.cliente_nome]=1; });\n var opts=Object.keys(set).sort(function(a,b){return a.localeCompare(b,\'pt\');}); var sel=$(\'cli\'); var cur=sel.value;\n sel.innerHTML=\'<option value="">Todos os clientes (\'+CAMS.length+\')</option>\'+opts.map(function(o){return \'<option value="\'+esc(o)+\'">\'+esc(o)+\'</option>\';}).join(\'\'); sel.value=cur; }\nfunction cxStat(id,ok){ var st=ok?\'online\':\'offline\'; if(statusMap[id]===st)return; statusMap[id]=st;\n var el=document.getElementById(\'stat-\'+id); if(el){ el.className=\'anx-stat \'+(ok?\'on\':\'off\'); el.textContent=ok?\'online\':\'offline\'; }\n if(FILT===\'online\'||FILT===\'offline\') applyFilter(); }\nfunction sweep(arr){ arr.forEach(function(c){ if(!statusMap[c.id] && _sweepQ.indexOf(c)<0) _sweepQ.push(c); }); if(!_sweepRun) runSweep(); }\nfunction runSweep(){ _sweepRun=true; var active=0, MAX=8;\n function nxt(){ if(!_sweepQ.length){ if(active===0)_sweepRun=false; return; }\n   while(active<MAX && _sweepQ.length){ var c=_sweepQ.shift(); if(statusMap[c.id]){ continue; } active++;\n     (function(c){ var u=(c.stream_url||\'\');\n       if(u.indexOf(\'.m3u8\')<0){ active--; cxStat(c.id,0); nxt(); return; }\n       fetch(u,{cache:\'no-store\'}).then(function(r){ return r.ok?r.text():\'\'; }).then(function(t){ active--; cxStat(c.id, t.indexOf(\'.ts\')>=0?1:0); nxt(); }).catch(function(){ active--; cxStat(c.id,0); nxt(); });\n     })(c); } }\n nxt(); }\nfunction applyFilter(){ var shown=0;\n document.querySelectorAll(\'.anx-card\').forEach(function(card){\n   var id=card.getAttribute(\'data-id\'), cfg=card.getAttribute(\'data-cfg\')===\'1\', st=statusMap[id], show=true;\n   if(FILT===\'no\') show=!cfg; else if(FILT===\'yes\') show=cfg;\n   else if(FILT===\'online\') show=(st===\'online\'); else if(FILT===\'offline\') show=(st===\'offline\');\n   card.style.display=show?\'\':\'none\'; if(show)shown++; });\n var nconf=CAMS.filter(function(c){return c.config}).length;\n $(\'cnt\').textContent=nconf+\' de \'+CAMS.length+\' configuradas · \'+shown+\' exibidas\'; }\nfunction render(){ var q=($(\'q\').value||\'\').toLowerCase(); var cli=($(\'cli\')?$(\'cli\').value:\'\');\n var arr=CAMS.filter(function(c){\n   if(cli && (c.cliente_nome||\'\')!==cli) return false;\n   return !q || (c.nome||\'\').toLowerCase().indexOf(q)>=0 || (c.cliente_nome||\'\').toLowerCase().indexOf(q)>=0; });\n var enc=encodeURIComponent(TOKEN);\n $(\'rows\').innerHTML=arr.map(function(c){ var cfg=c.config; var ats=ativosDe(cfg);\n  var st,stcls;\n  if(!cfg){ st=\'Sem config\'; stcls=\'\'; } else if(!cfg.ativo){ st=\'Inativo\'; stcls=\'\'; } else { st=\'Ativo\'; stcls=\'ok\'; }\n  var chips=(cfg&&ats.length)?ats.map(pill).join(\'\'):(\'<span class="anx-muted">\'+(cfg?\'(nenhum)\':\'sem analítico\')+\'</span>\');\n  var cur=statusMap[c.id]; var scls=(cur===\'online\'?\'on\':(cur===\'offline\'?\'off\':\'\')); var stxt=(cur||\'...\');\n  return \'<div class="anx-card" data-id="\'+c.id+\'" data-cfg="\'+(cfg?1:0)+\'">\'+\n   \'<div class="anx-thumb" onclick="viewCam(\\\'\'+c.id+\'\\\')" title="Ver ao vivo"><div class="anx-ph">sem imagem</div>\'+\n     \'<img src="/camthumb/\'+c.id+\'?t=\'+enc+\'" loading="lazy" onerror="this.style.display=\\\'none\\\'">\'+\n     \'<div class="anx-play">&#9654;</div>\'+\n     \'<span class="anx-badge \'+stcls+\'">\'+st+\'</span>\'+\n     \'<span class="anx-stat \'+scls+\'" id="stat-\'+c.id+\'">\'+stxt+\'</span></div>\'+\n   \'<div class="anx-body">\'+\n     \'<div class="anx-name">\'+esc(c.nome||\'-\')+(c.ia_placa?\' <span class="anx-tag">entrada</span>\':\'\')+\'</div>\'+\n     (c.cliente_nome?\'<div class="anx-cli">\'+esc(c.cliente_nome)+\'</div>\':\'\')+\n     \'<div class="anx-chips">\'+chips+\'</div>\'+\n     \'<div class="anx-win">\'+(cfg?(\'🕓 \'+esc(janelaDe(cfg))):\'\')+\'</div>\'+\n     \'<div class="anx-btns">\'+\n       \'<button class="btn-primary anx-mini" onclick="conf(\\\'\'+c.id+\'\\\')">Configurar</button>\'+\n       \'<button class="anx-mini" onclick="zonas(\\\'\'+c.id+\'\\\')">Zonas</button>\'+\n       (cfg?\'<button class="anx-mini anx-del" onclick="limparId(\\\'\'+c.id+\'\\\')">✕</button>\':\'\')+\n     \'</div>\'+\n   \'</div></div>\'; }).join(\'\') || \'<div class="anx-loading">Nenhuma câmera</div>\';\n applyFilter(); }\nvar _cxHls=null;\nfunction _destroyHls(){ try{ if(_cxHls){_cxHls.destroy(); _cxHls=null;} }catch(e){} }\nfunction viewCam(id){ var c=CAMS.filter(function(x){return String(x.id)===String(id)})[0]; if(!c)return;\n $(\'v_nome\').textContent=(c.nome||\'Câmera\')+\' — ao vivo\';\n var url=(c.stream_url||\'\').trim(); var em=(c.embed_url||\'\').trim(); var live=((url.indexOf(\'.m3u8\')>=0)||em)?(\'/api/camlive/\'+c.id+\'/index.m3u8?t=\'+encodeURIComponent(TOKEN)):\'\';\n var body=$(\'v_body\'); _destroyHls(); body.innerHTML=\'\';\n $(\'ovv\').classList.add(\'open\');\n if(live){\n   var v=document.createElement(\'video\'); v.autoplay=true; v.muted=true; v.setAttribute(\'playsinline\',\'\'); v.controls=true;\n   v.style.cssText=\'position:absolute;inset:0;width:100%;height:100%;background:#000;object-fit:contain\';\n   body.appendChild(v);\n   var ld=document.createElement(\'div\'); ld.className=\'anx-vempty\'; ld.id=\'v_load\'; ld.textContent=\'Conectando ao vivo...\'; body.appendChild(ld);\n   function ok(){ var l=document.getElementById(\'v_load\'); if(l)l.remove(); }\n   function fail(msg){ _destroyHls(); body.innerHTML=\'<div class="anx-vempty">\'+msg+(em?\' <a href="#" style="color:var(--accent)" onclick="_viaEmbed();return false">tentar player alternativo</a>\':\'\')+\'</div>\'; }\n   window._viaEmbed=function(){ _destroyHls(); body.innerHTML=\'<iframe src="\'+esc(em)+\'" allow="autoplay; fullscreen" allowfullscreen frameborder="0"></iframe>\'; };\n   if(v.canPlayType(\'application/vnd.apple.mpegurl\')){ v.src=live+\'?t=\'+encodeURIComponent(TOKEN); v.addEventListener(\'loadeddata\',ok); v.play().catch(function(){}); }\n   else if(window.Hls && window.Hls.isSupported()){\n     _cxHls=new window.Hls({liveSyncDurationCount:1, liveMaxLatencyDurationCount:10, maxBufferLength:10, backBufferLength:10, manifestLoadingTimeOut:20000, fragLoadingTimeOut:25000, xhrSetup:function(xhr){ xhr.setRequestHeader(\'Authorization\',\'Bearer \'+TOKEN); }});\n     _cxHls.loadSource(live); _cxHls.attachMedia(v);\n     _cxHls.on(window.Hls.Events.MANIFEST_PARSED,function(){ v.play().catch(function(){}); });\n     _cxHls.on(window.Hls.Events.FRAG_BUFFERED, ok);\n     _cxHls.on(window.Hls.Events.ERROR,function(ev,d){ if(d && d.fatal){ if(d.type===\'networkError\'){ try{_cxHls.startLoad();}catch(e){} } else if(d.type===\'mediaError\'){ try{_cxHls.recoverMediaError();}catch(e){} } else { fail(\'Falha ao carregar o vídeo.\'); } } });\n   } else if(em){ window._viaEmbed(); }\n   else { fail(\'Navegador sem suporte a HLS.\'); }\n } else if(em){ body.innerHTML=\'<iframe src="\'+esc(em)+\'" allow="autoplay; fullscreen" allowfullscreen frameborder="0"></iframe>\'; }\n else { body.innerHTML=\'<div class="anx-vempty">Esta câmera não tem stream ao vivo cadastrado.</div>\'; }\n}\nfunction vFecha(){ $(\'ovv\').classList.remove(\'open\'); _destroyHls(); $(\'v_body\').innerHTML=\'\'; }\nfunction pill(a){ return \'<span class="pill ok anx-chip">\'+esc(ROTULO[a]||a)+\'</span>\'; }\nfunction toggleSched(){ $(\'schedbox\').style.display=$(\'a_sched\').checked?\'block\':\'none\'; }\nfunction conf(id){ var c=CAMS.filter(function(x){return String(x.id)===String(id)})[0]; if(!c)return; var cfg=c.config;\n $(\'a_id\').value=id; $(\'a_nome\').textContent=c.nome||\'\'; $(\'a_placa_warn\').style.display=c.ia_placa?\'none\':\'block\';\n $(\'a_ativo\').checked=cfg?!!cfg.ativo:true;\n var ats=ativosDe(cfg); var set={}; ats.forEach(function(a){set[a]=1});\n if(set.arma){set.arma_fogo=1;set.arma_branca=1;} if(set.faca)set.arma_branca=1;\n VALS.forEach(function(v){ $(\'a_\'+v).checked=!!set[v]; });\n var hs=(cfg&&cfg.horarios||[]); var sched=hs.length>0; $(\'a_sched\').checked=sched; toggleSched();\n if(sched){ var h=hs[0]; [1,2,3,4,5,6,7].forEach(function(d){$(\'a_d\'+d).checked=(h.dias||[]).indexOf(d)>=0;});\n  $(\'a_ini\').value=h.hora_inicio||\'08:00\'; $(\'a_fim\').value=h.hora_fim||\'18:00\'; }\n $(\'a_limpar\').style.display=cfg?\'inline-block\':\'none\';\n $(\'ov\').classList.add(\'open\'); }\nfunction fecha(){ $(\'ov\').classList.remove(\'open\'); }\nasync function salvar(){ var c=CAMS.filter(function(x){return String(x.id)===$(\'a_id\').value})[0]; if(!c)return;\n var ana=VALS.filter(function(v){return $(\'a_\'+v).checked});\n var body={ camera_id:c.id, camera_nome:c.nome||\'\', ativo:$(\'a_ativo\').checked, zonas_intrusao:(c.config&&c.config.zonas_intrusao)||[] };\n if($(\'a_sched\').checked){ var dias=[1,2,3,4,5,6,7].filter(function(d){return $(\'a_d\'+d).checked}); if(!dias.length)dias=[1,2,3,4,5,6,7];\n  body.horarios=[{label:\'Personalizado\',dias:dias,hora_inicio:$(\'a_ini\').value||\'00:00\',hora_fim:$(\'a_fim\').value||\'23:59\',analiticos:ana}]; body.analiticos_padrao=[]; }\n else { body.horarios=[]; body.analiticos_padrao=ana; }\n try{ await api(\'POST\',\'/api/comercial/analiticos/salvar\',body); fecha(); msg(\'Config salva. Vale em ~2 min.\',true); loadA(); }catch(e){ msg(\'Erro ao salvar: \'+e.message); } }\nasync function limpar(){ await limparId($(\'a_id\').value); fecha(); }\nasync function limparId(id){ if(!confirm(\'Limpar a config desta câmera? (ela fica off)\'))return;\n try{ await api(\'POST\',\'/api/comercial/analiticos/limpar\',{camera_id:id}); msg(\'Config removida.\',true); loadA(); }catch(e){ msg(\'Erro: \'+e.message); } }\n\n/* ---- editor de zonas / linha (canvas sobre o snapshot) ---- */\nvar Z_SHAPES=[], Z_CUR=[], Z_MODE=\'zona\', Z_CAM=null, Z_IMG=null;\nfunction zMode(m){ Z_MODE=m; var mp={zona:\'z_mzona\',linha:\'z_mlinha\',heatmap:\'z_mheat\',agua:\'z_magua\'}; for(var k in mp){var b=document.getElementById(mp[k]); if(b)b.className=(m===k?\'btn-primary\':\'\');} }\nfunction zonas(id){ var c=CAMS.filter(function(x){return String(x.id)===String(id)})[0]; if(!c)return; Z_CAM=c;\n $(\'z_id\').value=id; $(\'z_nome\').textContent=c.nome||\'\';\n Z_SHAPES=(((c.config||{}).zonas_intrusao)||[]).map(function(s){return {tipo:s.tipo||\'zona\',nome:s.nome||\'\',pontos:(s.pontos||[]).slice()};});\n Z_CUR=[]; zMode(\'zona\'); Z_IMG=null; $(\'ovz\').classList.add(\'open\');\n var cv=$(\'z_cv\'); if(!cv._bound){ cv._bound=1; cv.addEventListener(\'click\',function(e){ var r=cv.getBoundingClientRect();\n   var nx=(e.clientX-r.left)/r.width, ny=(e.clientY-r.top)/r.height; Z_CUR.push([+nx.toFixed(4),+ny.toFixed(4)]);\n   if(Z_MODE===\'linha\'&&Z_CUR.length>=2){ Z_SHAPES.push({tipo:\'linha\',nome:\'Linha \'+(Z_SHAPES.length+1),pontos:Z_CUR.slice(0,2)}); Z_CUR=[]; }\n   zDraw(); }); }\n zDraw();\n fetch(\'/camthumb/\'+id+\'?t=\'+encodeURIComponent(TOKEN),{headers:{\'Authorization\':\'Bearer \'+TOKEN}}).then(function(r){ if(!r.ok)throw 0; return r.blob(); })\n  .then(function(b){ var im=new Image(); im.onload=function(){ Z_IMG=im; var cv=$(\'z_cv\');\n    var w=Math.min(im.naturalWidth||780,780); cv.width=w; cv.height=Math.round(w*((im.naturalHeight||439)/(im.naturalWidth||780))); zDraw(); };\n    im.src=URL.createObjectURL(b); })\n  .catch(function(){ Z_IMG=null; zDraw(); msg(\'Sem foto da câmera (offline) — dá p/ desenhar mesmo assim.\',true); });\n}\nfunction zDraw(){ var cv=$(\'z_cv\'); var ctx=cv.getContext(\'2d\'); ctx.clearRect(0,0,cv.width,cv.height);\n if(Z_IMG){ try{ctx.drawImage(Z_IMG,0,0,cv.width,cv.height);}catch(e){} } else { ctx.fillStyle=\'#111\'; ctx.fillRect(0,0,cv.width,cv.height); ctx.fillStyle=\'#888\'; ctx.fillText(\'(sem imagem)\',16,24); }\n function poly(pts,close,color){ if(!pts.length)return; ctx.strokeStyle=color; ctx.fillStyle=color; ctx.lineWidth=2; ctx.beginPath();\n   pts.forEach(function(p,i){ var x=p[0]*cv.width,y=p[1]*cv.height; if(i===0)ctx.moveTo(x,y); else ctx.lineTo(x,y); }); if(close)ctx.closePath(); ctx.stroke();\n   pts.forEach(function(p){ ctx.beginPath(); ctx.arc(p[0]*cv.width,p[1]*cv.height,4,0,7); ctx.fill(); }); }\n Z_SHAPES.forEach(function(s){ poly(s.pontos, s.tipo!==\'linha\', s.tipo===\'linha\'?\'#34d399\':(s.tipo===\'heatmap\'?\'#38bdf8\':(s.tipo===\'agua\'?\'#3b82f6\':\'#f97316\'))); });\n poly(Z_CUR,false,\'#ffffff\');\n var el=$(\'z_list\'); el.innerHTML=Z_SHAPES.map(function(s,i){ return \'<div>\'+(s.tipo===\'linha\'?\'Linha\':\'Zona\')+\' "\'+esc(s.nome)+\'" (\'+s.pontos.length+\' pts) <a href="#" onclick="zDel(\'+i+\');return false" style="color:var(--bad)">remover</a></div>\'; }).join(\'\')+(Z_CUR.length?\'<div style="color:#fff">em edição: \'+Z_CUR.length+\' ponto(s)</div>\':\'\');\n}\nfunction zUndo(){ Z_CUR.pop(); zDraw(); }\nfunction zNova(){ var poly=(Z_MODE!==\'linha\');\n if(poly&&Z_CUR.length>=3){ Z_SHAPES.push({tipo:Z_MODE,nome:(Z_MODE===\'heatmap\'?\'Area \':(Z_MODE===\'agua\'?\'Agua \':\'Zona \'))+(Z_SHAPES.length+1),pontos:Z_CUR.slice()}); Z_CUR=[]; }\n else if(!poly&&Z_CUR.length>=2){ Z_SHAPES.push({tipo:\'linha\',nome:\'Linha \'+(Z_SHAPES.length+1),pontos:Z_CUR.slice(0,2)}); Z_CUR=[]; }\n else { msg(\'Polígono precisa de 3+ pontos; linha de 2.\'); return; } zDraw(); }\nfunction zDel(i){ Z_SHAPES.splice(i,1); zDraw(); }\nfunction zClear(){ Z_SHAPES=[]; Z_CUR=[]; zDraw(); }\nfunction zFecha(){ $(\'ovz\').classList.remove(\'open\'); }\nfunction zEnsure(body){ var hz=Z_SHAPES.some(function(z){return z.tipo===\'zona\'}), hl=Z_SHAPES.some(function(z){return z.tipo===\'linha\'}), hh=Z_SHAPES.some(function(z){return z.tipo===\'heatmap\'}), ha=Z_SHAPES.some(function(z){return z.tipo===\'agua\'});\n function add(a,v){ if(a.indexOf(v)<0)a.push(v); }\n function ens(a){ if(hz)add(a,\'intruso\'); if(hl)add(a,\'linha\'); if(hh)add(a,\'heatmap\'); if(ha)add(a,\'piscina\'); }\n if(body.horarios&&body.horarios.length){ body.horarios.forEach(function(h){ h.analiticos=h.analiticos||[]; ens(h.analiticos); }); }\n else { body.analiticos_padrao=body.analiticos_padrao||[]; ens(body.analiticos_padrao); } }\nasync function zSalvar(){ var c=Z_CAM; if(!c)return; var poly=(Z_MODE!==\'linha\');\n if(poly&&Z_CUR.length>=3){ Z_SHAPES.push({tipo:Z_MODE,nome:(Z_MODE===\'heatmap\'?\'Area \':(Z_MODE===\'agua\'?\'Agua \':\'Zona \'))+(Z_SHAPES.length+1),pontos:Z_CUR.slice()}); Z_CUR=[]; }\n else if(!poly&&Z_CUR.length>=2){ Z_SHAPES.push({tipo:\'linha\',nome:\'Linha \'+(Z_SHAPES.length+1),pontos:Z_CUR.slice(0,2)}); Z_CUR=[]; }\n var cfg=c.config||{};\n var body={ camera_id:c.id, camera_nome:c.nome||\'\', ativo:(cfg.ativo!==undefined?cfg.ativo:true),\n  horarios:(cfg.horarios||[]).map(function(h){return {label:h.label||\'\',dias:(h.dias||[]).slice(),hora_inicio:h.hora_inicio||\'00:00\',hora_fim:h.hora_fim||\'23:59\',analiticos:(h.analiticos||[]).slice()};}),\n  analiticos_padrao:(cfg.analiticos_padrao||[]).slice(), zonas_intrusao:Z_SHAPES };\n zEnsure(body);\n try{ await api(\'POST\',\'/api/comercial/analiticos/salvar\',body); zFecha(); msg(\'Zonas salvas. Vale em ~2 min.\',true); loadA(); }catch(e){ msg(\'Erro ao salvar zonas: \'+e.message); } }\n</script>\n'


# ---------- corpo da tela de Mapa de Calor ----------
_HEATMAP_BODY = """
<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px">
 <div class="fld" style="margin:0"><label>Camera</label><select id="h_cam" style="min-width:220px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:9px 12px"></select></div>
 <div class="fld" style="margin:0"><label>Periodo</label><select id="h_per" style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:9px 12px">
  <option value="1h">Ultima hora</option><option value="hoje" selected>Hoje</option><option value="ontem">Ontem</option><option value="7d">Ultimos 7 dias</option><option value="30d">Ultimos 30 dias</option></select></div>
 <button class="btn-primary" onclick="hLoad()">Ver mapa</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Passagens (soma)</div><div class="v" id="h_tot">-</div></div>
 <div class="kpi"><div class="k">Pico (celula)</div><div class="v" id="h_pico">-</div></div>
 <div class="kpi"><div class="k">Horas com dado</div><div class="v" id="h_bk">-</div></div></div>
<div style="display:flex;gap:14px;align-items:center;margin:6px 0 10px">
 <span style="color:var(--muted);font-size:13px">Frio</span>
 <div style="height:12px;width:220px;border-radius:6px;background:linear-gradient(90deg,#0000ff,#00ffff,#00ff00,#ffff00,#ff0000)"></div>
 <span style="color:var(--muted);font-size:13px">Quente</span></div>
<canvas id="h_cv" width="760" height="428" style="max-width:100%;border:1px solid var(--border);border-radius:8px;display:block;background:#111"></canvas>
<div id="h_info" style="color:var(--muted);font-size:12px;margin-top:8px"></div>
<script>
var HCAMS=[]; window.PAGE_INIT=hInit;
function pad(n){return ('0'+n).slice(-2);}
function dayStr(d){return d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate());}
function hourStr(d){return dayStr(d)+pad(d.getHours());}
function jet(t){ t=Math.max(0,Math.min(1,t)); var r,g,b;
 if(t<0.25){b=1;g=t/0.25;r=0;} else if(t<0.5){b=1-(t-0.25)/0.25;g=1;r=0;}
 else if(t<0.75){b=0;g=1;r=(t-0.5)/0.25;} else {b=0;g=1-(t-0.75)/0.25;r=1;}
 return [Math.round(r*255),Math.round(g*255),Math.round(b*255)]; }
async function hInit(){ try{ HCAMS=await api('GET','/api/comercial/analiticos/cameras'); }catch(e){ HCAMS=[]; }
 $('h_cam').innerHTML=HCAMS.map(function(c){ var on=((((c.config||{}).analiticos_padrao)||[]).indexOf('heatmap')>=0)||(((c.config||{}).horarios||[]).some(function(h){return (h.analiticos||[]).indexOf('heatmap')>=0}));
   return '<option value="'+c.id+'">'+esc(c.nome||c.id)+(on?' (heatmap on)':'')+'</option>'; }).join(''); }
function hRange(){ var p=$('h_per').value; var now=new Date();
 if(p==='1h') return [hourStr(now), hourStr(now)];
 if(p==='hoje') return [dayStr(now)+'00', dayStr(now)+'23'];
 if(p==='ontem'){ var y=new Date(now.getTime()-86400000); return [dayStr(y)+'00', dayStr(y)+'23']; }
 if(p==='7d') return [dayStr(new Date(now.getTime()-6*86400000))+'00', dayStr(now)+'23'];
 return [dayStr(new Date(now.getTime()-29*86400000))+'00', dayStr(now)+'23']; }
async function hLoad(){ var cid=$('h_cam').value; if(!cid){ msg('Escolha uma camera'); return; }
 var r=hRange(); var cv=$('h_cv'); var ctx=cv.getContext('2d'); ctx.fillStyle='#111'; ctx.fillRect(0,0,cv.width,cv.height);
 var grid;
 try{ grid=await api('GET','/api/comercial/heatmap/grid?camera_id='+encodeURIComponent(cid)+'&de='+r[0]+'&ate='+r[1]); }catch(e){ msg('Erro: '+e.message); return; }
 $('h_tot').textContent=(grid.total||0).toLocaleString('pt-BR'); $('h_pico').textContent=(grid.max||0); $('h_bk').textContent=(grid.buckets||0);
 $('h_info').textContent='Periodo '+r[0]+' a '+r[1]+' | grade '+(grid.gw||0)+'x'+(grid.gh||0);
 // fundo: snapshot da camera (se online)
 function drawOverlay(){ if(!grid.gw||!grid.max){ msg('Sem dados de movimento nesse periodo.',true); return; }
   var gw=grid.gw,gh=grid.gh,cw=cv.width/gw,ch=cv.height/gh,mx=grid.max||1;
   for(var gy=0;gy<gh;gy++){ for(var gx=0;gx<gw;gx++){ var v=grid.grid[gy*gw+gx]; if(v<=0)continue;
     var t=v/mx, c=jet(t); ctx.fillStyle='rgba('+c[0]+','+c[1]+','+c[2]+','+(0.18+0.55*t)+')'; ctx.fillRect(gx*cw,gy*ch,cw+1,ch+1); } } }
 fetch('/camthumb/'+cid,{headers:{'Authorization':'Bearer '+TOKEN}}).then(function(x){ if(!x.ok)throw 0; return x.blob(); })
  .then(function(b){ var im=new Image(); im.onload=function(){ var w=Math.min(im.naturalWidth||760,760); cv.width=w; cv.height=Math.round(w*((im.naturalHeight||428)/(im.naturalWidth||760)));
    ctx.globalAlpha=0.65; ctx.drawImage(im,0,0,cv.width,cv.height); ctx.globalAlpha=1; drawOverlay(); }; im.src=URL.createObjectURL(b); })
  .catch(function(){ ctx.fillStyle='#111'; ctx.fillRect(0,0,cv.width,cv.height); drawOverlay(); }); }
</script>
"""


# ================= corpos do PORTAL DO PROVEDOR =================
_PROV_DASH_BODY = """
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">Total de Câmeras</div><div class="v" id="k_cam">-</div></div>
 <div class="kpi"><div class="k">Clientes Ativos</div><div class="v" id="k_at" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">A Receber</div><div class="v" id="k_ar" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">A Pagar</div><div class="v" id="k_ap" style="color:var(--bad)">-</div></div></div>

<div class="card">
 <div class="ttl">📈 A Receber por Mês</div>
 <div id="mrows"><div class="center" style="color:var(--muted)">carregando...</div></div>
</div>

<div class="dashgrid2">
 <div class="card"><div class="ttl">⚠️ Alertas</div><div id="alertas"><div class="center" style="color:var(--muted)">carregando...</div></div></div>
 <div class="card"><div class="ttl">Status das Faturas</div><div id="donut" style="text-align:center;padding:6px"></div></div>
</div>

<div id="atalhos" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:4px">
 <a class="btn-primary" style="text-decoration:none;padding:11px 16px;border-radius:9px" href="/provedor/clientes">Gerenciar clientes</a>
 <a style="text-decoration:none;padding:11px 16px;border-radius:9px;background:var(--surface2);border:1px solid var(--border);color:var(--ink)" href="/provedor/faturas">Ver cobrança</a>
 <a style="text-decoration:none;padding:11px 16px;border-radius:9px;background:var(--surface2);border:1px solid var(--border);color:var(--ink)" href="/provedor/propostas">Nova proposta</a></div>

<style>
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px;margin-bottom:18px}
.ttl{font-weight:700;font-size:15px;margin-bottom:14px}
.dashgrid2{display:grid;grid-template-columns:1.6fr 1fr;gap:18px}
@media(max-width:820px){.dashgrid2{grid-template-columns:1fr}}
.mrow{display:flex;justify-content:space-between;align-items:center;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px 15px;margin-bottom:10px}
.mrow .mv{font-weight:700;font-size:15px}
.mrow.tot{background:rgba(139,92,246,.14);border-color:rgba(139,92,246,.45)}.mrow.tot .mv{color:#a78bfa}
.alert{border:1px solid;border-radius:10px;padding:12px 14px;margin-bottom:12px}
.alert.red{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.35)}
.alert.amber{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.35)}
.alert.blue{background:rgba(59,130,246,.08);border-color:rgba(59,130,246,.35)}
.alert.ok{background:rgba(34,197,94,.08);border-color:rgba(34,197,94,.35)}
.alert .ah{font-weight:700;font-size:13.5px}.alert .as{color:var(--muted);font-size:12px;margin-bottom:4px}
.alert .li{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;padding:6px 0;border-top:1px solid var(--border)}
.leg{display:flex;gap:16px;justify-content:center;font-size:13px;margin-top:10px;flex-wrap:wrap}
.leg span{display:flex;align-items:center;gap:6px}.leg i{width:10px;height:10px;border-radius:50%;display:inline-block}
</style>
<script>
window.PAGE_INIT=load;
function dtb(s){ if(!s)return '-'; var p=(''+s).slice(0,10).split('-'); return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s; }
function filtraAtalhos(){ var ls=document.querySelectorAll('#atalhos a'); for(var i=0;i<ls.length;i++){ var h=ls[i].getAttribute('href'); if(h&&h!=='/provedor'&&!document.querySelector('a.it[href="'+h+'"]')){ ls[i].style.display='none'; } } }
async function load(){ filtraAtalhos(); try{ var d=await api('GET','/api/comercial/prov/dashboard');
 $('k_cam').textContent=d.cameras; $('k_at').textContent=d.ativos; $('k_ar').textContent=brl(d.a_receber); $('k_ap').textContent=brl(d.a_pagar);
 var mh=(d.a_receber_mes||[]).map(function(m){ return '<div class="mrow"><div><b style="text-transform:capitalize">'+esc(m.label)+'</b><div style="color:var(--muted);font-size:12px">'+m.count+' fatura(s)'+(m.vencido>0?(' &middot; <span style=\\"color:var(--bad)\\">'+brl(m.vencido)+' vencido</span>'):'')+'</div></div><div class="mv">'+brl(m.total)+'</div></div>'; }).join('');
 mh+='<div class="mrow tot"><b>Total a Receber</b><div class="mv">'+brl(d.total_receber)+'</div></div>';
 $('mrows').innerHTML=mh;
 var a='';
 if((d.vencidas||[]).length){ a+='<div class="alert red"><div class="ah">'+d.vencidas.length+' fatura(s) vencida(s)</div><div class="as">Total: '+brl(d.total_vencido)+'</div>'+
  d.vencidas.map(function(v){return '<div class="li"><span>'+esc(v.cliente||'Cliente')+'</span><span style="white-space:nowrap">Venc: '+dtb(v.vencimento)+' &middot; <span style=\\"color:var(--bad)\\">'+brl(v.valor)+'</span></span></div>';}).join('')+'</div>'; }
 if((d.bloqueados||[]).length){ a+='<div class="alert amber"><div class="ah">'+d.bloqueados.length+' cliente(s) bloqueado(s)</div><div class="as">Acesso suspenso por inadimplência</div>'+
  d.bloqueados.map(function(b){return '<div class="li"><span>'+esc(b.nome||'-')+'</span><span style="color:var(--muted)">'+esc(b.motivo||'')+'</span></div>';}).join('')+'</div>'; }
 if(d.cam_manutencao>0){ a+='<div class="alert blue"><div class="ah">'+d.cam_manutencao+' câmera(s) em manutenção</div><div class="as">Aguardando reparo</div></div>'; }
 if(!a) a='<div class="alert ok"><div class="ah">Tudo em ordem!</div><div class="as">Nenhum alerta no momento.</div></div>';
 $('alertas').innerHTML=a;
 $('donut').innerHTML=donut(d.donut||{});
 }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function donut(s){ var pe=s.pendentes||0,pa=s.pagas||0,ve=s.vencidas||0,t=pe+pa+ve;
 if(!t) return '<div style="color:var(--muted);padding:34px">Sem faturas ainda.</div>';
 var off=25, segs=[[pa,'#22c55e'],[pe,'#f97316'],[ve,'#ef4444']], circ='';
 segs.forEach(function(sg){ var pct=sg[0]/t*100; if(pct<=0)return;
  circ+='<circle cx="21" cy="21" r="15.915" fill="none" stroke="'+sg[1]+'" stroke-width="4.5" stroke-dasharray="'+pct.toFixed(3)+' '+(100-pct).toFixed(3)+'" stroke-dashoffset="'+off.toFixed(3)+'"></circle>'; off-=pct; });
 return '<svg viewBox="0 0 42 42" style="width:150px;height:150px"><circle cx="21" cy="21" r="15.915" fill="none" stroke="var(--border)" stroke-width="4.5"></circle>'+circ+'</svg>'+
  '<div class="leg"><span><i style="background:#f97316"></i>Pendentes: '+pe+'</span><span><i style="background:#22c55e"></i>Pagas: '+pa+'</span><span><i style="background:#ef4444"></i>Vencidas: '+ve+'</span></div>';
}
</script>
"""

_PROV_CLIENTES_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:16px">
 <input id="q" placeholder="Buscar cliente..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <button class="btn-primary" onclick="novo()">+ Novo Cliente</button></div>
<div id="msg" class="msg"></div>
<table><thead><tr><th>Cliente</th><th>Documento</th><th>Plano</th><th>Valor/mes</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:560px"><h2 id="mt">Novo Cliente</h2><input type="hidden" id="c_id">
 <div class="fld"><label>Nome / Razao social</label><input id="c_nome"></div>
 <div class="two"><div class="fld"><label>Documento (CPF/CNPJ)</label><input id="c_doc"></div><div class="fld"><label>Tipo</label><select id="c_dt"><option value="cnpj">CNPJ</option><option value="cpf">CPF</option></select></div></div>
 <div class="two"><div class="fld"><label>E-mail</label><input id="c_email" type="email"></div><div class="fld"><label>WhatsApp</label><input id="c_tel"></div></div>
 <div class="two"><div class="fld"><label>CEP</label><input id="c_cep" onblur="cepCli()" placeholder="00000-000"></div><div class="fld"><label>Numero</label><input id="c_num"></div></div>
 <div class="fld"><label>Endereco (rua)</label><input id="c_end"></div>
 <div class="two"><div class="fld"><label>Bairro</label><input id="c_bairro"></div><div class="fld"><label>Complemento</label><input id="c_compl"></div></div>
 <div class="two"><div class="fld"><label>Cidade</label><input id="c_cidade"></div><div class="fld"><label>UF</label><input id="c_uf" maxlength="2"></div></div>
 <div class="two"><div class="fld"><label>Plano</label><select id="c_plano" onchange="onPlano()"></select></div><div class="fld"><label>Valor mensal (R$)</label><input id="c_valor" type="number" step="0.01"></div></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>
<div class="ov" id="ovs"><div class="modal" style="max-width:720px"><h2>Sub-usuarios <span id="s_cli" style="color:var(--muted);font-weight:400;font-size:14px"></span></h2>
 <div id="smsg" class="msg"></div>
 <table><thead><tr><th>Nome</th><th>E-mail</th><th>Unidade</th><th>Cams</th><th>Status</th><th></th></tr></thead><tbody id="srows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>
 <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
  <h3 id="sf_t" style="font-size:14px;margin:0 0 10px">Novo sub-usuario</h3>
  <input type="hidden" id="s_id">
  <div class="two"><div class="fld"><label>Nome</label><input id="s_nome"></div><div class="fld"><label>Unidade / Apto (opcional)</label><input id="s_unid"></div></div>
  <div class="two"><div class="fld"><label>E-mail (login)</label><input id="s_email" type="email"></div><div class="fld"><label>WhatsApp (opcional)</label><input id="s_tel"></div></div>
  <div class="two"><div class="fld"><label>Senha</label><input id="s_pw" type="text" placeholder="min 4 (em branco = nao altera)"></div><div class="fld"><label style="display:flex;gap:7px;align-items:center;margin-top:24px;cursor:pointer;font-size:13px;color:var(--ink)"><input type="checkbox" id="s_wa" style="width:auto"> Receber alertas no WhatsApp</label></div></div>
  <div class="two"><div class="fld"><label>Cameras liberadas (ao vivo)</label><div id="s_cams" style="max-height:150px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:8px;font-size:13px">-</div></div><div class="fld"><label>Gravacoes liberadas</label><div id="s_gravs" style="max-height:150px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:8px;font-size:13px">-</div></div></div>
  <div class="fld"><label>Mosaicos liberados</label><div id="s_mos" style="max-height:130px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:8px;font-size:13px">-</div></div>
  <div style="text-align:right;margin-top:4px"><button class="act" onclick="sLimpa()">limpar</button><button class="btn-primary" onclick="sSalvar()">Salvar sub-usuario</button></div>
 </div>
 <div class="foot"><button onclick="sFecha()">Fechar</button></div></div></div>
<div class="ov" id="ovmos"><div class="modal" style="max-width:640px"><h2>Mosaicos <span id="mos_cli" style="color:var(--muted);font-weight:400;font-size:14px"></span></h2>
 <div id="mosmsg" class="msg"></div>
 <p style="color:var(--muted);font-size:12px;margin:0 0 8px">Agrupe ate 4 cameras deste cliente numa tela 2x2. O cliente ve os mosaicos ativos no portal; sub-usuario ve so os liberados.</p>
 <table><thead><tr><th>Nome</th><th>Cameras</th><th>Status</th><th></th></tr></thead><tbody id="mosrows"><tr><td colspan="4" class="center">carregando...</td></tr></tbody></table>
 <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
  <h3 id="mf_t" style="font-size:14px;margin:0 0 10px">Novo mosaico</h3>
  <input type="hidden" id="m_id">
  <div class="two"><div class="fld"><label>Nome</label><input id="m_nome" placeholder="Ex.: Entrada, Setor A"></div><div class="fld"><label style="display:flex;gap:7px;align-items:center;margin-top:24px;cursor:pointer;font-size:13px;color:var(--ink)"><input type="checkbox" id="m_ativo" checked style="width:auto"> Ativo</label></div></div>
  <div class="fld"><label>Cameras (ate 4)</label><div id="m_cams" style="max-height:170px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:8px;font-size:13px">-</div></div>
  <div style="text-align:right"><button class="act" onclick="mLimpa()">limpar</button><button class="btn-primary" onclick="mSalvar()">Salvar mosaico</button></div>
 </div>
 <div class="foot"><button onclick="mFecha()">Fechar</button></div></div></div>
<div class="ov" id="ovcam"><div class="modal" style="max-width:620px"><h2>Cameras do cliente <span id="cam_cli" style="color:var(--muted);font-weight:400;font-size:14px"></span></h2>
 <div id="cammsg" class="msg"></div>
 <p style="color:var(--muted);font-size:12px;margin:0 0 8px">Marque as cameras que pertencem a este cliente (ele passa a ve-las no portal). Desmarcar remove o vinculo.</p>
 <div id="cam_list" style="max-height:340px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:8px;font-size:13px">carregando...</div>
 <div class="foot"><button onclick="camFecha()">Cancelar</button><button class="btn-primary" onclick="camSalvar()">Salvar</button></div></div></div>
<script>
var CLI=[], PLANOS=[], SUBS=[], SCLI=null, SCAMS=[], ACAMS=[], ACLI=null, MOS=[], MCLI=null, MCAMS=[], SMOS=[]; window.PAGE_INIT=load;
function digs(s){return (''+(s||'')).replace(/\\D/g,'');}
function fmtDoc(raw){var d=digs(raw); if(d.length===14)return d.slice(0,2)+'.'+d.slice(2,5)+'.'+d.slice(5,8)+'/'+d.slice(8,12)+'-'+d.slice(12,14); if(d.length===11)return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9,11); return raw||'';}
async function load(){ try{ try{PLANOS=(await api('GET','/api/comercial/prov/planos'))||[];}catch(_e){PLANOS=[];} fillPlanosCli(); CLI=await api('GET','/api/comercial/prov/clientes'); render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function fillPlanosCli(){ var s=$('c_plano'); if(!s)return; var a=PLANOS.filter(function(p){return p.ativo!==false;});
 s.innerHTML='<option value="">- sem plano -</option>'+a.map(function(p){return '<option value="'+esc(p.id)+'" data-nome="'+esc(p.nome||'')+'" data-valor="'+(p.valor||0)+'">'+esc((p.nome||'-')+' - R$ '+(p.valor||0))+'</option>';}).join(''); }
function onPlano(){ var s=$('c_plano'),o=s.options[s.selectedIndex]; if(o&&s.value){ var v=o.getAttribute('data-valor'); if(v!==null&&v!=='')$('c_valor').value=v; } }
async function cepCli(){ var cep=($('c_cep').value||'').replace(/\D/g,''); if(cep.length!==8)return; try{ var r=await api('GET','/api/comercial/geocode?cep='+cep); if(r.logradouro)$('c_end').value=r.logradouro; if(r.bairro)$('c_bairro').value=r.bairro; if(r.cidade)$('c_cidade').value=r.cidade; if(r.uf)$('c_uf').value=r.uf; }catch(e){} }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=CLI.filter(function(c){return !q||((''+(c.nome||'')+' '+(c.document_number||'')).toLowerCase().indexOf(q)>=0);});
 $('rows').innerHTML=arr.map(function(c){ var bloq=(c.status||'ativo')==='bloqueado';
  var st=bloq?'<span class="pill" style="color:var(--bad)">Bloqueado</span>':'<span class="pill ok">Ativo</span>';
  return '<tr><td><b>'+esc(c.nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(c.email||'')+'</div></td>'+
   '<td>'+esc(fmtDoc(c.document_number)||'-')+'</td><td>'+esc(c.plano_nome||'-')+'</td><td class="money">'+brl(c.valor_mensal)+'</td><td>'+st+'</td>'+
   '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="editar(\\''+c.id+'\\')">editar</button>'+'<button class="act" onclick="cams(\\''+c.id+'\\')">cameras</button>'+'<button class="act" onclick="mosaicos(\\''+c.id+'\\')">mosaicos</button>'+'<button class="act" style="color:var(--accent)" onclick="subs(\\''+c.id+'\\')">sub-usuarios</button>'+
   (bloq?'<button class="act" style="color:var(--ok)" onclick="stat(\\''+c.id+'\\',false)">desbloquear</button>':'<button class="act" style="color:var(--bad)" onclick="stat(\\''+c.id+'\\',true)">bloquear</button>')+
   '<button class="act" onclick="excluir(\\''+c.id+'\\')">excluir</button></td></tr>'; }).join('')||'<tr><td colspan="6" class="center">Nenhum cliente. Clique em Novo Cliente.</td></tr>'; }
function novo(){ $('mt').textContent='Novo Cliente'; ['c_id','c_nome','c_doc','c_email','c_tel','c_plano','c_valor','c_cep','c_num','c_end','c_bairro','c_compl','c_cidade','c_uf'].forEach(function(i){$(i).value='';}); $('c_dt').value='cnpj'; $('ov').classList.add('open'); }
function editar(id){ var c=CLI.filter(function(x){return x.id===id})[0]; if(!c)return; $('mt').textContent='Editar Cliente'; $('c_id').value=c.id; $('c_nome').value=c.nome||''; $('c_doc').value=c.document_number||''; $('c_dt').value=c.document_type||'cnpj'; $('c_email').value=c.email||''; $('c_tel').value=c.telefone||''; $('c_plano').value=c.plano_id||''; $('c_valor').value=c.valor_mensal!=null?c.valor_mensal:''; $('c_cep').value=c.cep||''; $('c_num').value=c.numero||''; $('c_end').value=c.endereco||c.logradouro||''; $('c_bairro').value=c.bairro||''; $('c_compl').value=c.complemento||''; $('c_cidade').value=c.cidade||''; $('c_uf').value=c.uf||''; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var _ps=$('c_plano'),_po=_ps.options[_ps.selectedIndex]; var b={id:$('c_id').value,nome:$('c_nome').value.trim(),document_number:$('c_doc').value.trim(),document_type:$('c_dt').value,email:$('c_email').value.trim(),telefone:$('c_tel').value.trim(),plano_id:_ps.value,plano_nome:(_ps.value&&_po?_po.getAttribute('data-nome'):''),valor_mensal:parseFloat($('c_valor').value||0)||0,cep:$('c_cep').value.trim(),numero:$('c_num').value.trim(),endereco:$('c_end').value.trim(),bairro:$('c_bairro').value.trim(),complemento:$('c_compl').value.trim(),cidade:$('c_cidade').value.trim(),uf:$('c_uf').value.trim()};
 if(!b.nome){msg('Informe o nome');return;}
 try{ await api('POST','/api/comercial/prov/clientes/salvar',b); fecha(); msg('Cliente salvo.',true); load(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function stat(id,bloq){ if(!bloq){ abreDesbloq(id); return; } if(!confirm('Bloquear este cliente? O acesso dele e dos sub-usuarios sera suspenso imediatamente e as sessoes ativas serao encerradas.'))return; try{ await api('POST','/api/comercial/prov/clientes/'+id+'/status',{bloquear:true}); msg('Cliente bloqueado.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
var _dbId=null,_dbMode='simples';
function _dbInit(){ if(document.getElementById('dbov'))return;
 var st=document.createElement('style');
 st.textContent='.dbov{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:9999;padding:16px}.dbov.open{display:flex}.dbbox{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px;max-width:540px;width:100%}.dbcard{border:1px solid var(--border);border-radius:11px;padding:16px 12px;text-align:center;cursor:pointer;background:var(--surface2);transition:.12s}.dbcard.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}.dbcard div.i{font-size:22px;margin-bottom:4px}';
 document.head.appendChild(st);
 var h='<div class="dbov" id="dbov"><div class="dbbox">'+
  '<div style="display:flex;justify-content:space-between;align-items:center"><h3 style="margin:0;font-size:17px">Desbloquear Cliente</h3><button class="act" onclick="dbClose()" style="font-size:16px">&times;</button></div>'+
  '<div id="db_nome" style="color:var(--muted);margin:2px 0 14px;font-size:14px"></div>'+
  '<p style="font-size:13.5px;color:var(--muted);margin:0 0 14px;line-height:1.5">Deseja desbloquear com <b style="color:var(--ink)">promessa de pagamento</b>? A fatura continuara vencida &mdash; apenas o acesso sera liberado temporariamente.</p>'+
  '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'+
   '<div class="dbcard" id="db_c_s" onclick="dbModeS()"><div class="i">&#128275;</div><b>Desbloqueio Simples</b><div style="font-size:12px;color:var(--muted);margin-top:2px">Sem promessa</div></div>'+
   '<div class="dbcard" id="db_c_p" onclick="dbModeP()"><div class="i">&#128197;</div><b>Com Promessa</b><div style="font-size:12px;color:var(--muted);margin-top:2px">Definir data</div></div>'+
  '</div>'+
  '<div id="db_datebox" style="display:none;margin-top:14px">'+
   '<label style="font-size:13px;color:var(--muted)">Data prometida para pagamento</label>'+
   '<input type="date" id="db_data" style="width:100%;margin-top:5px;padding:9px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);color:var(--ink)">'+
   '<div style="font-size:12px;color:var(--muted);margin-top:7px;line-height:1.45">Se o pagamento nao for registrado ate esta data, o cliente sera bloqueado novamente automaticamente.</div>'+
  '</div>'+
  '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:18px"><button onclick="dbClose()">Cancelar</button><button class="btn-primary" onclick="dbConfirm()">Confirmar Desbloqueio</button></div>'+
  '</div></div>';
 document.body.insertAdjacentHTML('beforeend',h);
}
function abreDesbloq(id){ _dbInit(); var c=(CLI||[]).filter(function(x){return x.id===id})[0]||{}; _dbId=id;
 document.getElementById('db_nome').textContent=c.nome||'';
 var t=new Date(); t.setDate(t.getDate()+1); var di=document.getElementById('db_data'); di.min=t.toISOString().slice(0,10); di.value='';
 dbMode('simples'); document.getElementById('dbov').classList.add('open');
}
function dbMode(m){ _dbMode=m; document.getElementById('db_c_s').classList.toggle('sel',m==='simples'); document.getElementById('db_c_p').classList.toggle('sel',m==='promessa'); document.getElementById('db_datebox').style.display=(m==='promessa')?'block':'none'; }
function dbModeS(){ dbMode('simples'); }
function dbModeP(){ dbMode('promessa'); }
function dbClose(){ var o=document.getElementById('dbov'); if(o)o.classList.remove('open'); }
async function dbConfirm(){ var body={modo:_dbMode}; if(_dbMode==='promessa'){ var d=document.getElementById('db_data').value; if(!d){ msg('Data obrigatoria'); return; } var hoje=new Date().toISOString().slice(0,10); if(d<=hoje){ msg('A data da promessa deve ser futura'); return; } body.promessa_data=d; }
 try{ await api('POST','/api/comercial/prov/clientes/'+_dbId+'/desbloquear',body); dbClose(); msg(_dbMode==='promessa'?'Cliente desbloqueado com promessa.':'Cliente desbloqueado.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(id){ if(!confirm('Excluir este cliente?'))return; try{ await api('DELETE','/api/comercial/prov/clientes/'+id); msg('Excluido.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function subs(id){ var c=CLI.filter(function(x){return x.id===id})[0]; if(!c)return; SCLI=c; $('s_cli').textContent='- '+(c.nome||''); $('ovs').classList.add('open'); $('srows').innerHTML='<tr><td colspan="6" class="center">carregando...</td></tr>'; await sLoad(); sLimpa(); }
function sMsg(t,ok){ var m=$('smsg'); if(!m)return; m.textContent=t; m.className='msg'+(ok?' ok':''); }
async function sLoad(){ try{ SCAMS=(await api('GET','/api/cliente-cameras?cliente_id='+SCLI.id))||[]; SMOS=(await api('GET','/api/comercial/prov/mosaicos?cliente_id='+SCLI.id))||[]; SUBS=(await api('GET','/api/subusers?cliente_id='+SCLI.id))||[]; sRenderCams(); sRenderMos(); sRender(); }catch(e){ sMsg('Erro: '+e.message); } }
function sRender(){ if(!SUBS.length){ $('srows').innerHTML='<tr><td colspan="6" class="center" style="color:var(--muted)">Nenhum sub-usuario ainda.</td></tr>'; return; }
 $('srows').innerHTML=SUBS.map(function(s,i){ var inativo=s.status!=='ativo'; var stt=inativo?'<span class="pill" style="color:var(--bad)">Inativo</span>':'<span class="pill ok">Ativo</span>'; var act=inativo?'<button class="act" style="color:var(--ok)" onclick="sStat('+i+',true)">ativar</button>':'<button class="act" style="color:var(--bad)" onclick="sStat('+i+',false)">bloquear</button>'; return '<tr><td><b>'+esc(s.nome||'-')+'</b></td><td>'+esc(s.email||'')+'</td><td>'+esc(s.unidade||'-')+'</td><td>'+((s.allowed_cameras||[]).length)+'</td><td>'+stt+'</td><td style="text-align:right;white-space:nowrap"><button class="act" onclick="sEdit('+i+')">editar</button>'+act+'<button class="act" style="color:var(--bad)" onclick="sDel('+i+')">excluir</button></td></tr>'; }).join(''); }
function sBox(cls){ return SCAMS.length? SCAMS.map(function(cm){ return '<label style="display:block;padding:3px 0;cursor:pointer"><input type="checkbox" class="'+cls+'" value="'+esc(cm.id)+'" style="width:auto;margin-right:7px">'+esc(cm.nome)+'</label>'; }).join('') : '<span style="color:var(--muted)">Este cliente nao tem cameras cadastradas.</span>'; }
function sRenderCams(){ $('s_cams').innerHTML=sBox('scam'); $('s_gravs').innerHTML=sBox('sgrav'); }
function sRenderMos(){ var el=$('s_mos'); if(!el)return; el.innerHTML=SMOS.length? SMOS.map(function(m){ return '<label style="display:block;padding:3px 0;cursor:pointer"><input type="checkbox" class="smos" value="'+esc(m.id)+'" style="width:auto;margin-right:7px">'+esc(m.nome||'-')+(m.ativo===false?' (inativo)':'')+'</label>'; }).join('') : '<span style="color:var(--muted)">Nenhum mosaico deste cliente.</span>'; }
function sChecks(cls){ var out=[]; Array.prototype.forEach.call(document.querySelectorAll('.'+cls),function(el){ if(el.checked)out.push(el.value); }); return out; }
function sSet(cls,arr){ arr=arr||[]; Array.prototype.forEach.call(document.querySelectorAll('.'+cls),function(el){ el.checked=arr.indexOf(el.value)>=0; }); }
function sLimpa(){ $('sf_t').textContent='Novo sub-usuario'; ['s_id','s_nome','s_unid','s_email','s_tel','s_pw'].forEach(function(i){$(i).value='';}); $('s_wa').checked=false; sSet('scam',[]); sSet('sgrav',[]); sSet('smos',[]); }
function sEdit(i){ var s=SUBS[i]; if(!s)return; $('sf_t').textContent='Editar sub-usuario'; $('s_id').value=s.id; $('s_nome').value=s.nome||''; $('s_unid').value=s.unidade||''; $('s_email').value=s.email||''; $('s_tel').value=s.telefone||''; $('s_pw').value=''; $('s_wa').checked=!!s.receber_alertas_whatsapp; sSet('scam',s.allowed_cameras||[]); sSet('sgrav',s.allowed_gravacoes||[]); sSet('smos',s.allowed_mosaicos||[]); }
async function sSalvar(){ if(!SCLI)return; var id=$('s_id').value; var pw=$('s_pw').value; var b={client_id:SCLI.id,nome:$('s_nome').value.trim(),email:$('s_email').value.trim(),telefone:$('s_tel').value.trim(),unidade:$('s_unid').value.trim(),allowed_cameras:sChecks('scam'),allowed_gravacoes:sChecks('sgrav'),allowed_mosaicos:sChecks('smos'),receber_alertas_whatsapp:$('s_wa').checked}; if(pw)b.senha=pw; if(!b.nome||!b.email){ sMsg('Informe nome e e-mail.'); return; } try{ if(id){ await api('PUT','/api/subusers/'+id,b); } else { if(!pw||pw.length<4){ sMsg('Defina a senha (min 4).'); return; } await api('POST','/api/subusers',b); } sMsg('Salvo.',true); sLimpa(); sLoad(); }catch(e){ sMsg('Erro: '+e.message); } }
async function sStat(i,ativo){ var s=SUBS[i]; if(!s)return; try{ await api('POST','/api/subusers/'+s.id+'/status',{ativo:ativo}); sLoad(); }catch(e){ sMsg('Erro: '+e.message); } }
async function sDel(i){ var s=SUBS[i]; if(!s)return; if(!confirm('Excluir o sub-usuario '+(s.nome||'')+'? O login dele sera removido.'))return; try{ await api('DELETE','/api/subusers/'+s.id); sMsg('Excluido.',true); sLimpa(); sLoad(); }catch(e){ sMsg('Erro: '+e.message); } }
function sFecha(){ $('ovs').classList.remove('open'); }
async function cams(id){ var c=CLI.filter(function(x){return x.id===id})[0]; if(!c)return; ACLI=c; $('cam_cli').textContent='- '+(c.nome||''); $('cammsg').textContent=''; $('cam_list').innerHTML='carregando...'; $('ovcam').classList.add('open'); try{ ACAMS=(await api('GET','/api/comercial/prov/cameras-atrela'))||[]; camRender(); }catch(e){ $('cam_list').innerHTML='<span style="color:var(--bad)">Erro: '+esc(e&&e.message||e)+'</span>'; } }
function camRender(){ if(!ACAMS.length){ $('cam_list').innerHTML='<span style="color:var(--muted)">Nenhuma camera cadastrada. Cadastre em Cameras e IA.</span>'; return; }
 $('cam_list').innerHTML=ACAMS.map(function(cm){ var mine=cm.cliente_id===ACLI.id; var outro=(cm.cliente_id&&!mine)?' <span style="color:var(--muted)">(hoje de: '+esc(cm.cliente_nome||'outro cliente')+')</span>':''; return '<label style="display:block;padding:4px 0;cursor:pointer"><input type="checkbox" class="acam" value="'+esc(cm.id)+'"'+(mine?' checked':'')+' style="width:auto;margin-right:7px">'+esc(cm.nome)+outro+'</label>'; }).join(''); }
function camFecha(){ $('ovcam').classList.remove('open'); }
async function mosaicos(id){ var c=CLI.filter(function(x){return x.id===id})[0]; if(!c)return; MCLI=c; $('mos_cli').textContent='- '+(c.nome||''); $('mosmsg').textContent=''; $('ovmos').classList.add('open'); try{ MCAMS=(await api('GET','/api/cliente-cameras?cliente_id='+MCLI.id))||[]; MOS=(await api('GET','/api/comercial/prov/mosaicos?cliente_id='+MCLI.id))||[]; mRenderCams(); mRender(); mLimpa(); }catch(e){ $('mosmsg').textContent='Erro: '+(e&&e.message||e); } }
function mMsg(t,ok){ var m=$('mosmsg'); if(!m)return; m.textContent=t; m.className='msg'+(ok?' ok':''); }
async function mReload(){ try{ MOS=(await api('GET','/api/comercial/prov/mosaicos?cliente_id='+MCLI.id))||[]; mRender(); }catch(e){ mMsg('Erro: '+(e&&e.message||e)); } }
function mRender(){ if(!MOS.length){ $('mosrows').innerHTML='<tr><td colspan="4" class="center" style="color:var(--muted)">Nenhum mosaico ainda.</td></tr>'; return; }
 $('mosrows').innerHTML=MOS.map(function(m,i){ var inativo=m.ativo===false; var stt=inativo?'<span class="pill" style="color:var(--muted)">Inativo</span>':'<span class="pill ok">Ativo</span>'; var tg=inativo?'<button class="act" style="color:var(--ok)" onclick="mToggle('+i+')">ativar</button>':'<button class="act" onclick="mToggle('+i+')">desativar</button>'; return '<tr><td><b>'+esc(m.nome||'-')+'</b></td><td>'+((m.cameras||[]).length)+'/4</td><td>'+stt+'</td><td style="text-align:right;white-space:nowrap"><a class="act" href="/mosaico/'+esc(m.id)+'?t='+encodeURIComponent(TOKEN)+'" target="_blank" style="text-decoration:none">ver</a><button class="act" onclick="mEdit('+i+')">editar</button>'+tg+'<button class="act" style="color:var(--bad)" onclick="mDel('+i+')">excluir</button></td></tr>'; }).join(''); }
function mBox(){ return MCAMS.length? MCAMS.map(function(cm){ return '<label style="display:block;padding:3px 0;cursor:pointer"><input type="checkbox" class="mcam" value="'+esc(cm.id)+'" onchange="mLimit(this)" style="width:auto;margin-right:7px">'+esc(cm.nome)+'</label>'; }).join('') : '<span style="color:var(--muted)">Este cliente nao tem cameras. Use o botao cameras primeiro.</span>'; }
function mRenderCams(){ $('m_cams').innerHTML=mBox(); }
function mChecks(){ var out=[]; Array.prototype.forEach.call(document.querySelectorAll('.mcam'),function(el){ if(el.checked)out.push(el.value); }); return out; }
function mSetChecks(arr){ arr=arr||[]; Array.prototype.forEach.call(document.querySelectorAll('.mcam'),function(el){ el.checked=arr.indexOf(el.value)>=0; }); }
function mLimit(el){ if(el.checked && mChecks().length>4){ el.checked=false; mMsg('Maximo de 4 cameras por mosaico.'); } }
function mLimpa(){ $('mf_t').textContent='Novo mosaico'; $('m_id').value=''; $('m_nome').value=''; $('m_ativo').checked=true; mSetChecks([]); }
function mEdit(i){ var m=MOS[i]; if(!m)return; $('mf_t').textContent='Editar mosaico'; $('m_id').value=m.id; $('m_nome').value=m.nome||''; $('m_ativo').checked=m.ativo!==false; mSetChecks(m.cameras||[]); }
async function mSalvar(){ if(!MCLI)return; var cams=mChecks(); if(cams.length>4){ mMsg('Maximo de 4 cameras.'); return; } var b={id:$('m_id').value,cliente_id:MCLI.id,nome:$('m_nome').value.trim(),cameras:cams,ativo:$('m_ativo').checked}; if(!b.nome){ mMsg('Informe o nome.'); return; } if(!cams.length){ mMsg('Selecione ao menos 1 camera.'); return; } try{ await api('POST','/api/comercial/prov/mosaicos/salvar',b); mMsg('Salvo.',true); mLimpa(); mReload(); }catch(e){ mMsg('Erro: '+(e&&e.message||e)); } }
async function mToggle(i){ var m=MOS[i]; if(!m)return; try{ await api('POST','/api/comercial/prov/mosaicos/'+m.id+'/toggle'); mReload(); }catch(e){ mMsg('Erro: '+(e&&e.message||e)); } }
async function mDel(i){ var m=MOS[i]; if(!m)return; if(!confirm('Excluir o mosaico '+(m.nome||'')+'?'))return; try{ await api('DELETE','/api/comercial/prov/mosaicos/'+m.id); mMsg('Excluido.',true); mReload(); }catch(e){ mMsg('Erro: '+(e&&e.message||e)); } }
function mFecha(){ $('ovmos').classList.remove('open'); }
async function camSalvar(){ if(!ACLI)return; var ids=[]; Array.prototype.forEach.call(document.querySelectorAll('.acam'),function(el){ if(el.checked)ids.push(el.value); }); try{ var r=await api('POST','/api/comercial/prov/clientes/'+ACLI.id+'/cameras',{camera_ids:ids}); camFecha(); msg('Cameras atualizadas ('+(r.alteradas||0)+' alterada(s)).',true); load(); }catch(e){ msg('Erro: '+(e&&e.message||e)); } }
</script>
"""

_PROV_FATURAS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><div style="flex:1"></div>
 <button class="btn-primary" onclick="sync()">Sincronizar Asaas</button></div>
<div id="msg" class="msg"></div>
<div class="cards">
 <div class="kpi"><div class="k">A Receber</div><div class="v" id="k_ar" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Vencidas</div><div class="v" id="k_vc" style="color:var(--bad)">-</div></div>
 <div class="kpi"><div class="k">Recebido</div><div class="v" id="k_rc" style="color:var(--ok)">-</div></div></div>
<table><thead><tr><th>Cliente</th><th>Numero</th><th>Vencimento</th><th>Valor</th><th>Status</th></tr></thead>
<tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<script>
var FAT=[]; window.PAGE_INIT=load;
function dtd(s){var p=(''+(s||'')).slice(0,10).split('-'); return p.length===3?p[2]+'/'+p[1]+'/'+p[0]:(s||'-');}
function stp(s){return {paga:['Paga','ok'],pendente:['Pendente','off'],vencida:['Vencida','off'],cancelada:['Cancelada','off']}[s]||['?','off'];}
async function load(){ try{ FAT=await api('GET','/api/comercial/prov/faturas'); kpis(); render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function kpis(){ var ar=0,vc=0,rc=0; FAT.forEach(function(f){var v=parseFloat(f.valor||0);var s=f.status; if(s==='paga')rc+=v; else if(s==='vencida')vc+=v; else if(s==='pendente')ar+=v;});
 $('k_ar').textContent=brl(ar); $('k_vc').textContent=brl(vc); $('k_rc').textContent=brl(rc); }
function render(){ $('rows').innerHTML=FAT.map(function(f){var si=stp(f.status); var col=f.status==='vencida'?'color:var(--bad)':(f.status==='paga'?'color:var(--ok)':'');
 return '<tr><td>'+esc(f.cliente_nome||'-')+'</td><td>'+esc(f.numero||'-')+'</td><td>'+dtd(f.vencimento)+'</td><td class="money">'+brl(f.valor)+'</td><td><span class="pill '+si[1]+'" style="'+col+'">'+si[0]+'</span></td></tr>';}).join('')||'<tr><td colspan="5" class="center">Nenhuma fatura. Clique em Sincronizar Asaas.</td></tr>'; }
async function sync(){ try{ msg('Sincronizando com o Asaas...',true); var r=await api('POST','/api/comercial/prov/faturas/sync'); msg((r.sincronizadas||0)+' faturas sincronizadas.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_PROV_MARCA_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 18px">Personalize a identidade visual que seus clientes veem: <b>logo</b>, <b>cores</b> e <b>nome da marca</b>. Vale para a tela de login e, se voce tiver dominio proprio, para o seu dominio. Voce so edita a SUA marca.</p>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:26px;align-items:start">
 <div>
  <div class="fld"><label>Nome da marca (titulo do painel)</label><input id="nome" oninput="prev()" placeholder="Ex.: NetFibra Seguranca"></div>
  <div class="two">
   <div class="fld"><label>Cor da marca (botoes/destaque)</label><input id="cor" type="color" value="#f97316" oninput="prev()" style="height:44px;padding:3px"></div>
   <div class="fld"><label>Cor do menu (barra lateral)</label><input id="cormenu" type="color" value="#171a21" oninput="prev()" style="height:44px;padding:3px"></div>
  </div>
  <div class="fld"><label>Logo (PNG/JPG ate 2MB)</label><input id="logofile" type="file" accept="image/*" onchange="upLogo()"><div id="logostat" style="font-size:12px;color:var(--muted);margin-top:6px"></div></div>
  <div class="fld"><label>Dominio proprio (opcional - Fase 2)</label><input id="dominio" placeholder="ex.: painel.suaempresa.com.br" oninput="prev()"><div style="font-size:12px;color:var(--muted);margin-top:6px">Aponte um registro <b>CNAME</b> (ou A) do seu dominio para o nosso servidor. Apos salvar, o HTTPS e emitido automaticamente no primeiro acesso.</div></div>
  <div style="margin-top:8px"><button class="btn-primary" onclick="salvar()">Salvar minha marca</button></div>
 </div>
 <div>
  <div style="font-size:12px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">Previa ao vivo</div>
  <div style="border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;height:290px;background:var(--bg)">
   <div id="prevside" style="width:128px;flex:none;background:#171a21;display:flex;flex-direction:column;align-items:center;padding:16px 8px;gap:9px">
    <img id="prevlogo" alt="" style="max-width:96px;max-height:52px;display:none">
    <div id="prevlogotxt" style="font-weight:800;color:#fff;font-size:14px;text-align:center;letter-spacing:1px;word-break:break-word">MARCA</div>
    <div style="width:100%;height:1px;background:rgba(255,255,255,.12);margin:6px 0"></div>
    <div style="color:#cfd6e2;font-size:11px;align-self:flex-start;padding-left:8px">Dashboard</div>
    <div style="color:#cfd6e2;font-size:11px;align-self:flex-start;padding-left:8px">Cameras</div>
    <div style="color:#cfd6e2;font-size:11px;align-self:flex-start;padding-left:8px">Faturas</div>
   </div>
   <div style="flex:1;padding:18px;display:flex;flex-direction:column;gap:12px">
    <div id="prevtitle" style="font-weight:700;font-size:16px;color:var(--ink)">Painel</div>
    <div style="height:10px;width:72%;background:var(--surface2);border-radius:6px"></div>
    <div style="height:10px;width:52%;background:var(--surface2);border-radius:6px"></div>
    <div style="height:10px;width:60%;background:var(--surface2);border-radius:6px"></div>
    <button id="prevbtn" style="align-self:flex-start;margin-top:auto;background:#f97316;color:#1a1205;border:none;border-radius:8px;padding:9px 18px;font-weight:700">Entrar</button>
   </div>
  </div>
  <div style="font-size:12px;color:var(--muted);margin-top:8px">E assim que seus clientes verao o sistema com a sua marca.</div>
 </div>
</div>
<script>
window.PAGE_INIT=load;
function prev(){ var cor=$('cor').value, menu=$('cormenu').value, nome=($('nome').value||'').trim();
 $('prevside').style.background=menu; $('prevbtn').style.background=cor;
 $('prevtitle').textContent=nome||'Painel'; $('prevlogotxt').textContent=(nome||'MARCA').toUpperCase(); }
async function load(){ try{ var b=await api('GET','/api/comercial/branding/me');
  $('nome').value=b.nome_marca||''; if(b.cor)$('cor').value=b.cor; if(b.cor_menu)$('cormenu').value=b.cor_menu; $('dominio').value=b.dominio||'';
  if(b.logo){ var im=$('prevlogo'); im.src=b.logo; im.style.display='block'; $('prevlogotxt').style.display='none'; $('logostat').textContent='Logo atual carregada.'; }
 }catch(e){ msg('Erro ao carregar (logado como provedor?): '+e.message); } prev(); }
async function upLogo(){ var f=$('logofile').files[0]; if(!f)return; if(f.size>2*1024*1024){msg('Logo maior que 2MB');return;}
 var rd=new FileReader(); rd.onload=async function(){ try{ var r=await api('POST','/api/comercial/branding/logo',{data:rd.result});
   var im=$('prevlogo'); im.src=r.logo+'?t='+Date.now(); im.style.display='block'; $('prevlogotxt').style.display='none'; $('logostat').textContent='Logo enviada.'; msg('Logo enviada.',true);
  }catch(e){ msg('Erro na logo: '+e.message); } }; rd.readAsDataURL(f); }
async function salvar(){ var b={ nome_marca:$('nome').value.trim(), cor:$('cor').value, cor_menu:$('cormenu').value, dominio:$('dominio').value.trim().toLowerCase() };
 try{ await api('POST','/api/comercial/branding/salvar',b); msg('Marca salva com sucesso.',true); }catch(e){ msg('Erro ao salvar: '+e.message); } }
</script>
"""


_PROV_CAMERAS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px">
 <input id="q" placeholder="Buscar camera..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <button onclick="reload()">Atualizar</button><button class="btn-primary" onclick="novaCam()">+ Adicionar camera</button></div>
<div id="msg" class="msg"></div>
<div id="planobar" class="cards" style="margin-bottom:6px"></div>
<p id="planohint" style="color:var(--muted);font-size:13px;margin:0 0 14px">carregando...</p>
<div id="grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px"><div class="center" style="grid-column:1/-1">carregando...</div></div>

<div class="ov" id="ovlive"><div class="modal" style="max-width:900px;width:100%">
 <h2 style="display:flex;align-items:center;gap:10px"><span id="lv_nome"></span><span style="flex:1"></span><button onclick="fecharLive()">Fechar</button></h2>
 <div style="position:relative;padding-top:56.25%;background:#000;border-radius:8px;overflow:hidden">
  <iframe id="lv_frame" src="about:blank" allow="autoplay; fullscreen" style="position:absolute;inset:0;width:100%;height:100%;border:0"></iframe></div>
 <div style="font-size:12px;color:var(--muted);margin-top:8px">Se o player nao carregar aqui, <a id="lv_link" href="#" target="_blank" rel="noopener" style="color:var(--accent)">abra em nova aba</a>.</div>
</div></div>

<div class="ov" id="ov"><div class="modal" style="max-width:640px"><h2>IA - <span id="a_nome"></span></h2><input type="hidden" id="a_id">
 <label style="display:flex;gap:8px;align-items:center;margin-bottom:10px;cursor:pointer"><input type="checkbox" id="a_ativo" style="width:auto" checked> <span>Ativo (desmarcado = nada roda nesta camera)</span></label>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:4px 0 6px">Modulos de IA (cada um soma na fatura)</div>
 <div id="modbox"></div>
 <div id="a_placa_warn" style="display:none;color:var(--bad);font-size:12px;margin:6px 0">Obs: Placa/LPR so dispara em camera marcada como "de entrada" pela Corexia.</div>
 <div style="color:var(--muted);font-size:12px;margin:6px 0 8px">Zona de intrusao / linha / mapa de calor / piscina precisam da area desenhada — fale com a Corexia p/ configurar as zonas.</div>
 <label style="display:flex;gap:8px;align-items:center;margin:8px 0;cursor:pointer"><input type="checkbox" id="a_sched" style="width:auto" onchange="toggleSched()"> <span>Restringir por horario (senao roda 24h)</span></label>
 <div id="schedbox" style="display:none">
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
   <label class="ck"><input type="checkbox" id="a_d1" checked> Seg</label><label class="ck"><input type="checkbox" id="a_d2" checked> Ter</label>
   <label class="ck"><input type="checkbox" id="a_d3" checked> Qua</label><label class="ck"><input type="checkbox" id="a_d4" checked> Qui</label>
   <label class="ck"><input type="checkbox" id="a_d5" checked> Sex</label><label class="ck"><input type="checkbox" id="a_d6"> Sab</label>
   <label class="ck"><input type="checkbox" id="a_d7"> Dom</label></div>
  <div class="two"><div class="fld"><label>Hora inicio</label><input id="a_ini" type="time" value="08:00"></div>
   <div class="fld"><label>Hora fim</label><input id="a_fim" type="time" value="18:00"></div></div>
 </div>
 <div style="text-align:right;font-size:13px;margin-top:8px">Custo de IA desta camera: <b id="a_custo" style="color:var(--accent)">R$ 0,00</b>/mes</div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button id="a_limpar" onclick="limpar()" style="color:var(--bad)">Desligar IA</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>

<div class="ov" id="ovg"><div class="modal" style="max-width:460px"><h2>Gravacao em nuvem - <span id="g_nome"></span></h2><input type="hidden" id="g_id">
 <div class="fld"><label>Dias de gravacao (retencao na nuvem Corexia)</label><select id="g_dias" onchange="gravCusto()"></select></div>
 <div style="font-size:13px;color:var(--muted)">Custo desta camera: <b id="g_custo" style="color:var(--accent)">R$ 0,00</b>/mes</div>
 <div class="foot"><button onclick="document.getElementById('ovg').classList.remove('open')">Cancelar</button><button class="btn-primary" onclick="salvarGrav()">Salvar</button></div></div></div>

<div class="ov" id="ovloc"><div class="modal" style="max-width:520px"><h2>Localizacao - <span id="loc_nome"></span></h2><input type="hidden" id="loc_id">
 <div class="fld"><label>CEP (preenche endereco e mapa automaticamente)</label><input id="loc_cep" placeholder="00000-000" onblur="buscaCep()"><div id="loc_cepst" style="font-size:12px;color:var(--muted);margin-top:4px"></div></div>
 <div class="fld"><label>Endereco</label><input id="loc_end"></div>
 <div class="two"><div class="fld"><label>Bairro</label><input id="loc_bairro"></div><div class="fld"><label>Cidade</label><input id="loc_cidade"></div></div>
 <div class="two"><div class="fld"><label>UF</label><input id="loc_uf"></div><div class="fld"><label>&nbsp;</label><div style="font-size:12px;color:var(--muted);padding-top:10px">lat/long vem do CEP (ajustavel)</div></div></div>
 <div class="two"><div class="fld"><label>Latitude</label><input id="loc_lat"></div><div class="fld"><label>Longitude</label><input id="loc_lng"></div></div>
 <div class="foot"><button onclick="document.getElementById('ovloc').classList.remove('open')">Cancelar</button><button class="btn-primary" onclick="salvarLocal()">Salvar localizacao</button></div></div></div>

<div class="ov" id="ovnew"><div class="modal" style="max-width:640px"><h2 id="nmt">Adicionar camera</h2><input type="hidden" id="n_editid">
 <div class="fld"><label>Nome da camera</label><input id="n_nome" placeholder="Ex.: Portaria - Entrada"></div>
 <div class="fld"><label>CEP (preenche endereco e mapa automaticamente)</label><input id="n_cep" placeholder="00000-000" onblur="nBuscaCep()"><div id="n_cepst" style="font-size:12px;color:var(--muted);margin-top:4px"></div></div>
 <div class="fld"><label>Endereco</label><input id="n_end"></div>
 <div class="two"><div class="fld"><label>Bairro</label><input id="n_bairro"></div><div class="fld"><label>Cidade</label><input id="n_cidade"></div></div>
 <div class="two"><div class="fld"><label>UF</label><input id="n_uf"></div><div class="fld"><label>Fuso horario</label><select id="n_fuso"><option value="America/Sao_Paulo">Brasil (Brasilia - America/Sao_Paulo)</option><option value="America/Manaus">America/Manaus</option><option value="America/Rio_Branco">America/Rio_Branco (Acre)</option><option value="America/Noronha">America/Noronha</option></select></div></div>
 <div class="two"><div class="fld"><label>Latitude</label><input id="n_lat"></div><div class="fld"><label>Longitude</label><input id="n_lng"></div></div>
 <div class="two"><div class="fld"><label>Usuario da camera</label><input id="n_user"></div><div class="fld"><label>Senha da camera</label><input id="n_pass" type="password"></div></div>
 <div class="two"><div class="fld"><label>Protocolo</label><select id="n_proto" onchange="nProto()"><option value="rtmp">RTMP (a camera transmite para nos)</option><option value="rtsp">RTSP (nos puxamos da camera)</option></select></div>
  <div class="fld"><label>Gravar audio?</label><select id="n_audio"><option value="nao">Nao</option><option value="sim">Sim</option></select></div></div>
 <div class="fld" id="n_rtspbox" style="display:none"><label>Link RTSP da camera</label><input id="n_rtsp" placeholder="rtsp://usuario:senha@ip:554/..."></div>
 <label class="ck" style="margin:6px 0"><input type="checkbox" id="n_pub" checked> Link publico (permite incorporar a camera em outros sites)</label>
 <div id="n_addhint" style="font-size:12px;color:var(--muted);margin-bottom:6px">Ao salvar, geramos o <b>link RTMP</b> (vai dentro da camera) e o <b>link de incorporacao</b> (embed).</div>
 <div id="n_links" style="display:none;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px">
  <div style="font-size:11px;color:var(--muted);margin-bottom:3px">Link RTMP (vai dentro da camera):</div><input id="n_rtmp_show" readonly onclick="this.select()" style="font-family:var(--mono);font-size:12px"><div style="margin:2px 0 6px"><button class="act" style="color:var(--accent)" onclick="cp('n_rtmp_show')">copiar RTMP</button></div>
  <div style="font-size:11px;color:var(--muted);margin-bottom:3px">Link de incorporacao (embed):</div><input id="n_embed_show" readonly onclick="this.select()" style="font-family:var(--mono);font-size:12px"><div style="margin-top:2px"><button class="act" style="color:var(--accent)" onclick="cp('n_embed_show')">copiar embed</button></div>
 </div>
 <div class="foot"><button onclick="document.getElementById('ovnew').classList.remove('open')">Cancelar</button><button class="btn-primary" id="nsave" onclick="salvarNova()">Criar camera</button></div></div></div>

<div class="ov" id="ovres"><div class="modal" style="max-width:660px"><h2>Camera criada!</h2>
 <div class="fld"><label>Link RTMP - coloque este link DENTRO da camera (e o que faz ela transmitir pro servidor)</label><input id="r_rtmp" readonly onclick="this.select()" style="font-family:var(--mono)"><div style="margin-top:4px"><button class="act" style="color:var(--accent)" onclick="cp('r_rtmp')">copiar link RTMP</button></div></div>
 <div class="fld"><label>Codigo de incorporacao (embed) - cole em outro site</label><textarea id="r_embed" readonly rows="3" onclick="this.select()" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:10px;font-family:var(--mono);font-size:12px"></textarea><div style="margin-top:4px"><button class="act" style="color:var(--accent)" onclick="cp('r_embed')">copiar embed</button> <a id="r_open" href="#" target="_blank" rel="noopener" class="act" style="color:var(--accent)">abrir player</a></div></div>
 <div style="font-size:12px;color:var(--muted)">A camera aparece no mural e no mapa. Quando comecar a transmitir pelo link RTMP, o player mostra a imagem ao vivo.</div>
 <div class="foot"><button class="btn-primary" onclick="document.getElementById('ovres').classList.remove('open')">Fechar</button></div></div></div>

<div class="ov" id="ovdel"><div class="modal" style="max-width:480px"><h2>Excluir camera - <span id="d_nome"></span></h2><input type="hidden" id="d_id">
 <div style="color:var(--muted);font-size:13px;margin-bottom:8px">A exclusao passa por <b>aprovacao da equipe Corexia</b>. Informe o motivo (obrigatorio).</div>
 <div class="fld"><label>Motivo da exclusao</label><textarea id="d_motivo" rows="3" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:10px;font-size:14px"></textarea></div>
 <div class="foot"><button onclick="document.getElementById('ovdel').classList.remove('open')">Cancelar</button><button class="btn-primary" style="background:var(--bad);border-color:var(--bad);color:#1a0505" onclick="salvarExclusao()">Solicitar exclusao</button></div></div></div>

<style>.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}
.camcard{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.camthumb{position:relative;padding-top:56.25%;background:#0b0d12}
.camthumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.camthumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}
.modrow{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)}
.mprice{color:var(--muted);font-size:12px;font-family:var(--mono);white-space:nowrap}
.subbox{margin:0 0 8px 22px;padding:8px 10px;background:var(--surface2);border-radius:8px}
.subnote{font-size:12px;color:var(--muted);margin-bottom:6px}
.subgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px}</style>
<script>
var DATA={cameras:[],modulos:[],grav_tiers:[],gravacao:'',plano_nome:''}; var CAMS=[]; window.PAGE_INIT=reload;
function iaMap(){ var m={}; DATA.modulos.forEach(function(mod){mod.analiticos.forEach(function(a){m[a[0]]=mod.key;});}); return m; }
function ativosDe(cfg){ var s={}; if(!cfg)return s; (cfg.analiticos_padrao||[]).forEach(function(a){s[a]=1}); (cfg.horarios||[]).forEach(function(h){(h.analiticos||[]).forEach(function(a){s[a]=1})}); return s; }
function modsAtivos(cfg){ var s=ativosDe(cfg); var mp=iaMap(); var o={}; Object.keys(s).forEach(function(a){ if(mp[a])o[mp[a]]=1; }); return Object.keys(o); }
function modNome(){ var m={}; DATA.modulos.forEach(function(x){m[x.key]=x.nome;}); return m; }
async function reload(){ var _g=$('grid'); try{ DATA=await api('GET','/api/comercial/prov/cameras'); CAMS=DATA.cameras||[]; buildMods(); render(); loadCusto(); }catch(e){ var m=(e&&e.message)?e.message:(''+e); if(_g)_g.innerHTML='<div class="center" style="color:var(--bad)">Erro ao carregar: '+m+'</div>'; msg('Erro: '+m); } }
async function loadCusto(){ try{ renderCusto(await api('GET','/api/comercial/prov/custo')); }catch(e){} }
function renderCusto(c){
 var h='<div class="kpi"><div class="k">Plano</div><div class="v" style="font-size:15px">'+esc(c.plano_nome||'(sem plano)')+'</div></div>'+
  '<div class="kpi"><div class="k">Painel</div><div class="v">'+brl(c.painel)+'</div></div>'+
  '<div class="kpi"><div class="k">IA</div><div class="v">'+brl(c.ia_total)+'</div></div>'+
  (c.gravacao==='cloud'?'<div class="kpi"><div class="k">Gravacao</div><div class="v">'+brl(c.grav_total)+'</div></div>':'')+
  '<div class="kpi" style="border-color:var(--accent)"><div class="k">Total estimado/mes</div><div class="v" style="color:var(--accent)">'+brl(c.total)+'</div></div>';
 $('planobar').innerHTML=h;
 var base = c.gravacao==='local' ? 'Plano LOCAL: gravacao no seu servidor (sem custo). Voce paga painel + IA ativada.' :
            c.gravacao==='cloud' ? 'Plano CLOUD: gravacao na nuvem Corexia (por dias/camera) + IA ativada.' :
            'Provedor sem plano definido - fale com a Corexia.';
 $('planohint').textContent = c.painel_desc ? (base+' — '+c.painel_desc) : base;
}
function render(){ var q=($('q').value||'').toLowerCase(); var MN=modNome();
 var arr=CAMS.filter(function(c){return !q||(c.nome||'').toLowerCase().indexOf(q)>=0;});
 if(!arr.length){ $('grid').innerHTML='<div class="center" style="grid-column:1/-1">'+(CAMS.length?'Nenhuma camera encontrada.':'Nenhuma camera atribuida a voce ainda - fale com a Corexia.')+'</div>'; return; }
 $('grid').innerHTML=arr.map(function(c){ var mods=modsAtivos(c.config); var on=c.config&&c.config.ativo&&mods.length>0;
  var iaBadge=on?'<span class="pill ok" style="font-size:10px">IA on</span>':'<span class="pill off" style="font-size:10px">IA off</span>';
  var stOn=(c.status||'').toLowerCase()==='online';
  var thumb='<div class="ph">sem imagem</div><img src="/camthumb/'+c.id+'?t='+encodeURIComponent(TOKEN)+'" loading="lazy" onerror="this.style.display=\\'none\\'">';
  var chips=mods.slice(0,3).map(function(k){return '<span class="pill ok" style="font-size:10px">'+esc(MN[k]||k)+'</span>';}).join(' ')+(mods.length>3?' <span class="pill off" style="font-size:10px">+'+(mods.length-3)+'</span>':'');
  var grav=''; if(DATA.gravacao==='cloud'){ var d=c.dias_gravacao||0; grav='<button class="act" style="color:var(--accent)" onclick="grav(\\''+c.id+'\\')">'+(d>0?('gravacao: '+d+'d'):'gravacao')+'</button>'; }
   else if(DATA.gravacao==='local'){ grav='<span style="color:var(--muted);font-size:11px;align-self:center">grav. local</span>'; }
  var del=c.exclusao_pendente ? '<span class="pill" style="color:var(--bad);border-color:rgba(248,113,113,.4);font-size:10px;align-self:center">exclusao pendente</span>' : '<button class="act" style="color:var(--bad)" onclick="pedirExclusao(\\''+c.id+'\\')">excluir</button>';
  return '<div class="camcard"><div class="camthumb">'+thumb+
   '<span class="pill '+(stOn?'ok':'off')+'" style="position:absolute;top:8px;left:8px;font-size:10px;background:rgba(0,0,0,.55)">'+esc(stOn?'online':(c.status||'offline'))+'</span>'+
   '<span style="position:absolute;top:8px;right:8px">'+iaBadge+'</span></div>'+
   '<div style="padding:10px 12px"><div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(c.nome||'-')+'</div>'+
   '<div style="color:var(--muted);font-size:12px;margin:2px 0 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(c.cliente_nome||'sem cliente')+'</div>'+
   '<div style="min-height:20px;margin-bottom:8px">'+(chips.trim()||'<span style="color:var(--muted);font-size:12px">sem IA</span>')+'</div>'+
   '<div style="display:flex;gap:6px;flex-wrap:wrap"><button class="act" style="color:var(--accent)" onclick="ver(\\''+c.id+'\\')">ver ao vivo</button><button class="act" onclick="editarCam(\\''+c.id+'\\')">editar</button><button class="act" onclick="conf(\\''+c.id+'\\')">configurar IA</button><button class="act" onclick="editLocal(\\''+c.id+'\\')">local</button>'+grav+del+'</div></div></div>'; }).join(''); }
function ver(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return; if(!c.embed_url){msg('Camera sem link de player.');return;}
 $('lv_nome').textContent=c.nome||''; $('lv_frame').src=c.embed_url; $('lv_link').href=c.embed_url; $('ovlive').classList.add('open'); }
function fecharLive(){ $('ovlive').classList.remove('open'); $('lv_frame').src='about:blank'; }
function toggleSched(){ $('schedbox').style.display=$('a_sched').checked?'block':'none'; }
function buildMods(){ var h=''; DATA.modulos.forEach(function(mod){
  h+='<div class="modrow"><label class="ck" style="font-weight:600"><input type="checkbox" id="m_'+mod.key+'" onchange="onMod(\\''+mod.key+'\\')"> '+esc(mod.nome)+'</label><span class="mprice">'+brl(mod.valor)+'/mes</span></div>';
  if(mod.pacote){ h+='<div id="sub_'+mod.key+'" class="subbox" style="display:none"><div class="subnote" id="note_'+mod.key+'">Recomendado no maximo 3 topicos por camera (ex.: arma de fogo / faca / toca ninja).</div><div class="subgrid">';
   mod.analiticos.forEach(function(a){ h+='<label class="ck"><input type="checkbox" id="d_'+a[0]+'" onchange="onSub(\\''+mod.key+'\\')"> '+esc(a[1])+'</label>'; });
   h+='</div></div>'; } });
 $('modbox').innerHTML=h; }
function onMod(k){ var mod=DATA.modulos.filter(function(m){return m.key===k})[0];
 if(mod&&mod.pacote){ var sb=$('sub_'+k); if(sb)sb.style.display=$('m_'+k).checked?'block':'none'; }
 computeCusto(); }
function onSub(k){ var mod=DATA.modulos.filter(function(m){return m.key===k})[0]; if(!mod)return;
 var n=mod.analiticos.filter(function(a){var e=$('d_'+a[0]);return e&&e.checked;}).length;
 var note=$('note_'+k); if(note){ note.style.color=(n>3?'var(--bad)':'var(--muted)'); note.textContent=(n>3?('Voce marcou '+n+' topicos. '):'')+'Recomendado no maximo 3 por camera (ex.: arma de fogo / faca / toca ninja).'; }
 computeCusto(); }
function computeCusto(){ var t=0; DATA.modulos.forEach(function(mod){ var mc=$('m_'+mod.key); if(!mc||!mc.checked)return;
  if(mod.pacote){ if(mod.analiticos.some(function(a){var e=$('d_'+a[0]);return e&&e.checked;}))t+=mod.valor; } else t+=mod.valor; });
 $('a_custo').textContent=brl(t); }
function conf(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return; var cfg=c.config; var aset=ativosDe(cfg);
 $('a_id').value=id; $('a_nome').textContent=c.nome||''; $('a_ativo').checked=cfg?!!cfg.ativo:true;
 DATA.modulos.forEach(function(mod){ var active=mod.analiticos.some(function(a){return aset[a[0]];});
  var mc=$('m_'+mod.key); if(mc)mc.checked=active;
  if(mod.pacote){ mod.analiticos.forEach(function(a){var e=$('d_'+a[0]); if(e)e.checked=!!aset[a[0]];}); var sb=$('sub_'+mod.key); if(sb)sb.style.display=active?'block':'none'; onSub(mod.key); } });
 $('a_placa_warn').style.display=c.ia_placa?'none':'block';
 var hs=(cfg&&cfg.horarios)||[]; var sched=hs.length>0; $('a_sched').checked=sched; toggleSched();
 if(sched){ var h=hs[0]; [1,2,3,4,5,6,7].forEach(function(d){$('a_d'+d).checked=(h.dias||[]).indexOf(d)>=0;}); $('a_ini').value=h.hora_inicio||'08:00'; $('a_fim').value=h.hora_fim||'18:00'; }
 $('a_limpar').style.display=cfg?'inline-block':'none'; computeCusto(); $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var c=CAMS.filter(function(x){return x.id===$('a_id').value})[0]; if(!c)return; var ana=[];
 DATA.modulos.forEach(function(mod){ var mc=$('m_'+mod.key); if(!mc||!mc.checked)return;
  if(mod.pacote){ mod.analiticos.forEach(function(a){var e=$('d_'+a[0]); if(e&&e.checked)ana.push(a[0]);}); }
  else { mod.analiticos.forEach(function(a){ana.push(a[0]);}); } });
 var body={ camera_id:c.id, camera_nome:c.nome||'', ativo:$('a_ativo').checked, zonas_intrusao:(c.config&&c.config.zonas_intrusao)||[] };
 if($('a_sched').checked){ var dias=[1,2,3,4,5,6,7].filter(function(d){return $('a_d'+d).checked}); if(!dias.length)dias=[1,2,3,4,5,6,7];
  body.horarios=[{label:'Personalizado',dias:dias,hora_inicio:$('a_ini').value||'00:00',hora_fim:$('a_fim').value||'23:59',analiticos:ana}]; body.analiticos_padrao=[]; }
 else { body.horarios=[]; body.analiticos_padrao=ana; }
 try{ await api('POST','/api/comercial/prov/analiticos/salvar',body); fecha(); msg('IA salva. Vale em ~2 min.',true); reload(); }catch(e){ msg('Erro ao salvar: '+e.message); } }
async function limpar(){ if(!confirm('Desligar a IA desta camera?'))return;
 try{ await api('POST','/api/comercial/prov/analiticos/limpar',{camera_id:$('a_id').value}); fecha(); msg('IA desligada.',true); reload(); }catch(e){ msg('Erro: '+e.message); } }
function grav(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return;
 $('g_id').value=id; $('g_nome').textContent=c.nome||'';
 $('g_dias').innerHTML='<option value="0">Nao gravar</option>'+DATA.grav_tiers.map(function(t){return '<option value="'+t.dias+'">'+t.dias+' dia'+(t.dias>1?'s':'')+' - '+brl(t.valor)+'/mes</option>';}).join('');
 $('g_dias').value=String(c.dias_gravacao||0); gravCusto(); $('ovg').classList.add('open'); }
function gravCusto(){ var d=parseInt($('g_dias').value||0,10); var v=0; DATA.grav_tiers.forEach(function(t){ if(d>0&&v===0&&d<=t.dias)v=t.valor; }); if(d>0&&v===0&&DATA.grav_tiers.length)v=DATA.grav_tiers[DATA.grav_tiers.length-1].valor; $('g_custo').textContent=brl(v); }
async function salvarGrav(){ var id=$('g_id').value; var d=parseInt($('g_dias').value||0,10);
 try{ await api('POST','/api/comercial/prov/cameras/'+id+'/gravacao',{dias:d}); $('ovg').classList.remove('open'); msg('Gravacao atualizada.',true); reload(); }catch(e){ msg('Erro: '+e.message); } }
function editLocal(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return;
 $('loc_id').value=id; $('loc_nome').textContent=c.nome||''; $('loc_cepst').textContent='';
 $('loc_cep').value=c.cep||''; $('loc_end').value=c.endereco||''; $('loc_bairro').value=c.bairro||''; $('loc_cidade').value=c.cidade||''; $('loc_uf').value=c.uf||'';
 $('loc_lat').value=(c.latitude!=null?c.latitude:''); $('loc_lng').value=(c.longitude!=null?c.longitude:'');
 $('ovloc').classList.add('open'); }
async function buscaCep(){ var cep=($('loc_cep').value||'').replace(/\\D/g,''); if(cep.length!==8)return;
 $('loc_cepst').textContent='buscando...'; try{ var r=await api('GET','/api/comercial/geocode?cep='+cep);
  $('loc_end').value=r.logradouro||$('loc_end').value; $('loc_bairro').value=r.bairro||''; $('loc_cidade').value=r.cidade||''; $('loc_uf').value=r.uf||'';
  if(r.latitude!=null){ $('loc_lat').value=r.latitude; $('loc_lng').value=r.longitude; $('loc_cepst').textContent='Endereco e coordenadas preenchidos.'; }
  else { $('loc_cepst').textContent='Endereco preenchido; coordenadas nao encontradas (ajuste manual se quiser).'; }
 }catch(e){ $('loc_cepst').textContent='Erro: '+(e&&e.message||e); } }
async function salvarLocal(){ var b={ cep:$('loc_cep').value.trim(), endereco:$('loc_end').value.trim(), bairro:$('loc_bairro').value.trim(), cidade:$('loc_cidade').value.trim(), uf:$('loc_uf').value.trim(), latitude:$('loc_lat').value.trim(), longitude:$('loc_lng').value.trim() };
 try{ await api('POST','/api/comercial/prov/cameras/'+$('loc_id').value+'/local',b); $('ovloc').classList.remove('open'); msg('Localizacao salva.',true); reload(); }catch(e){ msg('Erro: '+(e&&e.message||e)); } }
function novaCam(){ ['n_nome','n_cep','n_end','n_bairro','n_cidade','n_uf','n_lat','n_lng','n_user','n_pass','n_rtsp'].forEach(function(i){$(i).value='';}); $('n_cepst').textContent=''; $('n_proto').value='rtmp'; $('n_audio').value='nao'; $('n_fuso').value='America/Sao_Paulo'; $('n_pub').checked=true; $('n_editid').value=''; $('nmt').textContent='Adicionar camera'; $('nsave').textContent='Criar camera'; $('n_links').style.display='none'; $('n_addhint').style.display='block'; $('n_pass').placeholder=''; nProto(); $('ovnew').classList.add('open'); }
function editarCam(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return;
 $('n_editid').value=id; $('nmt').textContent='Editar camera'; $('nsave').textContent='Salvar alteracoes'; $('n_addhint').style.display='none'; $('n_cepst').textContent='';
 $('n_nome').value=c.nome||''; $('n_cep').value=c.cep||''; $('n_end').value=c.endereco||''; $('n_bairro').value=c.bairro||''; $('n_cidade').value=c.cidade||''; $('n_uf').value=c.uf||'';
 $('n_lat').value=(c.latitude!=null?c.latitude:''); $('n_lng').value=(c.longitude!=null?c.longitude:'');
 $('n_user').value=c.usuario||''; $('n_pass').value=''; $('n_pass').placeholder='(em branco = manter a atual)';
 $('n_proto').value=c.protocolo||'rtmp'; $('n_audio').value=(c.grava_audio?'sim':'nao'); $('n_fuso').value=c.fuso||'America/Sao_Paulo'; $('n_pub').checked=(c.publico!==false); $('n_rtsp').value=c.rtsp_src||'';
 $('n_rtmp_show').value=c.rtmp_ingest||''; $('n_embed_show').value=c.embed_url||'(link publico desativado)'; $('n_links').style.display='block';
 nProto(); $('ovnew').classList.add('open'); }
function nProto(){ $('n_rtspbox').style.display=($('n_proto').value==='rtsp')?'block':'none'; }
async function nBuscaCep(){ var cep=($('n_cep').value||'').replace(/\\D/g,''); if(cep.length!==8)return; $('n_cepst').textContent='buscando...';
 try{ var r=await api('GET','/api/comercial/geocode?cep='+cep); $('n_end').value=r.logradouro||''; $('n_bairro').value=r.bairro||''; $('n_cidade').value=r.cidade||''; $('n_uf').value=r.uf||'';
  if(r.latitude!=null){ $('n_lat').value=r.latitude; $('n_lng').value=r.longitude; $('n_cepst').textContent='Endereco e coordenadas preenchidos.'; } else { $('n_cepst').textContent='Endereco preenchido; sem coordenadas (ajuste manual se quiser).'; }
 }catch(e){ $('n_cepst').textContent='Erro: '+(e&&e.message||e); } }
async function salvarNova(){ var b={ nome:$('n_nome').value.trim(), cep:$('n_cep').value.trim(), endereco:$('n_end').value.trim(), bairro:$('n_bairro').value.trim(), cidade:$('n_cidade').value.trim(), uf:$('n_uf').value.trim(), latitude:$('n_lat').value.trim(), longitude:$('n_lng').value.trim(), usuario:$('n_user').value.trim(), senha:$('n_pass').value, protocolo:$('n_proto').value, grava_audio:($('n_audio').value==='sim'), fuso:$('n_fuso').value, publico:$('n_pub').checked, rtsp_url:$('n_rtsp').value.trim() };
 if(!b.nome){ msg('Informe o nome da camera.'); return; }
 var eid=$('n_editid').value;
 try{
  if(eid){ await api('POST','/api/comercial/prov/cameras/'+eid+'/editar',b); $('ovnew').classList.remove('open'); msg('Camera atualizada.',true); reload(); return; }
  var r=await api('POST','/api/comercial/prov/cameras/criar',b); $('ovnew').classList.remove('open');
  $('r_rtmp').value=r.rtmp_ingest||'';
  if(r.embed_url){ $('r_embed').value='<iframe src="'+r.embed_url+'?autoplay=true" style="width:100%;height:100%;border:0" allow="autoplay; fullscreen" allowfullscreen></iframe>'; $('r_open').href=r.embed_url; }
  else { $('r_embed').value='(link publico desativado nesta camera)'; $('r_open').href='#'; }
  $('ovres').classList.add('open'); reload();
 }catch(e){ msg('Erro ao salvar: '+(e&&e.message||e)); } }
function cp(id){ var el=$(id); if(!el)return; el.select(); try{ document.execCommand('copy'); msg('Copiado.',true); }catch(e){ msg('Selecione e copie manual.'); } }
function pedirExclusao(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return; $('d_id').value=id; $('d_nome').textContent=c.nome||''; $('d_motivo').value=''; $('ovdel').classList.add('open'); }
async function salvarExclusao(){ var mot=$('d_motivo').value.trim(); if(!mot){ msg('Informe o motivo da exclusao.'); return; }
 try{ await api('POST','/api/comercial/prov/cameras/'+$('d_id').value+'/excluir-solicitar',{motivo:mot}); $('ovdel').classList.remove('open'); msg('Pedido enviado. Aguardando autorizacao da equipe Corexia.',true); reload(); }catch(e){ msg('Erro: '+(e&&e.message||e)); } }
</script>
"""


_PROV_ALERTAS_BODY = """
<div id="msg" class="msg"></div>
<div class="cards" style="margin-bottom:10px">
 <div class="kpi"><div class="k">Novos</div><div class="v" id="k_novos" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Hoje</div><div class="v" id="k_hoje">-</div></div>
 <div class="kpi"><div class="k">Total</div><div class="v" id="k_total">-</div></div>
</div>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
 <input id="q" placeholder="Buscar camera/cliente..." oninput="render()" style="flex:1;min-width:180px;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px">
 <select id="f_tipo" onchange="load()" style="background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 10px"></select>
 <select id="f_status" onchange="load()" style="background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 10px">
  <option value="">Todos status</option><option value="novo">Novos</option><option value="visualizado">Vistos</option><option value="resolvido">Resolvidos</option></select>
 <button onclick="load()">Atualizar</button>
 <button onclick="marcarTodos()">Marcar todos vistos</button>
 <label class="ck" style="font-size:12px"><input type="checkbox" id="auto" checked> auto</label>
 <label class="ck" style="font-size:12px"><input type="checkbox" id="som"> som</label>
</div>
<table><thead><tr><th style="width:70px">Foto</th><th>Camera / Cliente</th><th>Tipo</th><th>Conf.</th><th>Quando</th><th>Status</th><th></th></tr></thead>
<tbody id="rows"><tr><td colspan="7" class="center">carregando...</td></tr></tbody></table>

<div class="ov" id="ovimg"><div class="modal" style="max-width:760px;width:100%">
 <h2 style="display:flex;align-items:center;gap:10px"><span id="im_tit"></span><span style="flex:1"></span><button onclick="$('ovimg').classList.remove('open')">Fechar</button></h2>
 <img id="im_full" src="" alt="" style="width:100%;border-radius:8px;background:#000"></div></div>

<style>.ck{display:flex;gap:6px;align-items:center;color:var(--ink);cursor:pointer}.ck input{width:auto}
.athumb{width:60px;height:40px;object-fit:cover;border-radius:6px;background:#0b0d12;cursor:pointer}
.tpill{font-size:11px;font-family:var(--mono);padding:2px 8px;border-radius:999px;border:1px solid var(--border)}</style>
<script>
var ALERTAS=[], LAST_TOP=null, TIMER=null; window.PAGE_INIT=init;
var TLAB={fogo:'Fogo/Fumaca',arma_fogo:'Arma de fogo',arma_branca:'Arma branca',arma:'Arma',intruso:'Intrusao',linha:'Linha virtual',aglomeracao:'Aglomeracao',placa:'Placa',movimento:'Movimento',pessoa:'Pessoa',veiculo:'Veiculo',animal:'Animal',epi:'EPI',heatmap:'Mapa de calor',toca_ninja:'Toca ninja',piscina:'Piscina',outro:'Outro'};
var PERIGO={fogo:1,arma_fogo:1,arma_branca:1,arma:1,intruso:1,piscina:1,toca_ninja:1};
function dt(s){ s=(''+(s||'')); var d=s.slice(0,10).split('-'); var t=s.slice(11,16); return d.length===3?(d[2]+'/'+d[1]+' '+t):s; }
function stPill(s){ var m={novo:['Novo','','color:var(--accent);border-color:var(--accent)'],visualizado:['Visto','off',''],resolvido:['Resolvido','ok','']}[s]||['?','off','']; return '<span class="pill '+m[1]+'" style="'+m[2]+'">'+m[0]+'</span>'; }
function init(){ var s=$('f_tipo'); s.innerHTML='<option value="">Todos tipos</option>'+Object.keys(TLAB).map(function(k){return '<option value="'+k+'">'+esc(TLAB[k])+'</option>';}).join('');
 load(); TIMER=setInterval(function(){ if($('auto').checked) load(); }, 20000); }
async function load(){ try{
  var qs='?limit=300'; var t=$('f_tipo').value, st=$('f_status').value; if(t)qs+='&tipo='+t; if(st)qs+='&status='+st;
  var a=await api('GET','/api/comercial/prov/alertas'+qs);
  var r=await api('GET','/api/comercial/prov/alertas/resumo');
  $('k_novos').textContent=r.novos; $('k_hoje').textContent=r.hoje; $('k_total').textContent=r.total;
  var top=a[0];
  if(top && LAST_TOP && top.id!==LAST_TOP && top.status==='novo'){ beep(); fala('Novo alerta: '+(TLAB[top.tipo]||top.tipo)+' na camera '+(top.camera_nome||'')); }
  if(top) LAST_TOP=top.id;
  ALERTAS=a; render();
 }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=ALERTAS.filter(function(x){ return !q || ((''+(x.camera_nome||'')+' '+(x.cliente_nome||'')).toLowerCase().indexOf(q)>=0); });
 if(!arr.length){ $('rows').innerHTML='<tr><td colspan="7" class="center">Nenhum alerta'+(ALERTAS.length?' com esse filtro':' ainda')+'.</td></tr>'; return; }
 $('rows').innerHTML=arr.map(function(x){
  var img=x.imagem_url?('<img class="athumb" src="/api/comercial/prov/alerta/'+x.id+'/img?t='+encodeURIComponent(TOKEN)+'&w=200" onclick="verFoto(\\''+x.id+'\\')" onerror="this.style.visibility=\\'hidden\\'">'):'<span style="color:var(--muted);font-size:11px">-</span>';
  var cor=PERIGO[x.tipo]?'color:var(--bad);border-color:rgba(248,113,113,.4)':'';
  var acts='';
  if(x.status==='novo') acts+='<button class="act" onclick="setSt(\\''+x.id+'\\',\\'visualizado\\')">visto</button>';
  if(x.status!=='resolvido') acts+='<button class="act" style="color:var(--ok)" onclick="setSt(\\''+x.id+'\\',\\'resolvido\\')">resolver</button>';
  return '<tr'+(x.status==='novo'?' style="background:rgba(249,115,22,.05)"':'')+'><td>'+img+'</td>'+
   '<td><b>'+esc(x.camera_nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(x.cliente_nome||'sem cliente')+'</div></td>'+
   '<td><span class="tpill" style="'+cor+'">'+esc(TLAB[x.tipo]||x.tipo||'-')+'</span></td>'+
   '<td class="money">'+(x.confianca||0)+'%</td><td style="white-space:nowrap;font-size:13px">'+dt(x.criado)+'</td>'+
   '<td>'+stPill(x.status)+(x.whatsapp_enviado?' <span class="pill ok" style="font-size:10px">zap</span>':'')+'</td>'+
   '<td style="text-align:right;white-space:nowrap">'+acts+'</td></tr>'; }).join(''); }
function verFoto(id){ var x=ALERTAS.filter(function(a){return a.id===id})[0]; if(!x||!x.imagem_url)return;
 $('im_tit').textContent=(TLAB[x.tipo]||x.tipo)+' - '+(x.camera_nome||''); $('im_full').src='/api/comercial/prov/alerta/'+x.id+'/img?t='+encodeURIComponent(TOKEN);
 $('ovimg').classList.add('open'); if(x.status==='novo') setSt(id,'visualizado',true); }
async function setSt(id,st,silent){ try{ await api('POST','/api/comercial/prov/alertas/'+id+'/status',{status:st});
  var x=ALERTAS.filter(function(a){return a.id===id})[0]; if(x)x.status=st; render(); if(!silent)msg('Alerta '+st+'.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function marcarTodos(){ if(!confirm('Marcar todos os alertas novos como vistos?'))return;
 try{ var r=await api('POST','/api/comercial/prov/alertas/marcar-vistos'); msg((r.marcados||0)+' marcados como vistos.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
function beep(){ try{ var A=window.AudioContext||window.webkitAudioContext; if(!A)return; var a=new A(); var o=a.createOscillator(),g=a.createGain(); o.connect(g); g.connect(a.destination); o.type='sine'; o.frequency.value=880; g.gain.value=0.08; o.start(); setTimeout(function(){o.stop();a.close();},350);}catch(e){} }
function fala(txt){ try{ if($('som').checked && window.speechSynthesis){ var u=new SpeechSynthesisUtterance(txt); u.lang='pt-BR'; window.speechSynthesis.speak(u);} }catch(e){} }
</script>
"""


_PROV_PLANOS_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><div style="flex:1"></div><button onclick="modelos()">+ Criar modelos padrao</button><button class="btn-primary" onclick="novo()">+ Novo plano</button></div>
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Crie os planos que voce oferece aos SEUS clientes e defina o valor que voce cobra. Ao cadastrar um cliente, voce escolhe um destes planos.</p>
<table><thead><tr><th>Plano</th><th>Totem</th><th>Cameras</th><th>Contrato</th><th>Monit.</th><th>Valor/mes</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="8" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:640px"><h2 id="mt">Novo Plano</h2><input type="hidden" id="p_id">
 <div class="grid2">
  <div class="fld"><label>Nome do Plano *</label><input id="p_nome" placeholder="Ex.: Plano 4 Cameras"></div>
  <div class="fld"><label>Numero de Cameras *</label><select id="p_cams"></select></div>
 </div>
 <div class="fld"><label>Descricao</label><textarea id="p_desc" rows="2" placeholder="Descreva o plano..."></textarea></div>
 <div class="grid3">
  <div class="fld"><label>Valor Mensal (R$) *</label><input id="p_valor" type="number" step="0.01" placeholder="0,00"></div>
  <div class="fld"><label>Contrato (meses) *</label><input id="p_meses" type="number" value="36"></div>
  <div class="fld"><label>Tipo Documento</label><select id="p_doc"><option value="ambos">Ambos</option><option value="cnpj">CNPJ</option><option value="cpf">CPF</option></select></div>
 </div>
 <div class="grid2">
  <div class="fld"><label>Tipo Totem</label><select id="p_tipo"><option value="totem3">3 Cameras</option><option value="totem4">4 Cameras</option><option value="avulsa">Avulsa / IA</option><option value="outro">Personalizado</option></select></div>
  <div class="fld"><label>&nbsp;</label><div style="font-size:12px;color:var(--muted);padding-top:9px">Totem = kit de cameras do cliente.</div></div>
 </div>
 <div class="fld"><label>Recursos do Plano</label>
  <div style="display:flex;gap:8px"><input id="p_rec_in" placeholder="Digite um recurso..." onkeydown="if(event.key==='Enter'){event.preventDefault();addRec();}" style="flex:1"><button type="button" class="btn-primary" onclick="addRec()">Adicionar</button></div>
  <div id="p_recs" class="recs"></div>
 </div>
 <div class="cks">
  <label class="ck"><input type="checkbox" id="p_monit"> Inclui Monitoramento</label>
  <label class="ck"><input type="checkbox" id="p_ativo" checked> Plano Ativo</label>
  <label class="ck"><input type="checkbox" id="p_destaque"> Em Destaque</label>
 </div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>
<style>.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}
.fld{margin-bottom:10px}.fld label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.fld input,.fld select,.fld textarea{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 11px;font-size:14px;font-family:inherit}
.grid2{display:flex;gap:12px}.grid2>.fld{flex:1}.grid3{display:flex;gap:12px}.grid3>.fld{flex:1}
.cks{display:flex;gap:20px;flex-wrap:wrap;margin:4px 0}
.recs{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.recs .chip{background:var(--surface2);border:1px solid var(--border);border-radius:16px;padding:4px 10px;font-size:12px;display:flex;align-items:center;gap:6px}
.recs .chip b{cursor:pointer;color:var(--bad)}
@media(max-width:560px){.grid2,.grid3{flex-direction:column;gap:0}}</style>
<script>
var LST=[], RECS=[]; window.PAGE_INIT=load;
var TIPO={totem3:'3 cam',totem4:'4 cam',avulsa:'Avulsa/IA',outro:'Personalizado'};
function fillCams(){ var s=$('p_cams'); if(!s||s.options.length)return; var o=''; for(var i=1;i<=16;i++)o+='<option>'+i+'</option>'; s.innerHTML=o; }
async function load(){ fillCams(); try{ LST=(await api('GET','/api/comercial/prov/planos'))||[]; render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function render(){ $('rows').innerHTML=LST.map(function(p,i){
 return '<tr><td><b>'+esc(p.nome||'-')+'</b>'+(p.em_destaque?' <span class="pill ok">destaque</span>':'')+(p.descricao?'<div style="color:var(--muted);font-size:12px">'+esc(p.descricao)+'</div>':'')+'</td>'+
  '<td>'+esc(TIPO[p.tipo]||p.tipo||'-')+'</td><td>'+(p.cameras||0)+'</td><td>'+(p.contrato_meses||'-')+' m</td><td>'+(p.inclui_monitoramento?'sim':'-')+'</td>'+
  '<td class="money">'+brl(p.valor)+'</td>'+
  '<td>'+(p.ativo!==false?'<span class="pill ok">Ativo</span>':'<span class="pill off">Inativo</span>')+'</td>'+
  '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="editar('+i+')">editar</button><button class="act" style="color:var(--bad)" onclick="excluir('+i+')">excluir</button></td></tr>';
 }).join('')||'<tr><td colspan="8" class="center">Nenhum plano ainda. Clique em "Criar modelos padrao" ou "Novo plano".</td></tr>'; }
function renderRecs(){ $('p_recs').innerHTML=RECS.map(function(r,i){ return '<span class="chip">'+esc(r)+' <b onclick="delRec('+i+')">&times;</b></span>'; }).join(''); }
function addRec(){ var v=($('p_rec_in').value||'').trim(); if(!v)return; RECS.push(v); $('p_rec_in').value=''; renderRecs(); }
function delRec(i){ RECS.splice(i,1); renderRecs(); }
function novo(){ fillCams(); $('mt').textContent='Novo Plano'; $('p_id').value=''; $('p_nome').value=''; $('p_cams').value='3'; $('p_desc').value=''; $('p_valor').value=''; $('p_meses').value='36'; $('p_doc').value='ambos'; $('p_tipo').value='totem3'; RECS=[]; renderRecs(); $('p_monit').checked=true; $('p_ativo').checked=true; $('p_destaque').checked=false; $('ov').classList.add('open'); }
function editar(i){ fillCams(); var p=LST[i]; if(!p)return; $('mt').textContent='Editar Plano'; $('p_id').value=p.id; $('p_nome').value=p.nome||''; $('p_cams').value=(p.cameras!=null?p.cameras:3); $('p_desc').value=p.descricao||''; $('p_valor').value=(p.valor!=null?p.valor:''); $('p_meses').value=(p.contrato_meses||36); $('p_doc').value=p.tipo_documento||'ambos'; $('p_tipo').value=p.tipo||'outro'; RECS=(p.recursos||[]).slice(); renderRecs(); $('p_monit').checked=!!p.inclui_monitoramento; $('p_ativo').checked=p.ativo!==false; $('p_destaque').checked=!!p.em_destaque; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var b={id:$('p_id').value,nome:$('p_nome').value.trim(),cameras:parseInt($('p_cams').value||0)||0,descricao:$('p_desc').value.trim(),valor:parseFloat($('p_valor').value||0)||0,contrato_meses:parseInt($('p_meses').value||36)||36,tipo_documento:$('p_doc').value,tipo:$('p_tipo').value,recursos:RECS,inclui_monitoramento:$('p_monit').checked,ativo:$('p_ativo').checked,em_destaque:$('p_destaque').checked};
 if(!b.nome){ msg('Informe o nome do plano.'); return; }
 if(!(b.valor>0)){ msg('Informe o valor mensal.'); return; }
 try{ await api('POST','/api/comercial/prov/planos/salvar',b); fecha(); msg('Plano salvo.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(i){ var p=LST[i]; if(!p)return; if(!confirm('Excluir o plano?'))return; try{ await api('DELETE','/api/comercial/prov/planos/'+p.id); msg('Excluido.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function modelos(){ try{ var r=await api('POST','/api/comercial/prov/planos/modelos'); msg((r.criados||0)+' modelo(s) criado(s). Ajuste os valores no editar.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_PROV_CHAMADOS_BODY = """
<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
 <button class="btn-primary" id="tab_cli" onclick="setV('cli')">Dos meus clientes</button>
 <button class="act" id="tab_meus" onclick="setV('meus')">Meus (p/ Corexia)</button>
 <div style="flex:1"></div>
 <button class="act" onclick="novo()">+ Abrir chamado p/ Corexia</button>
</div>
<div id="msg" class="msg"></div>
<p id="hint" style="color:var(--muted);font-size:13px;margin:0 0 14px"></p>
<table><thead id="thd"></thead><tbody id="rows"><tr><td colspan="6" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:520px"><h2>Abrir chamado para a Corexia</h2>
 <div class="fld"><label>Tipo</label><select id="c_tipo"><option value="suporte">Suporte Tecnico</option><option value="financeiro">Financeiro</option><option value="geral">Atendimento</option></select></div>
 <div class="fld"><label>Descricao</label><textarea id="c_desc" rows="5" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:10px;font-size:14px;resize:vertical" placeholder="Descreva sua solicitacao..."></textarea></div>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="enviar()">Enviar</button></div></div></div>
<div class="ov" id="ovr"><div class="modal" style="max-width:560px"><h2>Responder cliente</h2>
 <div id="rinfo" style="font-size:13px;color:var(--muted);margin-bottom:10px;line-height:1.5"></div>
 <div class="fld"><label>Resposta ao cliente</label><textarea id="r_txt" rows="5" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:10px;font-size:14px;resize:vertical"></textarea></div>
 <div class="foot"><button onclick="fechaR()">Cancelar</button><button class="btn-primary" onclick="enviarResp()">Enviar resposta</button></div></div></div>
<script>
var ALL=[], V='cli'; window.PAGE_INIT=load;
function tp(t){ var m={suporte:'Suporte',financeiro:'Financeiro',geral:'Atendimento'}; return esc(m[t]||t||'-'); }
function stp(s){ var m={aberto:['Aberto','#fbbf24'],em_andamento:['Em andamento','#60a5fa'],resolvido:['Resolvido','#34d399'],fechado:['Fechado','#8a8f98']}; var x=m[s]||[s||'-','#8a8f98']; return '<span class="pill" style="color:'+x[1]+';border-color:'+x[1]+'55">'+x[0]+'</span>'; }
function fone(s){ s=(''+(s||'')); var t=''; for(var i=0;i<s.length;i++){ if(s.charAt(i)>='0'&&s.charAt(i)<='9')t+=s.charAt(i); } if(t.length>=12){var d=t.slice(-11);return '('+d.slice(0,2)+') '+d.slice(2,7)+'-'+d.slice(7);} return s||'-'; }
function dt(s){ if(!s)return '-'; try{var d=new Date(s); return d.toLocaleDateString('pt-BR')+' '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}catch(e){return s;} }
function setV(v){ V=v; $('tab_cli').className=(v==='cli'?'btn-primary':'act'); $('tab_meus').className=(v==='meus'?'btn-primary':'act'); render(); }
async function load(){ try{ ALL=(await api('GET','/api/chamados'))||[]; render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function render(){
 if(V==='cli'){ $('hint').textContent='Chamados abertos pelos seus clientes. Responda por aqui (o cliente e avisado no WhatsApp se voce tiver Z-API propria configurada).'; $('thd').innerHTML='<tr><th>Tipo</th><th>Cliente</th><th>Descricao</th><th>Contato</th><th>Status</th><th></th></tr>'; }
 else { $('hint').textContent='Chamados que voce abriu para a Corexia. A resposta da Corexia aparece aqui e no seu WhatsApp.'; $('thd').innerHTML='<tr><th>Tipo</th><th>Descricao</th><th>Resposta Corexia</th><th>Status</th><th>Aberto</th><th></th></tr>'; }
 var L=ALL.filter(function(c){ return V==='cli' ? c.aberto_por_role==='cliente' : c.aberto_por_role==='provedor'; });
 if(!L.length){ $('rows').innerHTML='<tr><td colspan="6" class="center" style="color:var(--muted)">Nenhum chamado.</td></tr>'; return; }
 $('rows').innerHTML=L.map(function(c){ var gi=ALL.indexOf(c);
  if(V==='cli'){ var acts='<button class="act" onclick="resp('+gi+')">responder</button>';
    if(c.status!=='resolvido'){ acts+='<button class="act" style="color:var(--ok)" onclick="concl('+gi+')">concluir</button>'; } else { acts+='<button class="act" onclick="reab('+gi+')">reabrir</button>'; }
    return '<tr><td>'+tp(c.tipo)+'</td><td><b>'+esc(c.cliente_nome||c.aberto_por_nome||'-')+'</b></td><td>'+esc(c.descricao||'')+(c.resposta?'<div style="color:var(--muted);font-size:12px;margin-top:3px">R: '+esc(c.resposta)+'</div>':'')+'</td><td style="white-space:nowrap">'+esc(fone(c.telefone))+'</td><td>'+stp(c.status)+'</td><td style="text-align:right;white-space:nowrap">'+acts+'</td></tr>';
  }
  return '<tr><td>'+tp(c.tipo)+'</td><td>'+esc(c.descricao||'')+'</td><td style="color:var(--muted)">'+(c.resposta?esc(c.resposta):'<i>aguardando</i>')+'</td><td>'+stp(c.status)+'</td><td style="white-space:nowrap;color:var(--muted)">'+dt(c.created_date)+'</td><td></td></tr>';
 }).join(''); }
function novo(){ $('c_tipo').value='suporte'; $('c_desc').value=''; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function enviar(){ var b={tipo:$('c_tipo').value,descricao:$('c_desc').value.trim()}; if(!b.descricao){ msg('Descreva sua solicitacao.'); return; } try{ await api('POST','/api/chamados',b); fecha(); msg('Chamado enviado a Corexia!',true); load(); }catch(e){ msg('Erro: '+e.message); } }
function resp(i){ var c=ALL[i]; if(!c)return; window.__c=c; $('rinfo').innerHTML='<b>'+esc(c.cliente_nome||c.aberto_por_nome||'')+'</b> &middot; '+esc(fone(c.telefone))+'<br>'+esc(c.descricao||''); $('r_txt').value=c.resposta||''; $('ovr').classList.add('open'); }
function fechaR(){ $('ovr').classList.remove('open'); }
async function enviarResp(){ var c=window.__c; if(!c)return; var t=$('r_txt').value.trim(); if(!t){ msg('Escreva a resposta.'); return; } try{ await api('PUT','/api/chamados/'+c.id,{resposta:t,status:'em_andamento'}); fechaR(); msg('Resposta enviada ao cliente.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function concl(i){ var c=ALL[i]; if(!c)return; if(!confirm('Concluir este chamado?'))return; try{ await api('PUT','/api/chamados/'+c.id,{status:'resolvido'}); msg('Chamado concluido.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function reab(i){ var c=ALL[i]; if(!c)return; try{ await api('PUT','/api/chamados/'+c.id,{status:'aberto'}); msg('Chamado reaberto.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_CHAMADOS_ADMIN_BODY = """
<div id="msg" class="msg"></div>
<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
 <button class="btn-primary" id="tab_ab" onclick="setF('abertos')">Abertos</button>
 <button class="act" id="tab_all" onclick="setF('')">Todos</button>
 <div style="flex:1"></div>
 <button class="act" onclick="abreP()">Plantao Corexia</button>
</div>
<table><thead><tr><th>Origem</th><th>Tipo</th><th>De</th><th>Descricao</th><th>Contato</th><th>Status</th><th>Aberto</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="8" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ovr"><div class="modal" style="max-width:580px"><h2>Responder chamado</h2>
 <div id="rinfo" style="font-size:13px;color:var(--muted);margin-bottom:10px;line-height:1.5"></div>
 <div class="fld"><label>Resposta</label><textarea id="r_txt" rows="5" style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:8px;color:var(--ink);padding:10px;font-size:14px;resize:vertical"></textarea></div>
 <div class="foot"><button onclick="fechaR()">Cancelar</button><button class="btn-primary" onclick="enviarResp()">Enviar resposta</button></div></div></div>
<div class="ov" id="ovp"><div class="modal" style="max-width:640px"><h2>Plantao Corexia</h2>
 <p style="color:var(--muted);font-size:12px;margin:0 0 10px">Quem recebe no WhatsApp os chamados abertos pelos provedores. So os <b>ativos</b> recebem.</p>
 <table><thead><tr><th>Nome</th><th>WhatsApp</th><th>Status</th><th></th></tr></thead><tbody id="prows"><tr><td colspan="4" class="center">carregando...</td></tr></tbody></table>
 <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
  <input type="hidden" id="p_id">
  <div class="two"><div class="fld"><label>Nome</label><input id="p_nome" placeholder="Ex.: Suporte N1"></div><div class="fld"><label>WhatsApp (DDD+numero)</label><input id="p_tel" placeholder="81997335544"></div></div>
  <label class="ck" style="margin:4px 0"><input type="checkbox" id="p_ativo" checked> Ativo (recebe chamados)</label>
  <div style="text-align:right"><button class="act" onclick="limpaP()">limpar</button><button class="btn-primary" onclick="salvarP()">Salvar plantonista</button></div>
 </div>
 <div class="foot"><button onclick="fechaP()">Fechar</button></div></div></div>
<style>.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}</style>
<script>
var ALL=[], PL=[], F='abertos'; window.PAGE_INIT=load;
function tp(t){ var m={suporte:'Suporte',financeiro:'Financeiro',geral:'Atendimento'}; return esc(m[t]||t||'-'); }
function orig(c){ return c.aberto_por_role==='cliente' ? '<span class="pill" style="color:#60a5fa;border-color:#60a5fa55">Cliente</span>' : '<span class="pill" style="color:#c084fc;border-color:#c084fc55">Provedor</span>'; }
function stp(s){ var m={aberto:['Aberto','#fbbf24'],em_andamento:['Em andamento','#60a5fa'],resolvido:['Resolvido','#34d399'],fechado:['Fechado','#8a8f98']}; var x=m[s]||[s||'-','#8a8f98']; return '<span class="pill" style="color:'+x[1]+';border-color:'+x[1]+'55">'+x[0]+'</span>'; }
function fone(s){ s=(''+(s||'')); var t=''; for(var i=0;i<s.length;i++){ if(s.charAt(i)>='0'&&s.charAt(i)<='9')t+=s.charAt(i); } if(t.length>=12){var d=t.slice(-11);return '('+d.slice(0,2)+') '+d.slice(2,7)+'-'+d.slice(7);} return s||'-'; }
function dt(s){ if(!s)return '-'; try{var d=new Date(s); return d.toLocaleDateString('pt-BR')+' '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}catch(e){return s;} }
function setF(f){ F=f; $('tab_ab').className=(f==='abertos'?'btn-primary':'act'); $('tab_all').className=(f===''?'btn-primary':'act'); render(); }
async function load(){ try{ ALL=(await api('GET','/api/chamados'))||[]; render(); }catch(e){ msg('Erro: '+e.message); } }
function render(){ var L=ALL.filter(function(c){ return F==='abertos' ? (c.status==='aberto'||c.status==='em_andamento') : true; });
 if(!L.length){ $('rows').innerHTML='<tr><td colspan="8" class="center" style="color:var(--muted)">Nenhum chamado.</td></tr>'; return; }
 $('rows').innerHTML=L.map(function(c){ var gi=ALL.indexOf(c); var acts='<button class="act" onclick="resp('+gi+')">responder</button>';
  if(c.status!=='resolvido'){ acts+='<button class="act" style="color:var(--ok)" onclick="concl('+gi+')">concluir</button>'; } else { acts+='<button class="act" onclick="reab('+gi+')">reabrir</button>'; }
  var de=c.aberto_por_role==='cliente' ? (c.cliente_nome||c.aberto_por_nome||'-')+' <span style="color:var(--muted);font-size:11px">('+esc(c.provedor_nome||'')+')</span>' : (c.provedor_nome||c.aberto_por_nome||'-');
  return '<tr><td>'+orig(c)+'</td><td>'+tp(c.tipo)+'</td><td><b>'+de+'</b></td><td>'+esc(c.descricao||'')+(c.resposta?'<div style="color:var(--muted);font-size:12px;margin-top:3px">R: '+esc(c.resposta)+'</div>':'')+'</td><td style="white-space:nowrap">'+esc(fone(c.telefone))+'</td><td>'+stp(c.status)+'</td><td style="white-space:nowrap;color:var(--muted)">'+dt(c.created_date)+'</td><td style="text-align:right;white-space:nowrap">'+acts+'</td></tr>';
 }).join(''); }
function resp(i){ var c=ALL[i]; if(!c)return; window.__c=c; $('rinfo').innerHTML='<b>'+esc(c.aberto_por_nome||'')+'</b> &middot; '+esc(fone(c.telefone))+'<br>'+esc(c.descricao||''); $('r_txt').value=c.resposta||''; $('ovr').classList.add('open'); }
function fechaR(){ $('ovr').classList.remove('open'); }
async function enviarResp(){ var c=window.__c; if(!c)return; var t=$('r_txt').value.trim(); if(!t){ msg('Escreva a resposta.'); return; } try{ await api('PUT','/api/chamados/'+c.id,{resposta:t,status:'em_andamento'}); fechaR(); msg('Resposta enviada.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function concl(i){ var c=ALL[i]; if(!c)return; if(!confirm('Concluir este chamado?'))return; try{ await api('PUT','/api/chamados/'+c.id,{status:'resolvido'}); msg('Chamado concluido.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function reab(i){ var c=ALL[i]; if(!c)return; try{ await api('PUT','/api/chamados/'+c.id,{status:'aberto'}); msg('Chamado reaberto.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function abreP(){ $('ovp').classList.add('open'); limpaP(); await loadP(); }
function fechaP(){ $('ovp').classList.remove('open'); }
async function loadP(){ try{ PL=(await api('GET','/api/comercial/plantao-corexia'))||[]; renderP(); }catch(e){ msg('Erro: '+e.message); } }
function renderP(){ $('prows').innerHTML=PL.map(function(p,i){ return '<tr><td><b>'+esc(p.nome||'-')+'</b></td><td>'+esc(fone(p.telefone))+'</td><td>'+(p.ativo!==false?'<span class="pill ok">Ativo</span>':'<span class="pill off">Inativo</span>')+'</td><td style="text-align:right;white-space:nowrap"><button class="act" onclick="editP('+i+')">editar</button><button class="act" style="color:var(--bad)" onclick="delP('+i+')">excluir</button></td></tr>'; }).join('')||'<tr><td colspan="4" class="center" style="color:var(--muted)">Nenhum plantonista. Adicione abaixo.</td></tr>'; }
function limpaP(){ $('p_id').value=''; $('p_nome').value=''; $('p_tel').value=''; $('p_ativo').checked=true; }
function editP(i){ var p=PL[i]; if(!p)return; $('p_id').value=p.id; $('p_nome').value=p.nome||''; $('p_tel').value=p.telefone||''; $('p_ativo').checked=p.ativo!==false; }
async function salvarP(){ var b={id:$('p_id').value,nome:$('p_nome').value.trim(),telefone:$('p_tel').value.trim(),ativo:$('p_ativo').checked}; if(!b.nome||!b.telefone){ msg('Informe nome e WhatsApp.'); return; } try{ await api('POST','/api/comercial/plantao-corexia/salvar',b); limpaP(); msg('Salvo.',true); loadP(); }catch(e){ msg('Erro: '+e.message); } }
async function delP(i){ var p=PL[i]; if(!p)return; if(!confirm('Excluir '+(p.nome||'')+'?'))return; try{ await api('DELETE','/api/comercial/plantao-corexia/'+p.id); loadP(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_PROV_PLANTAO_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><div style="flex:1"></div><button class="btn-primary" onclick="novo()">+ Numero de plantao</button></div>
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Numeros que recebem <b>todos os alertas</b> das suas cameras no WhatsApp (sua equipe de plantao/monitoramento). So os <b>ativos</b> recebem.</p>
<table><thead><tr><th>Nome</th><th>WhatsApp</th><th>Notas</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:480px"><h2 id="mt">Numero de plantao</h2><input type="hidden" id="p_id">
 <div class="fld"><label>Nome</label><input id="p_nome" placeholder="Ex.: Central de Monitoramento"></div>
 <div class="fld"><label>WhatsApp (DDD + numero)</label><input id="p_tel" placeholder="81997335544"></div>
 <div class="fld"><label>Notas (opcional)</label><input id="p_notas"></div>
 <label class="ck" style="margin:4px 0"><input type="checkbox" id="p_ativo" checked> Ativo (recebe alertas)</label>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>
<style>.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}</style>
<script>
var LST=[]; window.PAGE_INIT=load;
function fone(s){ s=(''+(s||'')).replace(/\\D/g,''); if(s.length>=12){var d=s.slice(-11);return '('+d.slice(0,2)+') '+d.slice(2,7)+'-'+d.slice(7);} return s||'-'; }
async function load(){ try{ LST=await api('GET','/api/comercial/prov/plantao'); render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function render(){ $('rows').innerHTML=LST.map(function(p){
 return '<tr><td><b>'+esc(p.nome||'-')+'</b></td><td>'+esc(fone(p.telefone))+'</td><td style="color:var(--muted)">'+esc(p.notas||'')+'</td>'+
  '<td>'+(p.ativo!==false?'<span class="pill ok">Ativo</span>':'<span class="pill off">Inativo</span>')+'</td>'+
  '<td style="text-align:right;white-space:nowrap"><button class="act" onclick="toggle(\\''+p.id+'\\')">'+(p.ativo!==false?'desativar':'ativar')+'</button>'+
  '<button class="act" onclick="editar(\\''+p.id+'\\')">editar</button><button class="act" style="color:var(--bad)" onclick="excluir(\\''+p.id+'\\')">excluir</button></td></tr>';
 }).join('')||'<tr><td colspan="5" class="center">Nenhum numero de plantao. Adicione sua equipe de monitoramento.</td></tr>'; }
function novo(){ $('mt').textContent='Novo numero'; ['p_id','p_nome','p_tel','p_notas'].forEach(function(i){$(i).value='';}); $('p_ativo').checked=true; $('ov').classList.add('open'); }
function editar(id){ var p=LST.filter(function(x){return x.id===id})[0]; if(!p)return; $('mt').textContent='Editar numero'; $('p_id').value=id; $('p_nome').value=p.nome||''; $('p_tel').value=p.telefone||''; $('p_notas').value=p.notas||''; $('p_ativo').checked=p.ativo!==false; $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
async function salvar(){ var b={id:$('p_id').value,nome:$('p_nome').value.trim(),telefone:$('p_tel').value.trim(),notas:$('p_notas').value.trim(),ativo:$('p_ativo').checked};
 if(!b.nome||!b.telefone){ msg('Informe nome e WhatsApp.'); return; }
 try{ await api('POST','/api/comercial/prov/plantao/salvar',b); fecha(); msg('Salvo.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
async function toggle(id){ try{ await api('POST','/api/comercial/prov/plantao/'+id+'/toggle'); load(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(id){ if(!confirm('Excluir este numero?'))return; try{ await api('DELETE','/api/comercial/prov/plantao/'+id); msg('Excluido.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_PROV_GRAVACOES_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:2px 0 16px">Aqui voce assiste as gravacoes em nuvem das suas cameras. A gravacao e a retencao (dias) sao definidas por camera na aba <b>Cameras e IA</b>. So aparecem abaixo as cameras que ja tem gravacao.</p>
<table><thead><tr><th>Camera</th><th>Gravacoes disponiveis</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="3" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:820px"><h2>Gravacoes - <span id="sv_nome"></span></h2>
 <div id="dias" style="display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 12px"></div>
 <div id="player" style="margin:10px 0"></div>
 <div id="segs" style="max-height:320px;overflow:auto"></div>
 <div class="foot"><button onclick="fecha()">Fechar</button></div></div></div>
<style>.seg{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 10px;border-bottom:1px solid var(--border);font-size:13px}.daybtn.sel{background:var(--accent);border-color:var(--accent);color:#1a1205;font-weight:700}</style>
<script>
var CAMS=[]; window.PAGE_INIT=load;
async function load(){
 try{ CAMS=(await api('GET','/api/gravacoes/cameras'))||[]; render(); }
 catch(e){ msg('Erro (logado como provedor?): '+e.message); }
}
function render(){
 if(!CAMS.length){ $('rows').innerHTML='<tr><td colspan="3" class="center" style="color:var(--muted)">Nenhuma gravacao disponivel ainda. A gravacao e definida por camera na aba Cameras e IA.</td></tr>'; return; }
 $('rows').innerHTML=CAMS.map(function(c,i){
  var n=(c.dias||[]).length;
  return '<tr><td><b>'+esc(c.camera_nome||c.camera_id)+'</b></td><td>'+n+' dia'+(n!=1?'s':'')+' disponivel'+(n!=1?'is':'')+'</td><td style="text-align:right"><button class="act" onclick="ver('+i+')">ver gravacoes</button></td></tr>';
 }).join('');
}
function ver(i){
 var c=CAMS[i]; if(!c)return;
 $('sv_nome').textContent=c.camera_nome||''; $('player').innerHTML='';
 $('dias').innerHTML=(c.dias||[]).map(function(d,j){ return '<button class="act daybtn" onclick="dia('+i+','+j+',this)">'+esc(d.split('-').reverse().join('/'))+'</button>'; }).join('');
 $('segs').innerHTML='<div class="center" style="color:var(--muted)">Escolha um dia acima.</div>';
 $('ov').classList.add('open');
}
async function dia(i,j,btn){
 var c=CAMS[i]; if(!c)return; var d=(c.dias||[])[j]; if(!d)return;
 Array.prototype.forEach.call(document.querySelectorAll('.daybtn'),function(b){b.classList.remove('sel')}); if(btn)btn.classList.add('sel');
 $('segs').innerHTML='<div class="center">carregando...</div>'; window.__U=[];
 try{
  var list=(await api('GET','/api/gravacoes?camera_id='+encodeURIComponent(c.camera_id)+'&data='+encodeURIComponent(d)))||[];
  if(!list.length){ $('segs').innerHTML='<div class="center" style="color:var(--muted)">Sem gravacoes neste dia.</div>'; return; }
  window.__U=list.map(function(s){ return '/gravacao/'+c.camera_id+'/'+encodeURIComponent(s.arquivo)+'?t='+encodeURIComponent(TOKEN); });
  $('segs').innerHTML=list.map(function(s,k){ return '<div class="seg"><span>'+esc(s.inicio||s.arquivo)+' &middot; '+(s.tamanho_mb||0)+' MB</span><button class="act" onclick="play('+k+')">assistir</button></div>'; }).join('');
 }catch(e){ $('segs').innerHTML='<div class="center" style="color:var(--bad)">Erro: '+esc(e.message)+'</div>'; }
}
function play(k){ var u=(window.__U||[])[k]; if(!u)return; $('player').innerHTML='<video controls autoplay playsinline style="width:100%;border-radius:8px;background:#000" src="'+u+'"></video>'; }
function fecha(){ $('ov').classList.remove('open'); $('player').innerHTML=''; }
</script>
"""


_PROV_PREF_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><input id="q" placeholder="Buscar cliente..." oninput="render()" style="flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:10px 12px;font-size:14px"><button onclick="load()">Atualizar</button></div>
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Por cliente, defina quais alertas ele recebe no WhatsApp (tipos, horario, dias). <b>Sem preferencia = recebe tudo, 24h.</b></p>
<table><thead><tr><th>Cliente</th><th>WhatsApp</th><th>Regra</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<div class="ov" id="ov"><div class="modal" style="max-width:600px"><h2>Preferencias - <span id="pf_nome"></span></h2><input type="hidden" id="pf_cid">
 <label class="ck" style="margin-bottom:8px"><input type="checkbox" id="pf_wa" checked> Notificar por WhatsApp</label>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;text-transform:uppercase;margin:6px 0">Tipos permitidos (nenhum marcado = todos)</div>
 <div id="pf_tipos" style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-bottom:8px"></div>
 <div class="two"><div class="fld"><label>Hora inicio (vazio = 00:00)</label><input id="pf_hi" type="time"></div><div class="fld"><label>Hora fim (vazio = 23:59)</label><input id="pf_hf" type="time"></div></div>
 <div style="color:var(--accent);font-family:var(--mono);font-size:11px;text-transform:uppercase;margin:6px 0">Dias (nenhum = todos)</div>
 <div id="pf_dias" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px"></div>
 <label class="ck"><input type="checkbox" id="pf_ativo" checked> Preferencia ativa (desmarcado = padrao: recebe tudo)</label>
 <div class="foot"><button onclick="fecha()">Cancelar</button><button class="btn-primary" onclick="salvar()">Salvar</button></div></div></div>
<style>.ck{display:flex;gap:7px;align-items:center;font-size:13px;color:var(--ink);cursor:pointer}.ck input{width:auto}</style>
<script>
var LST=[]; window.PAGE_INIT=load;
var TIPOS=[['fogo','Fogo/Fumaca'],['arma_fogo','Arma de fogo'],['arma_branca','Arma branca'],['placa','Placa'],['pessoa','Pessoa'],['veiculo','Veiculo'],['animal','Animal'],['epi','EPI'],['intruso','Intrusao'],['linha','Linha'],['toca_ninja','Toca ninja'],['piscina','Piscina']];
var DIAS=[[0,'Dom'],[1,'Seg'],[2,'Ter'],[3,'Qua'],[4,'Qui'],[5,'Sex'],[6,'Sab']];
async function load(){ try{ LST=await api('GET','/api/comercial/prov/preferencias'); render(); }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function regraTxt(p){ if(!p||!p.ativo) return 'Padrao (recebe tudo, 24h)'; if(!p.notificar_whatsapp) return 'WhatsApp DESLIGADO';
 var t=(p.tipos_permitidos&&p.tipos_permitidos.length)?(p.tipos_permitidos.length+' tipos'):'todos tipos';
 var h=(p.hora_inicio||p.hora_fim)?((p.hora_inicio||'00:00')+'-'+(p.hora_fim||'23:59')):'24h';
 var d=(p.dias_semana&&p.dias_semana.length)?(p.dias_semana.length+' dias'):'todo dia';
 return t+' | '+h+' | '+d; }
function render(){ var q=($('q').value||'').toLowerCase();
 var arr=LST.filter(function(x){return !q||(x.cliente_nome||'').toLowerCase().indexOf(q)>=0;});
 $('rows').innerHTML=arr.map(function(x){ var p=x.pref;
  var st=(!p||!p.ativo)?'<span class="pill off">Padrao</span>':(p.notificar_whatsapp?'<span class="pill ok">Ativa</span>':'<span class="pill" style="color:var(--bad)">WA off</span>');
  return '<tr><td><b>'+esc(x.cliente_nome||'-')+'</b></td><td>'+esc(x.telefone||'-')+'</td><td style="color:var(--muted);font-size:13px">'+esc(regraTxt(p))+'</td><td>'+st+'</td>'+
   '<td style="text-align:right"><button class="act" style="color:var(--accent)" onclick="editar(\\''+x.cliente_id+'\\')">configurar</button></td></tr>';
 }).join('')||'<tr><td colspan="5" class="center">Nenhum cliente. Cadastre em Meus Clientes.</td></tr>'; }
function editar(cid){ var x=LST.filter(function(y){return y.cliente_id===cid})[0]; if(!x)return; var p=x.pref||{};
 $('pf_cid').value=cid; $('pf_nome').textContent=x.cliente_nome||'';
 $('pf_wa').checked=p.notificar_whatsapp!==false; $('pf_ativo').checked=p.ativo!==false; $('pf_hi').value=p.hora_inicio||''; $('pf_hf').value=p.hora_fim||'';
 var tp=p.tipos_permitidos||[]; $('pf_tipos').innerHTML=TIPOS.map(function(t){return '<label class="ck"><input type="checkbox" value="'+t[0]+'"'+(tp.indexOf(t[0])>=0?' checked':'')+'> '+esc(t[1])+'</label>';}).join('');
 var dd=p.dias_semana||[]; $('pf_dias').innerHTML=DIAS.map(function(d){return '<label class="ck"><input type="checkbox" value="'+d[0]+'"'+(dd.indexOf(d[0])>=0?' checked':'')+'> '+d[1]+'</label>';}).join('');
 $('ov').classList.add('open'); }
function fecha(){ $('ov').classList.remove('open'); }
function chk(cont){ return Array.prototype.slice.call(document.querySelectorAll('#'+cont+' input:checked')).map(function(e){return e.value;}); }
async function salvar(){ var b={cliente_id:$('pf_cid').value,notificar_whatsapp:$('pf_wa').checked,ativo:$('pf_ativo').checked,
  hora_inicio:$('pf_hi').value,hora_fim:$('pf_hf').value,tipos_permitidos:chk('pf_tipos'),dias_semana:chk('pf_dias').map(function(v){return parseInt(v,10);})};
 try{ await api('POST','/api/comercial/prov/preferencias/salvar',b); fecha(); msg('Preferencias salvas.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_MONIT_BODY = """
<div id="msg" class="msg"></div>
<div class="cards" style="margin-bottom:14px">
 <div class="kpi"><div class="k">Cameras</div><div class="v" id="k_cams">-</div></div>
 <div class="kpi"><div class="k">Online</div><div class="v" id="k_on" style="color:var(--ok)">-</div></div>
 <div class="kpi"><div class="k">Alertas hoje</div><div class="v" id="k_hoje" style="color:var(--accent)">-</div></div>
 <div class="kpi"><div class="k">Alertas novos</div><div class="v" id="k_nv" style="color:var(--bad)">-</div></div>
</div>
<div style="display:grid;grid-template-columns:1.7fr 1fr;gap:16px;align-items:start">
 <div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>Mural de cameras</b><span style="display:flex;gap:10px;align-items:center"><label class="ck" style="font-size:12px"><input type="checkbox" id="auto" checked> auto</label><button onclick="load()">Atualizar</button></span></div>
  <div id="wall" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-bottom:18px"><div class="center" style="grid-column:1/-1">carregando...</div></div>
  <div style="margin-bottom:8px"><b>Mapa das cameras</b></div>
  <div id="mapbox" style="height:340px;border:1px solid var(--border);border-radius:12px;overflow:hidden;background:var(--surface)"><div class="center" style="padding:30px">carregando...</div></div>
 </div>
 <div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>Alertas recentes</b><label class="ck" style="font-size:12px"><input type="checkbox" id="som"> som</label></div>
  <div id="feed"><div class="center">carregando...</div></div>
 </div>
</div>
<div class="ov" id="ovlive"><div class="modal" style="max-width:900px;width:100%">
 <h2 style="display:flex;align-items:center;gap:10px"><span id="lv_nome"></span><span style="flex:1"></span><button onclick="fecharLive()">Fechar</button></h2>
 <div style="position:relative;padding-top:56.25%;background:#000;border-radius:8px;overflow:hidden"><iframe id="lv_frame" src="about:blank" allow="autoplay; fullscreen" style="position:absolute;inset:0;width:100%;height:100%;border:0"></iframe></div>
 <div style="font-size:12px;color:var(--muted);margin-top:8px">Se nao carregar, <a id="lv_link" href="#" target="_blank" rel="noopener" style="color:var(--accent)">abra em nova aba</a>.</div>
</div></div>
<style>.ck{display:flex;gap:6px;align-items:center;color:var(--ink);cursor:pointer}.ck input{width:auto}
.mcard{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;cursor:pointer}
.mthumb{position:relative;padding-top:56.25%;background:#0b0d12}
.mthumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.mthumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px}
.frow{display:flex;gap:8px;padding:8px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;background:var(--surface)}
.frow img{width:54px;height:34px;object-fit:cover;border-radius:5px;background:#0b0d12;flex:none}
.leaflet-popup-content{color:#111}</style>
<script>
window.PAGE_INIT=load;
var CAMS=[], LASTTOP=null, MAP=null;
var TLAB={fogo:'Fogo',arma_fogo:'Arma de fogo',arma_branca:'Arma branca',arma:'Arma',intruso:'Intrusao',linha:'Linha',placa:'Placa',pessoa:'Pessoa',veiculo:'Veiculo',animal:'Animal',epi:'EPI',heatmap:'Calor',toca_ninja:'Toca ninja',piscina:'Piscina',movimento:'Movimento',aglomeracao:'Aglomeracao',outro:'Outro'};
function dt(s){ s=(''+(s||'')); var d=s.slice(0,10).split('-'); var t=s.slice(11,16); return d.length===3?(d[2]+'/'+d[1]+' '+t):s; }
async function load(){ try{
  var d=await api('GET','/api/comercial/prov/cameras'); CAMS=d.cameras||[];
  $('k_cams').textContent=CAMS.length;
  $('k_on').textContent=CAMS.filter(function(c){return (c.status||'').toLowerCase()==='online';}).length;
  renderWall(); initMap();
  var r=await api('GET','/api/comercial/prov/alertas/resumo'); $('k_hoje').textContent=r.hoje; $('k_nv').textContent=r.novos;
  var a=await api('GET','/api/comercial/prov/alertas?limit=12'); renderFeed(a);
 }catch(e){ msg('Erro (logado como provedor?): '+e.message); } }
function renderWall(){ if(!CAMS.length){ $('wall').innerHTML='<div class="center" style="grid-column:1/-1">Nenhuma camera atribuida a voce ainda.</div>'; return; }
 $('wall').innerHTML=CAMS.map(function(c){ var stOn=(c.status||'').toLowerCase()==='online';
  var thumb='<div class="ph">sem imagem</div><img src="/camthumb/'+c.id+'?t='+encodeURIComponent(TOKEN)+'" loading="lazy" onerror="this.style.display=\\'none\\'">';
  return '<div class="mcard" onclick="ver(\\''+c.id+'\\')"><div class="mthumb">'+thumb+'<span class="pill '+(stOn?'ok':'off')+'" style="position:absolute;top:6px;left:6px;font-size:10px;background:rgba(0,0,0,.55)">'+(stOn?'online':'offline')+'</span></div><div style="padding:7px 9px;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(c.nome||'-')+'</div></div>';
 }).join(''); }
function renderFeed(a){ if(!a.length){ $('feed').innerHTML='<div class="center">Sem alertas recentes.</div>'; return; }
 var top=a[0]; if(top&&LASTTOP&&top.id!==LASTTOP&&top.status==='novo'){ beep(); fala((TLAB[top.tipo]||top.tipo)+' em '+(top.camera_nome||'')); } if(top)LASTTOP=top.id;
 $('feed').innerHTML=a.map(function(x){ var im=x.imagem_url?('<img src="/api/comercial/prov/alerta/'+x.id+'/img?t='+encodeURIComponent(TOKEN)+'&w=200" onerror="this.style.visibility=\\'hidden\\'">'):'';
  return '<div class="frow">'+im+'<div style="min-width:0"><div style="font-weight:600;font-size:13px">'+esc(TLAB[x.tipo]||x.tipo||'-')+' <span style="color:var(--muted);font-weight:400">'+(x.confianca||0)+'%</span></div><div style="color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(x.camera_nome||'')+'</div><div style="color:var(--muted);font-size:11px">'+dt(x.criado)+'</div></div></div>';
 }).join(''); }
function ver(id){ var c=CAMS.filter(function(x){return x.id===id})[0]; if(!c)return; if(!c.embed_url){msg('Camera sem player.');return;} $('lv_nome').textContent=c.nome||''; $('lv_frame').src=c.embed_url; $('lv_link').href=c.embed_url; $('ovlive').classList.add('open'); }
function fecharLive(){ $('ovlive').classList.remove('open'); $('lv_frame').src='about:blank'; }
function initMap(){ var pts=CAMS.filter(function(c){return c.latitude&&c.longitude;}); var box=$('mapbox');
 if(!pts.length){ box.innerHTML='<div class="center" style="padding:30px;color:var(--muted)">Nenhuma camera com localizacao (lat/long) ainda. Quando as cameras tiverem coordenadas, elas aparecem aqui no mapa.</div>'; return; }
 loadLeaflet(function(){ if(!MAP){ box.innerHTML=''; MAP=L.map(box).setView([pts[0].latitude,pts[0].longitude],13); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'OSM'}).addTo(MAP); }
  pts.forEach(function(c){ L.marker([c.latitude,c.longitude]).addTo(MAP).bindPopup(esc(c.nome||'')); }); }); }
function loadLeaflet(cb){ if(window.L){cb();return;}
 var css=document.createElement('link'); css.rel='stylesheet'; css.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'; document.head.appendChild(css);
 var s=document.createElement('script'); s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'; s.onload=cb; s.onerror=function(){ var b=$('mapbox'); if(b)b.innerHTML='<div class="center" style="padding:30px;color:var(--muted)">Nao foi possivel carregar o mapa (sem internet?).</div>'; }; document.body.appendChild(s); }
function beep(){ try{ var A=window.AudioContext||window.webkitAudioContext; if(!A)return; var a=new A(); var o=a.createOscillator(),g=a.createGain(); o.connect(g); g.connect(a.destination); o.type='sine'; o.frequency.value=880; g.gain.value=0.08; o.start(); setTimeout(function(){o.stop();a.close();},350);}catch(e){} }
function fala(t){ try{ if($('som').checked&&window.speechSynthesis){ var u=new SpeechSynthesisUtterance('Alerta: '+t); u.lang='pt-BR'; window.speechSynthesis.speak(u);} }catch(e){} }
setInterval(function(){ if($('auto')&&$('auto').checked) load(); },20000);
</script>
"""


_TESTER_BODY = """
<div id="msg" class="msg"></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:22px">
 <h2 style="margin:0 0 12px;font-size:17px">Novo provedor de teste</h2>
 <p style="color:var(--muted);font-size:13px;margin:0 0 14px">Cria um provedor no plano <b>Cloud</b> com direito a ate 3 cameras (2 ao vivo + 1 com gravacao 1 dia) + IA Corexia, <b>sem cobranca</b> pelo periodo. Ao vencer, o painel bloqueia sozinho e voce reativa (entra na cobranca normal) ou exclui.</p>
 <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-items:end">
  <div class="dfld"><label>Nome</label><input id="t_nome" placeholder="Ex.: Provedor Teste"></div>
  <div class="dfld"><label>E-mail (login)</label><input id="t_email" placeholder="prov@exemplo.com"></div>
  <div class="dfld"><label>Senha (min 4)</label><input id="t_senha" type="text"></div>
  <div class="dfld"><label>WhatsApp (opcional)</label><input id="t_tel" placeholder="DDD + numero"></div>
  <div class="dfld"><label>Periodo sem cobranca</label><select id="t_dias"><option value="7">7 dias</option><option value="14">14 dias</option></select></div>
  <button class="btn-primary" onclick="criar()" style="height:41px">Criar tester</button>
 </div>
</div>
<h2 style="font-size:16px;margin:0 0 10px">Provedores em teste</h2>
<table><thead><tr><th>Provedor</th><th>Trial ate</th><th>Cameras</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<style>.dfld label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.dfld input,.dfld select{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 11px;font-size:14px}</style>
<script>
window.PAGE_INIT=init;
async function init(){
 loadList();
 $('rows').addEventListener('click',function(e){ var b=e.target.closest('[data-act]'); if(!b)return;
  var act=b.getAttribute('data-act'), id=b.getAttribute('data-id'); if(act==='reativar')reativar(id); else if(act==='excluir')excluir(id); });
}
async function criar(){
 var b={nome:$('t_nome').value.trim(),email:$('t_email').value.trim(),password:$('t_senha').value,dias:parseInt($('t_dias').value||7)||7,telefone:$('t_tel').value.trim()};
 if(!b.nome||!b.email||!b.password){ msg('Preencha nome, email e senha.'); return; }
 try{ var r=await api('POST','/api/tester/criar',b); msg('Provedor tester criado! Trial ate '+esc(r.trial_ate)+'. Login: '+esc(b.email),true);
  ['t_nome','t_email','t_senha','t_tel'].forEach(function(i){$(i).value='';}); loadList(); }
 catch(e){ msg('Erro: '+e.message); }
}
async function loadList(){ try{ var L=(await api('GET','/api/tester/listar'))||[];
 $('rows').innerHTML=L.map(function(d){
  var badge=d.status==='bloqueado'?'<span class="pill" style="color:var(--bad)">Bloqueado</span>':(d.status_disp==='trial vencido'?'<span class="pill off">Trial vencido</span>':'<span class="pill ok">Trial ativo</span>');
  return '<tr><td><b>'+esc(d.nome||'-')+'</b><div style="color:var(--muted);font-size:12px">'+esc(d.email||'')+'</div></td><td>'+esc(d.trial_ate||'')+'</td><td>'+d.n_cameras+' / '+d.limite+'</td><td>'+badge+'</td><td style="text-align:right;white-space:nowrap"><button class="act" style="color:var(--ok)" data-act="reativar" data-id="'+esc(d.id)+'">reativar</button><button class="act" style="color:var(--bad)" data-act="excluir" data-id="'+esc(d.id)+'">excluir</button></td></tr>';
 }).join('')||'<tr><td colspan="5" class="center">Nenhum provedor em teste.</td></tr>'; }catch(e){ msg('Erro na lista: '+e.message); } }
async function reativar(id){ if(!confirm('Reativar este provedor? Ele sai do teste e entra no fluxo normal de cobranca.'))return;
 try{ await api('POST','/api/tester/'+id+'/reativar'); msg('Reativado. Agora e provedor normal - cobre pela aba Provedor/Revenda.',true); loadList(); }catch(e){ msg('Erro: '+e.message); } }
async function excluir(id){ if(!confirm('EXCLUIR este provedor de teste? Remove o painel, o login e as cameras dele. Irreversivel.'))return;
 try{ await api('DELETE','/api/tester/'+id); msg('Excluido.',true); loadList(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_DEMO_BODY = """
<div id="msg" class="msg"></div>
<div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:22px">
 <h2 style="margin:0 0 12px;font-size:17px">Nova demonstracao</h2>
 <p style="color:var(--muted);font-size:13px;margin:0 0 14px">Concede a um usuario o acesso a ate 4 cameras por um periodo. Ele vera so essas cameras e o acesso expira sozinho.</p>
 <div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:14px">
  <label class="ck"><input type="radio" name="umodo" id="u_novo" checked onchange="modoUser()"> Criar login novo</label>
  <label class="ck"><input type="radio" name="umodo" id="u_exi" onchange="modoUser()"> Usuario existente</label>
 </div>
 <div id="box_novo" style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px">
  <div class="dfld"><label>Nome</label><input id="n_nome" placeholder="Ex.: Cliente Demo"></div>
  <div class="dfld"><label>E-mail</label><input id="n_email" placeholder="demo@exemplo.com"></div>
  <div class="dfld"><label>Senha (min 4)</label><input id="n_senha" type="text" placeholder="senha"></div>
 </div>
 <div id="box_exist" style="display:none;margin-bottom:16px"><div class="dfld"><label>Usuario ja cadastrado</label><select id="u_exist"></select></div></div>
 <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end">
  <div class="dfld" style="flex:1;min-width:280px"><label>Cameras da demo &nbsp;<span id="camcount" style="color:var(--accent);font-weight:700">0/4 selecionadas</span></label>
   <input id="cq" placeholder="Buscar camera pelo nome..." oninput="renderCams()" style="margin-bottom:8px">
   <div id="camlist" style="max-height:220px;overflow:auto;border:1px solid var(--border);border-radius:9px;padding:4px"></div>
  </div>
  <div class="dfld" style="min-width:150px"><label>Duracao</label><select id="dur"><option value="7">7 dias</option><option value="30">30 dias</option><option value="90">3 meses</option><option value="180">6 meses</option><option value="365">12 meses</option></select></div>
  <button class="btn-primary" onclick="criar()" style="height:41px">Criar demonstracao</button>
 </div>
</div>
<h2 style="font-size:16px;margin:0 0 10px">Demonstracoes</h2>
<table><thead><tr><th>Usuario</th><th>Cameras</th><th>Expira</th><th>Status</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="5" class="center">carregando...</td></tr></tbody></table>
<style>.ck{display:flex;gap:7px;align-items:center;font-size:14px;color:var(--ink);cursor:pointer}.ck input{width:auto}
.dfld label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
.dfld input,.dfld select{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--ink);padding:9px 11px;font-size:14px}
.camrow{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:7px;font-size:13px;cursor:pointer}
.camrow:hover{background:var(--surface2)}.camrow input{width:auto}</style>
<script>
var CAMS=[], USERS=[], SEL={}; window.PAGE_INIT=init;
async function init(){
 try{
  CAMS=(await api('GET','/api/entities/Camera'))||[];
  USERS=((await api('GET','/api/users'))||[]).filter(function(u){return u.role!=='admin';});
  fillUsers(); renderCams(); loadList();
  $('camlist').addEventListener('change',function(e){ var el=e.target; var id=el.getAttribute&&el.getAttribute('data-cam'); if(!id)return;
   if(el.checked){ if(Object.keys(SEL).length>=4 && !SEL[id]){ msg('Maximo 4 cameras.'); el.checked=false; return; } SEL[id]=1; } else { delete SEL[id]; }
   $('camcount').textContent=Object.keys(SEL).length+'/4 selecionadas'; });
  $('rows').addEventListener('click',function(e){ var b=e.target.closest('[data-rev]'); if(b) revogar(b.getAttribute('data-rev')); });
 }catch(e){ msg('Erro (logado como admin?): '+e.message); }
}
function modoUser(){ var novo=$('u_novo').checked; $('box_novo').style.display=novo?'grid':'none'; $('box_exist').style.display=novo?'none':'block'; }
function fillUsers(){ $('u_exist').innerHTML='<option value="">- selecione -</option>'+USERS.map(function(u){return '<option value="'+esc(u.id)+'">'+esc((u.full_name||u.email)+' - '+u.email)+'</option>';}).join(''); }
function renderCams(){ var q=($('cq').value||'').toLowerCase();
 var arr=CAMS.filter(function(c){ return !q || (((c.nome||'')+' '+(c.provedor_nome||'')).toLowerCase().indexOf(q)>=0); });
 $('camlist').innerHTML=arr.map(function(c){ var on=!!SEL[c.id];
  return '<label class="camrow"><input type="checkbox" data-cam="'+esc(c.id)+'"'+(on?' checked':'')+'> <b>'+esc(c.nome||c.id)+'</b> <span style="color:var(--muted)">'+esc(c.provedor_nome||'')+'</span></label>';
 }).join('')||'<div style="color:var(--muted);padding:8px">Nenhuma camera.</div>';
 $('camcount').textContent=Object.keys(SEL).length+'/4 selecionadas';
}
async function criar(){
 var dias=parseInt($('dur').value||0)||0; var cams=Object.keys(SEL);
 if(!cams.length){ msg('Escolha de 1 a 4 cameras.'); return; }
 var body={cameras:cams,dias:dias};
 if($('u_novo').checked){ body.email=$('n_email').value.trim(); body.password=$('n_senha').value; body.full_name=$('n_nome').value.trim();
  if(!body.email||!body.password){ msg('Preencha email e senha do novo login.'); return; } }
 else { body.user_id=$('u_exist').value; if(!body.user_id){ msg('Selecione um usuario.'); return; } }
 try{ var r=await api('POST','/api/demo/criar',body); msg('Demonstracao criada! Expira em '+esc(r.expira)+'.',true);
  SEL={}; ['n_nome','n_email','n_senha'].forEach(function(i){$(i).value='';}); renderCams(); loadList(); }
 catch(e){ msg('Erro: '+e.message); }
}
async function loadList(){ try{ var L=(await api('GET','/api/demo/listar'))||[];
 $('rows').innerHTML=L.map(function(d){
  var badge=d.status==='ativo'?'<span class="pill ok">Ativo</span>':(d.status==='expirado'?'<span class="pill off">Expirado</span>':'<span class="pill" style="color:var(--bad)">Revogado</span>');
  var btn=d.status==='ativo'?'<button class="act" style="color:var(--bad)" data-rev="'+esc(d.id)+'">revogar</button>':'';
  return '<tr><td><b>'+esc(d.nome||d.email||d.user_id)+'</b><div style="color:var(--muted);font-size:12px">'+esc(d.email||'')+'</div></td><td>'+d.n_cameras+'</td><td>'+esc(d.expira||'')+'</td><td>'+badge+'</td><td style="text-align:right">'+btn+'</td></tr>';
 }).join('')||'<tr><td colspan="5" class="center">Nenhuma demonstracao ainda.</td></tr>'; }catch(e){ msg('Erro na lista: '+e.message); } }
async function revogar(id){ if(!confirm('Revogar o acesso demo? O usuario perde o acesso na hora.'))return;
 try{ await api('POST','/api/demo/'+id+'/revogar'); msg('Acesso revogado.',true); loadList(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""


_EXCLUSOES_BODY = """
<div id="msg" class="msg"></div>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">Pedidos de exclusao de camera dos provedores. <b>Aprovar</b> exclui a camera e ela sai do valor da cobranca. <b>Negar</b> mantem a camera e avisa o provedor (WhatsApp + e-mail).</p>
<div style="font-weight:700;margin-bottom:8px">Pendentes</div>
<div id="pend"><div class="center">carregando...</div></div>
<div style="font-weight:700;margin:22px 0 8px">Decididos recentemente</div>
<div id="rec"></div>
<script>
window.PAGE_INIT=load;
function dt(s){ var d=(''+(s||'')).slice(0,10).split('-'); var t=(''+(s||'')).slice(11,16); return d.length===3?(d[2]+'/'+d[1]+' '+t):s; }
async function load(){ try{ var j=await api('GET','/api/comercial/exclusoes');
  $('pend').innerHTML=(j.pendentes||[]).map(function(x){
   return '<div style="background:var(--surface);border:1px solid rgba(248,113,113,.45);border-left:4px solid var(--bad);border-radius:10px;padding:12px 14px;margin-bottom:10px">'+
    '<div style="font-weight:700;font-size:15px">'+esc(x.provedor_nome||'-')+' &middot; '+esc(x.camera_nome||'-')+'</div>'+
    '<div style="color:var(--ink);font-size:13px;margin:6px 0">Motivo: '+esc(x.motivo||'-')+'</div>'+
    '<div style="color:var(--muted);font-size:12px">Solicitado em '+dt(x.criado)+(x.cliente_nome?(' &middot; cliente: '+esc(x.cliente_nome)):'')+'</div>'+
    '<div style="margin-top:10px;display:flex;gap:8px"><button class="btn-primary" style="background:var(--ok);border-color:var(--ok);color:#04160c" onclick="decidir(\\''+x.id+'\\',true)">Aprovar exclusao</button>'+
    '<button class="btn-primary" style="background:var(--bad);border-color:var(--bad);color:#1a0505" onclick="decidir(\\''+x.id+'\\',false)">Negar</button></div></div>';
  }).join('')||'<div class="center">Nenhum pedido pendente.</div>';
  $('rec').innerHTML=(j.recentes||[]).map(function(x){ var ap=x.status==='aprovada';
   return '<div style="padding:9px 12px;border-bottom:1px solid var(--border);font-size:13px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>'+esc(x.provedor_nome||'-')+'</b> <span style="color:var(--muted)">'+esc(x.camera_nome||'-')+'</span> <span class="pill '+(ap?'ok':'')+'" style="'+(ap?'':'color:var(--bad);border-color:rgba(248,113,113,.4)')+'">'+(ap?'aprovada':'negada')+'</span> <span style="color:var(--muted)">'+dt(x.decidido_em||x.criado)+'</span></div>';
  }).join('')||'<div style="color:var(--muted);font-size:13px">Nenhum ainda.</div>';
 }catch(e){ msg('Erro (logado como admin?): '+e.message); } }
async function decidir(id,aprovar){ var resp='';
 if(!aprovar){ resp=window.prompt('Motivo da negacao (opcional, vai para o provedor):')||''; }
 if(!confirm(aprovar?'Aprovar? A camera sera EXCLUIDA e sai da cobranca do provedor.':'Negar este pedido de exclusao?'))return;
 try{ await api('POST','/api/comercial/exclusoes/'+id+'/decidir',{aprovar:aprovar,resposta:resp}); msg(aprovar?'Aprovado. Camera excluida e provedor avisado.':'Negado. Provedor avisado.',true); load(); }catch(e){ msg('Erro: '+e.message); } }
</script>
"""
