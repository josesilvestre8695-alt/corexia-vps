import sqlite3, glob, requests
c = sqlite3.connect("corexia.db")
r = c.execute("SELECT s.token FROM sessions s JOIN users u ON s.user_id=u.id WHERE u.role='admin' ORDER BY s.created DESC LIMIT 1").fetchone()
tok = r[0] if r else ""
cid = None
for p in glob.glob("gravacoes_live/*/index.m3u8"):
    cid = p.split("/")[1]; break
print("cam:", cid, "| admin token:", (tok[:12] + "...") if tok else "(NENHUM admin logado)")
if tok and cid:
    resp = requests.get(f"http://localhost:8000/camthumb/{cid}?t={tok}", timeout=20)
    print("autenticado: HTTP", resp.status_code, "|", resp.headers.get("content-type"), "|", len(resp.content), "bytes")
    resp2 = requests.get(f"http://localhost:8000/camthumb/{cid}?t={tok}", timeout=20)
    print("2a (cache): HTTP", resp2.status_code, "|", len(resp2.content), "bytes")
