# -*- coding: utf-8 -*-
"""Indexador da Fase 2 (roda na VPS, cron corexia). Enumera segmentos das cameras com
busca_ia (ultimos N dias), assina a URL e manda pro servico CLIP da Xeon (via tunel) indexar.
Idempotente (estado local) + teto por rodada + lock (sem sobreposicao)."""
import sys, os, json, time, sqlite3

def main():
    sys.path.insert(0, "/opt/corexia/app")
    for ln in open("/opt/corexia/app/.env"):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); os.environ.setdefault(k, v)
    import fcntl, requests
    import server
    from datetime import datetime, timedelta

    SVC = os.getenv("BUSCA_SVC_URL", "http://127.0.0.1:9765")
    SEC = os.getenv("BUSCA_SVC_SECRET", "")
    DAYS = int(os.getenv("BUSCA_INDEX_DAYS", "7"))
    STEP = float(os.getenv("BUSCA_INDEX_STEP", "3"))
    CAP = int(os.getenv("BUSCA_INDEX_CAP", "25"))
    STATE = "/opt/corexia/app/busca_index_state.db"

    lf = open("/tmp/busca_indexer.lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        print("ja rodando; saindo"); return

    db = sqlite3.connect(STATE)
    db.execute("CREATE TABLE IF NOT EXISTS done(cam TEXT, arquivo TEXT, ts REAL, PRIMARY KEY(cam,arquivo))")
    db.commit()

    def is_done(cam, arq):
        return db.execute("SELECT 1 FROM done WHERE cam=? AND arquivo=?", (cam, arq)).fetchone() is not None

    def mark(cam, arq):
        db.execute("INSERT OR REPLACE INTO done VALUES(?,?,?)", (cam, arq, time.time())); db.commit()

    cams = [o for o in server._todas_cameras() if o.get("busca_ia")]
    print("[%s] cameras com busca_ia: %d" % (time.strftime("%H:%M:%S"), len(cams)))
    today = datetime.now()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS)]

    done_run = 0
    for o in cams:
        if done_run >= CAP:
            break
        cid = o.get("id")
        cliente = server._rec_safe(o.get("cliente_nome") or "SEM_CLIENTE")
        camnome = server._rec_safe(o.get("nome") or "")
        for date in dates:
            if done_run >= CAP:
                break
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
            except Exception:
                continue
            folder = "%s/%s/%s" % (cliente, server._rec_week(dt), date)
            for f in server._rec_browse(folder):
                if done_run >= CAP:
                    break
                nm = f.get("name") or ""
                if f.get("is_dir") or not nm.endswith(".mp4") or not nm.startswith(camnome + "_"):
                    continue
                if is_done(cid, nm):
                    continue
                inicio = nm[len(camnome) + 1:-4].replace("-", ":")
                relpath = folder + "/" + nm
                url = server._rec_signed_url(relpath, ttl=6 * 3600)
                try:
                    r = requests.post(SVC + "/index", timeout=240,
                                      headers={"X-Busca-Secret": SEC, "Content-Type": "application/json"},
                                      json={"camera_key": cid, "date": date, "arquivo": nm,
                                            "inicio": inicio, "signed_url": url, "step": STEP})
                    if r.status_code == 200:
                        n = r.json().get("indexed", 0)
                        mark(cid, nm); done_run += 1
                        print("  %s %s/%s -> %s vetores" % (date, cid[:8], nm[-14:], n))
                    else:
                        print("  falha %s %s: HTTP %s" % (cid[:8], nm[-14:], r.status_code))
                except Exception as e:
                    print("  erro %s %s: %s" % (cid[:8], nm[-14:], str(e)[:90]))
    print("rodada: %d segmentos processados" % done_run)

if __name__ == "__main__":
    main()
