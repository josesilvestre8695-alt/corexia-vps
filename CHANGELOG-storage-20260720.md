# Corexia — Gravação em MP4 na máquina de storage (50 TB)

**Data:** 2026-07-20 · **Autor:** dev (assumindo o projeto)
**Objetivo:** armazenar as gravações de todas as câmeras na máquina de HDs (10.93.0.122,
50 TB) e reproduzi-las pelo painel, SEM acoplar à produção (detectores + live view da Xeon).

## Topologia
- **Xeon 10.93.0.126** (produção): backend :8000, detectores, relay HLS ao vivo. Chega à
  storage com IP de origem **181.191.109.137** (NAT do datacenter entre as /30 internas).
- **Storage 10.93.0.122** (`storagecorexia`, user `corexia`): 50 TB, ociosa, tem internet.
  Grava os MP4 no disco LOCAL e exporta por NFS (read-only) para a Xeon reproduzir.

## O que foi feito

### Na storage (10.93.0.122)
1. **Volume**: `lvcreate` `ubuntu-vg/gravacoes` (~49 TB) → `mkfs.xfs` → montado em
   `/srv/corexia-gravacoes` (fstab, `noatime,nofail`), dono `corexia:corexia`.
2. **NFS server** (`nfs-kernel-server`) exportando `/srv/corexia-gravacoes` para
   `181.191.109.137` e `10.93.0.126` (`rw,all_squash,anonuid=1000`). Arquivo `/etc/exports`.
3. **Gravador**: `/opt/corexia-recorder/gravador_storage.py` (roda ffmpeg `-c copy -f segment`
   por câmera; segmentos de 5 min; nome `YYYY-MM-DD_HH-MM-SS.mp4` por `<camera_id>/`,
   idêntico ao que o `server.py` espera). Puxa a lista do backend da Xeon
   (`/listarCamerasGravacao` + WEBHOOK_SECRET). Reaping de zumbis a cada 5 s + backoff por
   câmera offline (até 10 min). Retenção local por idade (30 d) e espaço (45 TB).
   - Config: `/etc/corexia-recorder.env` (contém o segredo; chmod 600, dono corexia).
   - Serviço: `corexia-recorder.service` (systemd, `Restart=always`, User=corexia).
   - ffmpeg instalado via apt.

### Na Xeon (10.93.0.126)
4. **NFS client** (`nfs-common`): monta `10.93.0.122:/srv/corexia-gravacoes` em
   `/mnt/corexia-storage` **read-only** (`ro,soft,nofail,timeo=50,retrans=2,x-systemd.automount`;
   fstab). `soft` = se a storage cair, as chamadas do backend erram em ~15 s em vez de travar.
5. **Backend**: `server.py` linha 51 agora `GRAV_DIR = os.getenv("GRAV_DIR") or <default>`
   (mudança mínima, default preservado). `.env` recebeu `GRAV_DIR=/mnt/corexia-storage`.
   Assim o painel (`/api/gravacoes*`, `/gravacao/...`) serve as gravações da storage.
   O `gravador.py` da Xeon NÃO lê `GRAV_DIR` → segue intacto (relay HLS local inalterado).
   - Backups: `server.py.bak.pre-storage-<ts>`, `.env.bak.pre-storage-<ts>`.

## Verificação (2026-07-20)
- 131 diretórios de câmera criados; **106 câmeras** com gravação válida no painel (= as online).
- Segmento fechado de 304 s / 159 MB legível via NFS; download pelo painel HTTP 200, MP4 tocável.
- Storage: 0 zumbis, load ~0.9, CPU ~21%, ~22 MB/s de ingest (~1,9 TB/dia → ~24 dias em 48 TB).
- Produção da Xeon intacta: backend/vigia0/vigia_nvdec@0/@1 todos active; live view ok.

## ROLLBACK (se necessário)
1. **Desligar gravação** (storage): `sudo systemctl disable --now corexia-recorder.service`.
2. **Reverter playback pra local** (Xeon): remova a linha `GRAV_DIR=/mnt/corexia-storage` do
   `.env` (ou `cp server.py.bak.pre-storage-<ts> server.py`) e
   `sudo systemctl restart corexia-backend`.
3. **Desmontar NFS** (Xeon): `sudo umount /mnt/corexia-storage` + remova a linha do `/etc/fstab`.
4. **Desfazer NFS server** (storage, opcional): `sudo systemctl disable --now nfs-server`;
   os dados MP4 continuam em `/srv/corexia-gravacoes`.
Nada disso afeta detectores, alertas ou live view.

## Ajustes rápidos (sem código)
Edite `/etc/corexia-recorder.env` na storage e `systemctl restart corexia-recorder`:
- `GRAVACAO_RETENCAO_DIAS` (idade), `GRAVACAO_MAX_GB` (teto de espaço, hoje 45000 = 45 TB),
  `GRAVACAO_SEGMENTO_SEG` (tam. do segmento).

## Pendências / follow-up
- `/api/gravacoes*` faz `os.listdir` síncrono no NFS dentro de `async def` — com `soft` não
  trava permanente, mas idealmente mover pra threadpool.
- Câmeras embed-only do YouTube (teste) não são gravadas (sem rtsp_url). Fora do escopo.

---

## Adendo 2026-07-20 (parte 2) — Workspaces por cliente + painel de download

### Auditoria do front-end (pedido: senha não pode estar no front)
- A senha do admin `corexia123` NÃO está no bundle. Nenhum segredo vivo (webhook real
  `FJvMTzd…`, Roboflow, Gemini, Evolution apikey, VAPID privada) está no front. Login é
  server-side; o front só guarda o token de sessão (`localStorage.corexia_token`) após logar.
