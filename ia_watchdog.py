#!/usr/bin/env python3
# IA Watchdog Corexia: monitora a SAUDE do pipeline de IA e avisa URGENTE (WhatsApp + e-mail + dashboard).
# Reaproveita o encanamento de alerta do monitor_corexia.py. Roda por cron do root a cada 1 min.
# Modos:  check (padrao, cron) | status (imprime, sem alertar) | test (manda 1 alerta de teste)
import os, sys, json, time, subprocess
sys.path.insert(0, "/home/tvlan/corexia-vision-ai")
import monitor_corexia as M   # send_whatsapp, send_email, alert, ENV, now, requests

HERE = "/home/tvlan/corexia-vision-ai"
STATE = "/home/tvlan/.corexia_ia_watchdog.json"
HEALTH_FILE = os.path.join(HERE, "ia_health.json")
TAG = "[ia-watchdog]"
REMIND_SEC = 15 * 60      # relembra a cada 15 min enquanto continua fora
PROBE_EVERY = 5 * 60      # sondagem ativa (Gemini/Roboflow) a cada 5 min
FREEZE_SEC = 12 * 60      # worker sem log ha mais que isso = congelado
requests = M.requests

# ---------- helpers de sistema ----------
def svc(name):
    return subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True).stdout.strip()

def last_log_age_sec(unit):
    r = subprocess.run(["journalctl", "-u", unit, "-n", "1", "-o", "short-unix", "--no-pager"],
                       capture_output=True, text=True)
    line = r.stdout.strip()
    if not line:
        return None
    try:
        return time.time() - float(line.split()[0])
    except Exception:
        return None

def journal_has(units, pattern, since):
    pat = pattern.lower()
    for u in units:
        r = subprocess.run(["journalctl", "-u", u, "--since", since, "--no-pager"],
                           capture_output=True, text=True)
        if pat in r.stdout.lower():
            return True
    return False

def get_key(var):
    v = M.ENV.get(var) or os.getenv(var) or ""
    if v:
        return v
    try:
        pids = subprocess.run(["pgrep", "-f", "detector_nvdec.py"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            for kv in open("/proc/%s/environ" % pid, "rb").read().split(b"\0"):
                if kv.startswith(var.encode() + b"="):
                    return kv.split(b"=", 1)[1].decode()
    except Exception:
        pass
    return ""

# ---------- sondagens ativas ----------
def gemini_probe(key):
    if not key:
        return ("nokey", 0)
    model = M.ENV.get("GEMINI_MODEL", "gemini-2.5-flash")
    last = ""
    for _try in range(2):   # 1 retry: soluco transitorio nao alerta (so falha 2x seguidas)
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key),
                json={"contents": [{"parts": [{"text": "ping"}]}]}, timeout=20)
            if r.status_code == 200:
                return ("ok", 200)
            if r.status_code in (401, 403):
                return ("auth", r.status_code)
            if r.status_code == 429:
                return ("quota", 429)
            return ("erro", r.status_code)
        except Exception as e:
            last = str(e)[:80]
            if _try == 0:
                time.sleep(2)
    return ("down", last)

def roboflow_probe(key):
    if not key:
        return ("nokey", 0)
    last = ""
    for _try in range(2):   # 1 retry: soluco transitorio nao alerta
        try:
            r = requests.get("https://api.roboflow.com/?api_key=%s" % key, timeout=20)
            if r.status_code == 200:
                return ("ok", 200)
            if r.status_code in (401, 403):
                return ("auth", r.status_code)
            if r.status_code == 429:
                return ("quota", 429)
            return ("erro", r.status_code)
        except Exception as e:
            last = str(e)[:80]
            if _try == 0:
                time.sleep(2)
    return ("down", last)

def _probe_verdict(pr, nome):
    st, code = pr[0], pr[1]
    if st == "ok":
        return True, "ok"
    if st == "nokey":
        return True, "sem chave (%s desligado)" % nome
    if st == "quota":
        return False, "SEM CREDITO / cota excedida (HTTP 429)"
    if st == "auth":
        return False, "chave invalida ou negada (HTTP %s)" % code
    if st == "down":
        return False, "fora do ar: %s" % code
    return False, "erro HTTP %s" % code

