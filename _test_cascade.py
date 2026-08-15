import sqlite3, json, datetime, sys, hashlib, os
DB = "corexia.db"
CID = "casc_cliente_test_0001"
UID = "casc_user_test_0001"
CAMID = "casc_cam_test_0001"

def now(): return datetime.datetime.utcnow().isoformat()

def setup():
    c = sqlite3.connect(DB)
    prov = c.execute("select id from entities where entity='Provedor' limit 1").fetchone()
    prov_id = prov[0] if prov else ""
    c.execute("INSERT OR REPLACE INTO entities (entity,id,data,created_date,updated_date) VALUES ('Cliente',?,?,?,?)",
              (CID, json.dumps({"nome": "Cascade Teste", "provedor_id": prov_id, "status": "ativo"}), now(), now()))
    c.execute("INSERT OR REPLACE INTO entities (entity,id,data,created_date,updated_date) VALUES ('Camera',?,?,?,?)",
              (CAMID, json.dumps({"nome": "Cam Cascade", "cliente_id": CID, "provedor_id": prov_id}), now(), now()))
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    c.execute("INSERT OR REPLACE INTO users (id,email,password_hash,full_name,role,provedor_id,cliente_id,status,created) VALUES (?,?,?,?,?,?,?,?,?)",
              (UID, "cascade@teste.com", "x", "Cascade", "cliente", prov_id, CID, "ativo", now()))
    c.execute("INSERT OR REPLACE INTO sessions (token,user_id,created) VALUES (?,?,?)", ("casc_tok_0001", UID, now()))
    c.commit(); c.close()
    print("SETUP: cliente+camera+user+session de teste criados")

def check(msg):
    c = sqlite3.connect(DB)
    cli = c.execute("select count(*) from entities where id=?", (CID,)).fetchone()[0]
    cam = c.execute("select count(*) from entities where id=?", (CAMID,)).fetchone()[0]
    usr = c.execute("select count(*) from users where id=?", (UID,)).fetchone()[0]
    ses = c.execute("select count(*) from sessions where user_id=?", (UID,)).fetchone()[0]
    c.close()
    print(f"{msg}: cliente={cli} camera={cam} user={usr} session={ses}")
    return cli, cam, usr, ses

if sys.argv[1] == "setup":
    setup(); check("apos setup")
elif sys.argv[1] == "check":
    check("estado")
elif sys.argv[1] == "cleanup":
    c = sqlite3.connect(DB)
    c.execute("delete from entities where id in (?,?)", (CID, CAMID))
    c.execute("delete from users where id=?", (UID,))
    c.execute("delete from sessions where user_id=?", (UID,))
    c.commit(); c.close(); print("cleanup ok")
