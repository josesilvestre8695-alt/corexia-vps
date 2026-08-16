#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alerta de camera FORA DO AR (offline) para cameras JA migradas ao ingest Corexia.
Fonte: mediamtx_ready.json (empurrado pelo storage 122). So cameras monitor_offline=true que ja estiveram online.
Alerta 1x ao passar de CAM_OFFLINE_MIN (default 10 min); NORMALIZADA ao voltar. Envia via Z-API do sistema (env) p/ NumeroPlantao ativos.
Modos: check (cron) | status | testalert
"""
import os, sys, json, time, sqlite3, requests

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass
DB = os.path.join(HERE, "corexia.db")
STATE = os.path.join(HERE, "cam_offline_state.json")
READY_FILE = os.path.join(HERE, "mediamtx_ready.json")
READY_MAX_AGE = int(os.getenv("READY_MAX_AGE", "360"))
THRESHOLD = int(os.getenv("CAM_OFFLINE_MIN", "10")) * 60


def _plantao_numbers():
    out = []
    try:
        c = sqlite3.connect(DB)
        for (d,) in c.execute("select data from entities where entity='NumeroPlantao'"):
            x = json.loads(d)
            n = "".join(ch for ch in (x.get("numero") or x.get("telefone") or "") if ch.isdigit())
            if n and x.get("ativo", True) is not False:
                if len(n) <= 11:
                    n = "55" + n
                out.append(n)
        c.close()
    except Exception:
        pass
    return list(dict.fromkeys(out))


def notify(subj, msg):
    full = subj + "\n" + msg
    inst = os.getenv("ZAPI_INSTANCE_ID", ""); tok = os.getenv("ZAPI_TOKEN", ""); ct = os.getenv("ZAPI_CLIENT_TOKEN", "")
    nums = _plantao_numbers()
    if not (inst and tok and nums):
        print("notify: config incompleta (inst=%s tok=%s nums=%d)" % (bool(inst), bool(tok), len(nums)), flush=True)
        return
    for n in nums:
        try:
            requests.post("https://api.z-api.io/instances/%s/token/%s/send-text" % (inst, tok),
                          headers={"Content-Type": "application/json", "Client-Token": ct},
                          json={"phone": n, "message": full}, timeout=20)
            print("notify: enviado p/", n, flush=True)
        except Exception as e:
            print("notify: falhou", n, str(e)[:80], flush=True)


def ready_keys():
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
            o["id"] = r["id"]
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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "testalert":
        notify("\U0001F9EA Teste Corexia", "Teste do alerta de camera offline as %s. Se recebeu isto, o canal de alerta esta OK." % time.strftime("%d/%m %H:%M"))
        return
    keys, api_ok = ready_keys()
    cams = monitored_cams()
    if mode == "status":
        on = sum(1 for o in cams if o["stream_key"] in keys)
        print("ok=%s monitoradas=%d online=%d offline=%d | plantao=%d num(s)" % (api_ok, len(cams), on, len(cams) - on, len(_plantao_numbers())))
        for o in cams:
            print("  [%s] %s" % ("ON " if o["stream_key"] in keys else "OFF", o.get("nome", "?")))
        return
    if not api_ok:
        print("estado do storage indisponivel - nao avalio", flush=True)
        return
    st = load_state()
    now = time.time()
    alive = set()
    for o in cams:
        cid = o["id"]; key = o["stream_key"]; nome = o.get("nome", "?")
        alive.add(cid)
        rec = st.get(cid, {})
        if key in keys:
            if rec.get("alerted"):
                notify("✅ Camera NORMALIZADA", "%s voltou a transmitir." % nome)
            st[cid] = {"seen": True}
        else:
            if not rec.get("seen"):
                if rec:
                    st[cid] = rec
                continue
            if "off_since" not in rec:
                rec = {"seen": True, "off_since": now, "alerted": False}
            off_for = now - rec["off_since"]
            if off_for >= THRESHOLD and not rec.get("alerted"):
                notify("\U0001F534 Camera FORA DO AR", "%s esta offline ha %d min (cliente: %s)." % (nome, int(off_for // 60), o.get("cliente_nome", "")))
                rec["alerted"] = True
            st[cid] = rec
    for cid in [c for c in st if c not in alive]:
        st.pop(cid, None)
    save_state(st)


if __name__ == "__main__":
    main()
