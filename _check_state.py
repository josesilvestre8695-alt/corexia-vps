import requests
B = "http://localhost:8000"
cams = requests.get(B + "/api/entities/Camera").json()
print(len(cams), "cameras:")
for c in cams:
    print("  -", c.get("nome"), "| embed:", bool(c.get("embed_url")),
          "| rtsp:", bool(c.get("rtsp_url")), "| status:", c.get("status"))
al = requests.get(B + "/api/entities/Alerta").json()
print(len(al), "alertas")
