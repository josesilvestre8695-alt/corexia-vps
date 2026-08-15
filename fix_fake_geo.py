"""Remove a geo gravada pela logica antiga para IPs privados.

Esses pontos apontavam para a localizacao do proprio servidor, nao do visitante.
Eventos com IP publico (geo real) sao preservados.
"""
import re
import sqlite3


def is_private(ip):
    if not ip:
        return True
    ip = ip.replace("::ffff:", "")
    if ip in ("::1", "localhost"):
        return True
    if ip.startswith(("127.", "10.", "192.168.", "fe80", "fc", "fd")):
        return True
    return bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip))


con = sqlite3.connect("lp_analytics.db")
rows = con.execute("SELECT id, ip FROM events WHERE lat IS NOT NULL").fetchall()
alvos = [r[0] for r in rows if is_private(r[1])]
con.executemany(
    "UPDATE events SET city=NULL, region=NULL, lat=NULL, lng=NULL WHERE id=?",
    [(i,) for i in alvos],
)
con.commit()
restantes = con.execute("SELECT COUNT(*) FROM events WHERE lat IS NOT NULL").fetchone()[0]
print("geo falsa removida de %d evento(s); %d com geo real preservados" % (len(alvos), restantes))
