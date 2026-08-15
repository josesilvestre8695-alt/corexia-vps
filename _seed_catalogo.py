"""Fase 0 — semeia a base de precos da Corexia no store de entidades (idempotente).
Cria/atualiza: Plano (painel_local, painel_cloud), CatalogoIA, CatalogoGravacao.
Rodar:  ./venv/bin/python _seed_catalogo.py
"""
import sqlite3, json, os
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corexia.db")
now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

SEED = [
    ("Plano", "plano_painel_local", {
        "tipo": "painel_local", "nivel": 1, "nome": "Painel Local",
        "descricao": "Grava localmente no provedor. Direito a ate 100 cameras ao vivo.",
        "base_mensal": 797.00, "cameras_ao_vivo_incluidas": 100,
        "camera_extra_mensal": 5.97, "gravacao": "local", "ativo": True,
        # campos que o painel (linhagem Viggia) le p/ exibir o preco no card
        "valor_mensal": 797.00, "value": 797.00,
    }),
    ("Plano", "plano_painel_cloud", {
        "tipo": "painel_cloud", "nivel": 1, "nome": "Painel Cloud",
        "descricao": "Grava na nuvem Corexia. Cobranca por consumo (camera + gravacao + IA).",
        "camera_ao_vivo_mensal": 5.97, "gravacao": "cloud", "ativo": True,
        # cloud e por consumo; exibe "a partir de" o valor da camera ao vivo
        "valor_mensal": 5.97, "value": 5.97,
    }),
    ("CatalogoGravacao", "catalogo_gravacao", {
        "tipo": "gravacao", "unidade": "mensal_por_camera", "ativo": True,
        "tiers": [
            {"dias": 1,   "valor": 9.97},   {"dias": 3,   "valor": 14.97},
            {"dias": 5,   "valor": 20.00},  {"dias": 7,   "valor": 24.97},
            {"dias": 15,  "valor": 39.97},  {"dias": 30,  "valor": 69.97},
            {"dias": 60,  "valor": 129.97}, {"dias": 90,  "valor": 179.97},
            {"dias": 366, "valor": 597.97},
        ],
    }),
    ("CatalogoIA", "catalogo_ia", {
        "tipo": "ia", "unidade": "mensal_por_camera", "ativo": True,
        "itens": [
            {"key": "objetos",  "nome": "IA Objetos",             "valor": 27.00,
             "detecta": ["arma de fogo","faca","toca ninja","linha de intrusao","pessoa caida","moto","capacete de moto","celular"]},
            {"key": "epi",      "nome": "IA EPI",                 "valor": 15.00,
             "detecta": ["uso de equipamento de EPI pelos trabalhadores"]},
            {"key": "veiculos", "nome": "IA Modelos de veiculos", "valor": 27.00,
             "detecta": ["identificacao de modelo de veiculo"]},
            {"key": "face",     "nome": "IA Face",                "valor": 47.00,
             "detecta": ["deteccao de face de pessoas"]},
            {"key": "fogo",     "nome": "IA Fogo e fumaca",       "valor": 27.00,
             "detecta": ["fogo","fumaca"]},
        ],
    }),
]

c = sqlite3.connect(DB)
for entity, eid, data in SEED:
    existe = c.execute("SELECT created_date FROM entities WHERE entity=? AND id=?", (entity, eid)).fetchone()
    created = existe[0] if existe else now
    c.execute("INSERT OR REPLACE INTO entities (entity,id,data,created_date,updated_date) VALUES (?,?,?,?,?)",
              (entity, eid, json.dumps(data, ensure_ascii=False), created, now))
    print(f"  {'atualizado' if existe else 'criado':10} {entity}/{eid}")
c.commit()

print("\n=== catalogo no banco ===")
for entity in ("Plano", "CatalogoIA", "CatalogoGravacao"):
    for (eid, d) in c.execute("SELECT id,data FROM entities WHERE entity=?", (entity,)):
        o = json.loads(d)
        if entity == "Plano":
            base = o.get("base_mensal") or o.get("camera_ao_vivo_mensal")
            print(f"  Plano {o['nome']:14} base/camera=R${base}")
        elif entity == "CatalogoIA":
            print("  IA:", ", ".join(f"{i['nome']}=R${i['valor']}" for i in o["itens"]))
        else:
            print("  Gravacao tiers:", ", ".join(f"{t['dias']}d=R${t['valor']}" for t in o["tiers"]))
c.close()
