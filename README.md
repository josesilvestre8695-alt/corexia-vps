# Corexia Vision AI — detecção real-time local

Serviço que roda nas suas 2× RTX 3080, detecta perigo (arma, faca, fogo) em tempo real
nos streams do Flussonic e grava o alerta no seu sistema Base44 (via `webhookAlertas`,
que já cria o Alerta e dispara o WhatsApp).

```
Flussonic (RTSP) -> Roboflow Inference LOCAL (GPU) -> Gemini confirma -> webhookAlertas (Base44)
```

## Passo a passo

### 1. Pré-requisitos da máquina (Xeon + 2× 3080)
- Driver NVIDIA + `nvidia-smi` mostrando as 2 GPUs.
- Python 3.10+ e ffmpeg instalados.
- (Recomendado Ubuntu 22.04; funciona em Windows também.)

### 2. Ambiente
```bash
python -m venv venv
# Linux:  source venv/bin/activate
# Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configurar
- `cp .env.example .env` e preencha:
  - **ROBOFLOW_API_KEY** — roboflow.com → Settings → API Key
  - **MODEL_ID** — escolha um modelo de arma/faca/fogo no Roboflow Universe (ex.: `weapon-detection-xyz/3`)
  - **GEMINI_API_KEY** — aistudio.google.com
  - **WEBHOOK_URL** — no painel Base44, abra a função `webhookAlertas` e copie a URL pública dela
  - **WEBHOOK_SECRET** — o mesmo valor da env `WEBHOOK_SECRET` das functions do Base44 (padrão: `corexia-webhook-2024`)
- `cp cameras.example.json cameras.json` e liste suas câmeras:
  - `rtsp_url` = URL RTSP de play da câmera no Flussonic
  - `camera_id` = ID da câmera no Base44 (p/ resolver o cliente e mandar WhatsApp). Se não tiver, pode pôr `cliente_telefone` direto.
  - `gpu` = 0 ou 1 (divida as câmeras entre as duas placas)

### 4. Testar com 1 câmera
Deixe só 1 câmera no `cameras.json` (gpu 0) e rode:
```bash
CUDA_VISIBLE_DEVICES=0 python detector.py 0
```
Aponte a câmera pra uma imagem de arma (celular) e veja o alerta cair no painel.

### 5. Rodar em produção (2 GPUs)
```bash
# Linux
./run.sh
# Windows: rode run_gpu0.bat e run_gpu1.bat em janelas separadas
```

### 6. 24/7 (Linux, systemd)
Crie serviços `vigia0` (CUDA_VISIBLE_DEVICES=0, `detector.py 0`) e `vigia1`
(CUDA_VISIBLE_DEVICES=1, `detector.py 1`) com `Restart=always`.

## Notas
- **Custo:** Roboflow roda LOCAL (grátis por frame). Gemini só nos suspeitos = centavos/mês.
- **Falso positivo:** o Gemini é o 2º filtro. Se ele errar/quota estourar, o sistema faz *fail-open* (alerta mesmo assim) — melhor um alarme falso do que perder uma arma.
- **Licença:** o `inference` do Roboflow é permissivo. Se trocar por Ultralytics YOLO puro (AGPL) num produto pago, cuidado com a licença.