- ÚNICO achado: componente legado `web/assets/WebhookAnalitico-*.js` embute o secret ANTIGO
  padrão `corexia-webhook-2024` + URL morta do Base44. NÃO é o secret real e o backend REJEITA
  esse valor (server.py aborta se WEBHOOK_SECRET==default) → não explorável. Recomendação:
  remover a página "Webhook Analitico" do frontend-fonte e rebuildar. (Não dá pra rebuildar
  daqui: o fonte `C:\tmp\corexia-smart-vision` não está nesta máquina.)
- Risco maior REMANESCENTE (não alterado, pendente de decisão): painel exposto na internet
  (`http://181.191.109.137:8000`) + senha admin fraca. Front não vaza a senha, mas qualquer um
  pode tentar logar. Recomendo HTTPS + restringir origem/VPN quando for abrir pra clientes.

### Organização por cliente (workspaces)
- **No HD** (storage): o recorder agora monta `_workspaces/<Provedor>/<Cliente>/<Camera>` como
  árvore de SYMLINKS (não duplica dados) apontando para `../../../<camera_id>`. Regenerada a
  cada sync a partir do mapeamento do backend. Câmera sem dono cai em
  `_SEM_PROVEDOR/_NAO_ATRIBUIDAS`. Ao atribuir a câmera a um cliente/provedor, ela migra sozinha.
  (`/listarCamerasGravacao` passou a devolver cliente_id/nome + provedor_id/nome p/ isso.)
- **No painel**: nova página `http://<host>:8000/gravacoes-hd` (arquivo `gravacoes_hd.html`,
  servido pelo backend) — árvore Provedor → Cliente → Câmera → dias → arquivos, com download.
  Reaproveita o login do painel (token do localStorage). ESCOPADA por papel via
  `GET /api/gravacoes/workspaces` (usa `_cameras_visiveis`, o mesmo scope já auditado):
  admin vê tudo (inclui bucket "Não atribuídas"); provedor vê só as câmeras dele; cliente só as dele.

### Pré-requisito p/ o multi-tenant povoar
Hoje só 4 câmeras têm cliente/provedor (as demos, que nem gravam). As 130 reais estão
"Não atribuídas" (é onde estão os ~450 GB). Para provedores/clientes verem SUAS gravações,
é preciso ATRIBUIR cada câmera a um provedor/cliente (página Câmeras do painel, como admin).

### Arquivos alterados (Xeon)
- `server.py` (+ endpoint workspaces, + rota /gravacoes-hd, + tenant fields). Backup:
  `server.py.bak.workspaces-<ts>`. Novo: `gravacoes_hd.html`.
### Arquivos alterados (storage)
- `/opt/corexia-recorder/gravador_storage.py` (+ árvore `_workspaces`). Backup `.bak.<ts>`.

### Rollback destes itens
- Front/painel: `cp server.py.bak.workspaces-<ts> server.py && sudo systemctl restart corexia-backend`
  (remove endpoints e a rota). Apagar `gravacoes_hd.html` é opcional.
- HD: no recorder, a árvore `_workspaces` é só symlinks; some ao restaurar o `.bak` do recorder
  e `rm -rf /srv/corexia-gravacoes/_workspaces`.

---

## Adendo 2026-07-23 — Gravação por ALERTA + Chamados + achado do fonte do front

### Gravação por evento de alerta (recorder v3)
- `gravador_storage.py` v3 na storage com 2 modos (GRAVACAO_MODO em /etc/corexia-recorder.env):
  - `evento` (ATIVO): buffer HLS curto por câmera (auto-apaga em _buffer/) + ao cair um ALERTA,
    exporta um CLIPE com pré-roll (PRE_ROLL_SEG=30) + pós-roll (POST_ROLL_SEG=60) em
    <camera_id>/AAAA-MM-DD_HH-MM-SS_<tipo>_a<id>.mp4 (mesmo layout → painel serve igual).
  - `continuo`: 24/7 (comportamento anterior). Reverter = GRAVACAO_MODO=continuo + restart.
- Backend: novo POST /eventosRecentes (secret) que o recorder consulta p/ saber dos alertas.
- Testado com alerta sintético: clipe de 88.8s (30+60) gerado e válido; artefatos limpos.
- ATENÇÃO: detecção está gerando ~0 alertas/24h → em modo evento grava ~nada até a detecção
  ser investigada. As gravações contínuas antigas (~450GB) permanecem e são servidas.

### Chamados (tickets) — backend
- server.py: POST/GET/PUT /api/chamados (entity 'Chamado'). Cliente/provedor abrem; admin vê
  todos e responde/muda status. Escopo por _scope_ok (provedor vê os dele + dos clientes dele).
- Testado ponta-a-ponta (criar como cliente, admin lista, update status). UI ainda pendente.

### Fonte do front (React) — ACHADO IMPORTANTE
- O fonte que gerou o build DEPLOYADO (16/jul, 75 chunks lazy, API relativa /api) NÃO está na
  Xeon nem nas pastas locais. As 2 pastas locais são versões ANTERIORES:
  - Downloads/corexia-smart-vision = versão Base44 antiga (base44.auth, sem corexia_token).
  - Downloads/corexia2.0/client = versão do backend próprio mas de 30/jun: imports estáticos
    (build monolítico), apiClient default localhost:3001. Build-baseline diverge do deploy.
- => NÃO rebuildar o front dessas pastas (regrediria produção). Para popup de gravações e abas
  nativas (Chamados/Abrir Chamado) preciso do FONTE ATUAL que gerou o build de 16/jul.
