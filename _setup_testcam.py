import requests
B = "http://localhost:8000"; S = "corexia-webhook-2024"
cam = {"nome": "Camera de Teste (video)", "rtsp_url": "http://localhost:8888/stream.m3u8",
       "cliente_nome": "Teste Video", "cliente_telefone": "81997335544", "status": "online"}
r = requests.post(B + "/api/entities/Camera", json=cam)
print("cadastrada:", r.json().get("nome"), "| id:", r.json().get("id"))
d = requests.post(B + "/listarCamerasIA", json={"secret": S, "validar": True}, timeout=30).json()
print("detector enxerga:", d.get("online_validas"), "validas de", d.get("total"))
for c in d.get("cameras", []):
    print("  -", c["nome"], "| valido:", c.get("stream_valido"), "| url:", c.get("stream_url"))
