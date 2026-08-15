"""Limpa os dados de TESTE pra deixar o painel LIMPO (alertas, cams de teste, imgs)."""
import requests, sqlite3, os, glob
B = "http://localhost:8000"
HERE = os.path.dirname(os.path.abspath(__file__))

# 1) apaga TODOS os alertas (entidade) — dados de teste
al = requests.get(B + "/api/entities/Alerta").json()
for a in al:
    requests.delete(f"{B}/api/entities/Alerta/{a['id']}")
print(f"alertas (entidade) apagados: {len(al)}")

# 2) remove as cameras de TESTE (localhost gun/fire)
cams = requests.get(B + "/api/entities/Camera").json()
rem = 0
for c in cams:
    url = (c.get("rtsp_url") or c.get("stream_url") or "")
    if "localhost:888" in url or "127.0.0.1:888" in url:
        requests.delete(f"{B}/api/entities/Camera/{c['id']}")
        print("  removida cam teste:", c.get("nome"))
        rem += 1
print(f"cameras de teste removidas: {rem}")

# 3) limpa a tabela alertas (painel HTML antigo) + imagens
try:
    c = sqlite3.connect(os.path.join(HERE, "corexia.db"))
    c.execute("DELETE FROM alertas"); c.commit(); c.close()
    print("tabela 'alertas' limpa")
except Exception as e:
    print("aviso tabela alertas:", e)
imgs = glob.glob(os.path.join(HERE, "alertas_img", "*.jpg"))
for f in imgs:
    try: os.remove(f)
    except Exception: pass
print(f"imagens de alerta removidas: {len(imgs)}")
