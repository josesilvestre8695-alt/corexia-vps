#!/usr/bin/env python3
# Watchdog Corexia (Camada 1): checa o backend, reinicia se cair e alerta por WhatsApp (Z-API) + e-mail (Gmail).
# Roda por cron do root:  * * * * * ... monitor_corexia.py check   |   @reboot ... monitor_corexia.py boot
import os, sys, json, time, ssl, smtplib, subprocess
from email.message import EmailMessage
try:
    import requests
except Exception:
    requests = None

HERE = "/home/tvlan/corexia-vision-ai"
STATE = "/home/tvlan/.corexia_monitor_state.json"
TAG = "[monitor]"

def load_env(path):
    d = {}
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d

ENV = load_env(os.path.join(HERE, ".env"))
BK = load_env("/home/tvlan/.corexia_backup.env")
MON = load_env("/home/tvlan/.corexia_monitor.env")
ALERT_WA = MON.get("ALERT_WHATSAPP", "")
ALERT_EMAIL = MON.get("ALERT_EMAIL", "")

def _num(s):
    s = "".join(ch for ch in str(s) if ch.isdigit())
    if s and not s.startswith("55"):
        s = "55" + s
    return s

def send_whatsapp(msg):
    inst = ENV.get("ZAPI_INSTANCE_ID", ""); tok = ENV.get("ZAPI_TOKEN", ""); cli = ENV.get("ZAPI_CLIENT_TOKEN", "")
    if not (requests and ALERT_WA and inst and tok):
        print(TAG, "whatsapp: config incompleta"); return False
    try:
        url = "https://api.z-api.io/instances/%s/token/%s/send-text" % (inst, tok)
        r = requests.post(url, json={"phone": _num(ALERT_WA), "message": msg},
                          headers={"Content-Type": "application/json", "Client-Token": cli}, timeout=25)
        print(TAG, "whatsapp", r.status_code, r.text[:120]); return r.ok
    except Exception as e:
        print(TAG, "whatsapp erro", str(e)[:120]); return False

def send_email(subj, body):
    user = BK.get("MAIL_USER", ""); pw = BK.get("MAIL_PASS", "")
    if not (user and pw and ALERT_EMAIL):
        print(TAG, "email: config incompleta"); return False
    try:
        m = EmailMessage(); m["From"] = user; m["To"] = ALERT_EMAIL; m["Subject"] = subj; m.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
            s.login(user, pw); s.send_message(m)
        print(TAG, "email ok"); return True
    except Exception as e:
        print(TAG, "email erro", str(e)[:120]); return False

def alert(subj, msg):
    print(TAG, "ALERTA:", subj)
    send_whatsapp(msg); send_email(subj, msg)

def health_ok():
    if not requests:
        return True
    try:
        return requests.get("http://localhost:8000/", timeout=8).status_code < 500
    except Exception:
        return False

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"state": "up", "fails": 0}

def save_state(st):
    try:
        json.dump(st, open(STATE, "w"))
    except Exception as e:
        print(TAG, "state erro", e)

def now():
    return time.strftime("%d/%m %H:%M")

def do_check():
    st = load_state()
    if health_ok():
        if st.get("state") == "down":
            alert("Corexia: VOLTOU ao ar", "Corexia voltou ao ar as %s. O painel esta respondendo normalmente." % now())
        save_state({"state": "up", "fails": 0}); return
    fails = int(st.get("fails", 0)) + 1
    if st.get("state") == "up" and fails >= 2:
        alert("Corexia: FORA DO AR", "Corexia NAO esta respondendo (2 checagens seguidas) as %s. Reiniciando o backend automaticamente..." % now())
        try:
            subprocess.run(["systemctl", "restart", "corexia-backend"], timeout=60)
        except Exception as e:
            print(TAG, "restart erro", e)
        save_state({"state": "down", "fails": fails})
    else:
        save_state({"state": st.get("state", "up"), "fails": fails})

def do_boot():
    up = False
    for _ in range(18):
        if health_ok():
            up = True; break
        time.sleep(5)
    if up:
        alert("Corexia: servidor reiniciou", "O servidor Corexia REINICIOU e o painel ja voltou as %s. Uptime baixo - verifique se foi queda de energia/crash." % now())
        save_state({"state": "up", "fails": 0})
    else:
        alert("Corexia: reboot SEM backend", "O servidor reiniciou as %s mas o painel NAO subiu em 90s. Verifique com urgencia." % now())
        save_state({"state": "down", "fails": 2})

def do_test():
    ok_wa = send_whatsapp("Teste do monitor Corexia as %s. Se voce recebeu isto, os alertas de queda por WhatsApp estao OK." % now())
    ok_ml = send_email("Corexia: teste de alerta", "Teste do monitor Corexia as %s. Se voce recebeu este e-mail, os alertas por e-mail estao OK." % now())
    print(TAG, "TESTE -> whatsapp:", ok_wa, "| email:", ok_ml)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": do_check, "boot": do_boot, "test": do_test}.get(mode, lambda: print("modo invalido:", mode))()
