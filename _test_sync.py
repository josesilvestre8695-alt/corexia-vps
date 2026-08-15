import sqlite3, json, sys, secrets, datetime

acao = sys.argv[1] if len(sys.argv) > 1 else "check"
TEST_ID = "testsync0000000000000001"

c = sqlite3.connect("corexia.db")
c.row_factory = sqlite3.Row

def cliente_id():
    for r in c.execute("select * from entities where entity='Cliente' limit 1"):
        return r["id"]
    return ""

if acao == "add":
    cid = cliente_id()
    data = {
        "nome": "TESTE SYNC (excluir)", "stream_url": "http://localhost:8888/stream.m3u8",
        "cliente_id": cid, "cliente_nome": "Teste Sync", "cliente_telefone": "",
        "status": "online",
    }
    now = datetime.datetime.utcnow().isoformat()
    c.execute("INSERT OR REPLACE INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              ("Camera", TEST_ID, json.dumps(data), now, now))
    c.commit()
    print("ADD ok -> camera de teste inserida (cliente_id=%s)" % cid)

elif acao == "del":
    c.execute("DELETE FROM entities WHERE id=?", (TEST_ID,))
    c.commit()
    print("DEL ok -> camera de teste removida")

# estado
n = c.execute("select count(*) from entities where id=?", (TEST_ID,)).fetchone()[0]
print("camera de teste no banco:", bool(n))
