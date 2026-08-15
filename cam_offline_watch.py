#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alerta de camera FORA DO AR (offline) para as cameras JA migradas para o ingest Corexia.
- Fonte de verdade: estado "quem esta publicando" que o STORAGE (122) empurra p/ o VPS em
  mediamtx_ready.json (o VPS nao alcanca a API do MediaMTX diretamente).
- So considera cameras com flag monitor_offline=true.
- So alerta cameras que JA estiveram online alguma vez (evita alertar camera ainda nao cortada).
- Sem flapping: alerta 1x ao passar de CAM_OFFLINE_MIN (default 50 min); NORMALIZADA ao voltar.
- Se o estado do storage estiver velho/ausente, NAO alerta (evita falso-positivo por falha nossa).
Modos:  check (cron, default) | status (imprime, nao alerta)
"""
import os, sys, json, time, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "corexia.db")
STATE = os.path.join(HERE, "cam_offline_state.json")
READY_FILE = os.path.join(HERE, "mediamtx_ready.json")
READY_MAX_AGE = int(os.getenv("READY_MAX_AGE", "360"))   # 6 min: mais velho que isso = nao avalio
THRESHOLD = int(os.getenv("CAM_OFFLINE_MIN", "50")) * 60
VIGGIA = "8ca6232e6761364869986448"


def ready_keys():
    """set de stream_keys publicando agora (do push do storage 122); (set, ok)."""
    try:
        d = json.load(open(READY_FILE))
    except Exception as e:
        print("sem estado do storage (%s)" % e, flush=True)
        return set(), False
    age = time.time() - float(d.get("ts", 0))
    if age > READY_MAX_AGE:
        print("estado do storage velho (%ds) - nao avalio" % int(age), flush=True)
        return set(), False
    return set(str(x) for x in d.get("ready", [])), True


def monitored_cams():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    out = []
    for r in c.execute("SELECT id,data FROM entities WHERE entity='Camera'"):
        o = json.loads(r["data"])
        if o.get("monitor_offline") and o.get("stream_key"):
            o["id"] = r["id"]          # id fica em coluna separada, nao no JSON
            out.append(o)
    c.close()
    return out


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, STATE)


def notify(subj, msg):
    full = subj + "\n" + msg
    try:
        import monitor_corexia as M
        M.send_whatsapp(full)
    except Exception as e:
        print("wa monitor falhou:", e, flush=True)
    try:
        import comercial as C
        nums = C._plantao_prov_nums(VIGGIA) or []
        if nums:
            zi, zt, zc = C._zapi_do_provedor(VIGGIA)
            for n in nums:
                C._zapi_send(n, full, zi, zt, zc)
    except Exception as e:
        print("wa provedor falhou:", e, flush=True)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    keys, api_ok = ready_keys()
    cams = monitored_cams()

    if mode == "status":
        on = sum(1 for o in cams if o["stream_key"] in keys)
        print("ok=%s monitoradas=%d online=%d offline=%d" % (api_ok, len(cams), on, len(cams) - on))
        for o in cams:
            print("  [%s] %s" % ("ON " if o["stream_key"] in keys else "OFF", o.get("nome", "?")))
        return

    if not api_ok:
        print("estado do storage indisponivel - nao avalio (evita falso-positivo)", flush=True)
        return

    st = load_state()
    now = time.time()
    alive = set()
    for o in cams:
        cid = o["id"]; key = o["stream_key"]; nome = o.get("nome", "?")
        alive.add(cid)
        rec = st.get(cid, {})
        if key in keys:                       # ONLINE
            if rec.get("alerted"):
                notify("✅ Camera NORMALIZADA - Grupo Viggia", "%s voltou a transmitir." % nome)
            st[cid] = {"seen": True}          # marca que ja esteve online; zera offline/alerted
        else:                                 # OFFLINE
            if not rec.get("seen"):
                # nunca esteve online -> camera ainda nao cortada p/ Corexia: NAO alerta
                if rec:
                    st[cid] = rec
                continue
            if "off_since" not in rec:
                rec = {"seen": True, "off_since": now, "alerted": False}
            off_for = now - rec["off_since"]
            if off_for >= THRESHOLD and not rec.get("alerted"):
                notify("\U0001F534 Camera FORA DO AR - Grupo Viggia",
                       "%s esta offline ha %d min (cliente: %s)." %
                       (nome, int(off_for // 60), o.get("cliente_nome", "")))
                rec["alerted"] = True
            st[cid] = rec
    for cid in [c for c in st if c not in alive]:   # limpa cameras nao mais monitoradas
        st.pop(cid, None)
    save_state(st)


if __name__ == "__main__":
    main()