def _ia_titulo(nome, det):
    """Titulo especifico conforme o tipo de falha (credito x conexao x chave)."""
    d = (det or "").lower()
    if "credito" in d or "cota" in d or "429" in d:
        return "%s SEM CREDITO (cota excedida)" % nome
    if "chave invalida" in d or "negada" in d:
        return "%s: chave invalida / negada" % nome
    return "%s fora do ar (falha de conexao)" % nome


# ---------- checagens ----------
def gpu_check():
    r = subprocess.run(["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, "nvidia-smi falhou: " + (r.stderr.strip()[:100] or "sem saida")
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return False, "esperava 2 GPUs, encontrei %d" % len(lines)
    quentes = []
    for l in lines:
        p = [x.strip() for x in l.split(",")]
        try:
            if int(p[1]) >= 90:
                quentes.append("%s %sC" % (p[0], p[1]))
        except Exception:
            pass
    if quentes:
        return False, "GPU superaquecida: " + ", ".join(quentes)
    return True, "2x RTX 3080 ok"

def disk_check_path(path, label, warn=75, crit=90):
    try:
        out = subprocess.run(["df", "--output=pcent", path], capture_output=True, text=True, timeout=30).stdout.strip().splitlines()
        pct = int(out[-1].strip().replace("%", ""))
    except Exception as e:
        return True, "df erro (%s): %s" % (label, str(e)[:60]), "aviso"
    if pct >= crit:
        return False, "disco %s em %d%% (critico)" % (label, pct), "critico"
    if pct >= warn:
        return False, "disco %s em %d%% (limite %d%%)" % (label, pct, warn), "aviso"
    return True, "disco %s em %d%%" % (label, pct), "aviso"

def disk_check():
    return disk_check_path("/", "Xeon (/)", 75, 90)

WORKERS = ["vigia_nvdec@0", "vigia_nvdec@1"]

def run_all_checks(probe):
    out = []
    for unit, label in [("corexia-backend", "Backend/API"), ("mediamtx", "Streaming (MediaMTX)"),
                        ("vigia_nvdec@0", "Detector IA worker 0"), ("vigia_nvdec@1", "Detector IA worker 1")]:
        a = svc(unit)
        out.append({"key": "svc_" + unit, "ok": a == "active", "sev": "critico",
                    "titulo": label + " parou", "detalhe": "systemctl is-active: " + (a or "?")})
    for unit, label in [("vigia_nvdec@0", "worker 0"), ("vigia_nvdec@1", "worker 1")]:
        if svc(unit) == "active":
            age = last_log_age_sec(unit)
            frozen = age is not None and age > FREEZE_SEC
            out.append({"key": "freeze_" + unit, "ok": not frozen, "sev": "critico",
                        "titulo": "Detector IA %s congelado" % label,
                        "detalhe": "sem atividade ha %d min" % (int((age or 0) / 60))})
    ok, det = gpu_check()
    out.append({"key": "gpu", "ok": ok, "sev": "critico", "titulo": "GPU com problema", "detalhe": det})
    # Gemini: sondagem (autoritativa) + log como reforco
    if "gemini" in probe:
        ok, det = _probe_verdict(probe["gemini"], "Gemini")
    else:
        ok, det = (not journal_has(WORKERS, "FORA DO AR", "-15 min"),
                   "circuito abriu no log (aguardando sondagem)")
    out.append({"key": "gemini", "ok": ok, "sev": "critico",
                "titulo": ("Gemini (verificador IA)" if ok else _ia_titulo("Gemini (verificador IA)", det)), "detalhe": det})
    # Roboflow: sondagem + log
    if "roboflow" in probe:
        ok, det = _probe_verdict(probe["roboflow"], "Roboflow")
        if ok and journal_has(WORKERS, "roboflow", "-15 min"):
            ok, det = False, "erros de Roboflow no log do detector (ultimos 15 min)"
    else:
        ok, det = (not journal_has(WORKERS, "roboflow", "-15 min"), "aguardando sondagem")
    out.append({"key": "roboflow", "ok": ok, "sev": "critico",
                "titulo": ("Roboflow (modelos IA)" if ok else _ia_titulo("Roboflow (modelos IA)", det)), "detalhe": det})
    ok, det, sev = disk_check()
    out.append({"key": "disk", "ok": ok, "sev": sev, "titulo": "Disco da Xeon quase cheio", "detalhe": det})
    ok, det, sev = disk_check_path("/mnt/corexia-storage", "Storage (49TB)", 75, 90)
    out.append({"key": "disk_storage", "ok": ok, "sev": sev, "titulo": "Disco da Storage quase cheio", "detalhe": det})
    return out

# ---------- estado + saida ----------
def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"checks": {}, "last_probe": 0, "probe": {}}

def save_state(st):
    try:
        json.dump(st, open(STATE, "w"))
    except Exception as e:
        print(TAG, "state erro", e)

def write_health_file(results, checks_state):
    crit = [r for r in results if not r["ok"] and r["sev"] == "critico"]
    warn = [r for r in results if not r["ok"] and r["sev"] == "aviso"]
    data = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "overall": "critico" if crit else ("aviso" if warn else "ok"),
            "problemas": [{"titulo": r["titulo"], "detalhe": r["detalhe"], "sev": r["sev"],
                           "since": checks_state.get(r["key"], {}).get("since")}
                          for r in results if not r["ok"]],
            "checks": [{"key": r["key"], "ok": r["ok"], "titulo": r["titulo"],
                        "detalhe": r["detalhe"], "sev": r["sev"]} for r in results]}
    try:
        tmp = HEALTH_FILE + ".tmp"
        json.dump(data, open(tmp, "w"))
        os.replace(tmp, HEALTH_FILE)
        os.chmod(HEALTH_FILE, 0o644)
    except Exception as e:
        print(TAG, "health file erro", e)
    return data

