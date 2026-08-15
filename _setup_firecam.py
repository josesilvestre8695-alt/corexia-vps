import requests
B = "http://localhost:8000"
cam = {"nome": "Camera Fogo (teste)", "rtsp_url": "http://localhost:8889/stream.m3u8",
       "cliente_nome": "Teste Fogo", "cliente_telefone": "81997335544", "status": "online"}
r = requests.post(B + "/api/entities/Camera", json=cam)
print("cadastrada:", r.json().get("nome"), "| id:", r.json().get("id"))
