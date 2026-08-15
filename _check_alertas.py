import requests
d = requests.get("http://localhost:8000/api/entities/Alerta").json()
d = d if isinstance(d, list) else d.get("items", [])
d = sorted(d, key=lambda a: a.get("created_date", ""), reverse=True)
print(len(d), "alertas no total")
for a in d[:7]:
    print("-", a.get("tipo"), "|", a.get("camera_nome"), "|", a.get("confianca"),
          "% | img:", a.get("imagem_url"), "|", (a.get("descricao") or "")[:45])