def notify_group(subj, items):
    linhas = "\n".join("- %s (%s)" % (r["titulo"], r["detalhe"]) for r in items)
    M.alert(subj, "%s\n\n%s\n\nHorario: %s" % (subj, linhas, M.now()))

def do_check():
    st = load_state()
    now_ts = time.time()
    prev = st.get("checks", {})
    if now_ts - st.get("last_probe", 0) >= PROBE_EVERY:
        st["probe"] = {"gemini": gemini_probe(get_key("GEMINI_API_KEY")),
                       "roboflow": roboflow_probe(get_key("ROBOFLOW_API_KEY"))}
        st["last_probe"] = now_ts
    probe = st.get("probe", {})
    results = run_all_checks(probe)
    new_checks = {}
    new_fail, remind, recovered = [], [], []
    for r in results:
        k = r["key"]
        p = prev.get(k, {"ok": True, "since": now_ts, "last_alert": 0})
        if r["ok"]:
            if not p.get("ok", True):
                recovered.append(r)
            new_checks[k] = {"ok": True, "since": now_ts, "last_alert": 0}
        else:
            if p.get("ok", True):
                new_fail.append(r)
                new_checks[k] = {"ok": False, "since": now_ts, "last_alert": now_ts}
            else:
                la = p.get("last_alert", 0)
                if now_ts - la >= REMIND_SEC:
                    remind.append(r)
                    la = now_ts
                new_checks[k] = {"ok": False, "since": p.get("since", now_ts), "last_alert": la}
    st["checks"] = new_checks
    save_state(st)
    write_health_file(results, new_checks)
    if new_fail:
        notify_group("\U0001F534 IA COREXIA - PROBLEMA DETECTADO", new_fail)
    if remind:
        notify_group("\U0001F534 IA COREXIA - AINDA FORA DO AR", remind)
    if recovered:
        notify_group("✅ IA COREXIA - NORMALIZADO", recovered)
    print(TAG, M.now(), "checagens:", len(results),
          "| falhas:", sum(1 for r in results if not r["ok"]),
          "| novas:", len(new_fail), "| relembrar:", len(remind), "| resolvidas:", len(recovered))

def do_status():
    probe = {"gemini": gemini_probe(get_key("GEMINI_API_KEY")),
             "roboflow": roboflow_probe(get_key("ROBOFLOW_API_KEY"))}
    results = run_all_checks(probe)
    print(TAG, "STATUS", M.now())
    for r in results:
        print("  [%s] %s :: %s" % ("OK " if r["ok"] else "FALHA", r["titulo"], r["detalhe"]))
    d = write_health_file(results, {})
    print(TAG, "overall:", d["overall"])

def do_test():
    M.alert("\U0001F9EA IA Corexia - teste de alerta",
            "Teste do IA Watchdog as %s. Se voce recebeu no WhatsApp E no e-mail, os avisos de saude da IA estao OK." % M.now())

MODE = sys.argv[1] if len(sys.argv) > 1 else "check"
{"check": do_check, "status": do_status, "test": do_test}.get(
    MODE, lambda: print("modo invalido:", MODE))()
