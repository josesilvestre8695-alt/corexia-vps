import sqlite3, json, datetime, secrets

CAM_ID = "6313df722d27e27d7999fb78"
STREAM = "https://live41.analitico.app.br/stream/get/cameratest2og0g.m3u8?token=cameratest2og0g"

def now(): return datetime.datetime.utcnow().isoformat()

c = sqlite3.connect("corexia.db"); c.row_factory = sqlite3.Row

# 1) cliente "Corexia Escritorio" (cria se nao existir)
cli = c.execute("SELECT id, data FROM entities WHERE entity='Cliente' AND json_extract(data,'$.nome')='Corexia Escritorio'").fetchone()
if cli:
    cli_id = cli["id"]
    print("cliente ja existia:", cli_id)
else:
    cli_id = secrets.token_hex(12)
    c.execute("INSERT INTO entities (entity,id,data,created_date,updated_date) VALUES ('Cliente',?,?,?,?)",
              (cli_id, json.dumps({"nome": "Corexia Escritorio", "status": "ativo", "provedor_id": "",
                                    "endereco": "rua Altemar dutra", "telefone": ""}), now(), now()))
    print("cliente criado:", cli_id)

# 2) conserta a camera: stream direto no rtsp_url, embed quebrado fora, cliente vinculado
r = c.execute("SELECT data FROM entities WHERE id=?", (CAM_ID,)).fetchone()
d = json.loads(r["data"])
d["rtsp_url"] = STREAM
d["embed_url"] = ""              # era iframe quebrado; ao vivo agora vem do restream /live
d["cliente_id"] = cli_id
d["cliente_nome"] = "Corexia Escritorio"
d["nome"] = "Camera Escritorio (RTMP)"
d["updated_date"] = now()
c.execute("UPDATE entities SET data=?, updated_date=? WHERE id=?", (json.dumps(d), now(), CAM_ID))
c.commit()

print("camera atualizada:")
for k in ("nome", "rtsp_url", "cliente_id", "cliente_nome", "status"):
    print(f"  {k}: {d.get(k)!r}")
