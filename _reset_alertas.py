"""Zera alertas (falsos positivos do servlink) + esvazia o fallback cameras_saas.json. Sem auth (sqlite direto)."""
import sqlite3, os, glob
HERE = os.path.dirname(os.path.abspath(__file__))
c = sqlite3.connect(os.path.join(HERE, "corexia.db"))
n = c.execute("DELETE FROM entities WHERE entity='Alerta'").rowcount
c.execute("DELETE FROM alertas")
c.commit(); c.close()
for f in glob.glob(os.path.join(HERE, "alertas_img", "*.jpg")):
    try: os.remove(f)
    except OSError: pass
open(os.path.join(HERE, "cameras_saas.json"), "w").write("[]")   # mata o fallback antigo
print(f"alertas apagados: {n} | cameras_saas.json esvaziado")
