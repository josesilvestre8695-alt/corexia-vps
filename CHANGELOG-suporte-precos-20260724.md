# 2026-07-24 — Suporte + Gravações por tenant + Segurança + Precificação
Backup: server.py.bak-20260724-suporte | web_old_20260724/
## Backend (server.py)
- /img/{name}: agora exige auth + escopo por tenant (era público); cache private
- Media token mt1 (HMAC, 24h, só-mídia): GET /api/auth/media-token; ?t= aceita sessão (compat)
- CRUD genérico bloqueia entity Chamado p/ não-admin (fluxo oficial: /api/chamados)
- PUT /api/chamados: provedor responde chamados dos clientes dele; autor só reabre/fecha; admin pleno
- Web push ao abrir chamado (admins+provedor) e ao responder (autor)
- Pricing: provedor pode gravar ia_contrato/grav_contrato na Camera; gates conservadores em
  /listarCamerasIA e /listarCamerasGravacao (só bloqueia contrato inativo/vencido; sem contrato = segue)
## Frontend (web/)
- /suporte (admin: abas provedores×clientes finais; provedor: responde clientes + abre p/ Corexia)
- /portal/suporte (cliente final abre/acompanha chamado) + menu/tab/card
- /gravacoes: visão "Por cliente" (workspaces provedor→cliente→câmeras)
- /contratacoes: contratar IA (R$1,90/cam/dia) e gravação (R$0,90/cam/dia) por câmera × dias (fictício)
- lp.html: plano base R$797/mês (100 câmeras) + add-ons por câmera/dia
- Toda mídia (?t=) migrada pro media token curto
## Validação
9 smoke tests OK; gravador 134 câmeras / IA 130 inalterados pós-deploy

## 2026-07-24 (rodada 2) — Gate ESTRITO de gravação + Monitor de infraestrutura
Backup: server.py.bak-20260724-gate
- GATE_GRAV_ESTRITO=1 (default): gravador SO recebe câmera com grav_contrato ativo e dentro
  da janela inicio->fim (conta da data da contratação). Sem contrato = NAO grava.
  GATE_IA_ESTRITO=0 (IA segue conservadora até a virada comercial). Chaves no .env.
- Validado ao vivo: 0 câmeras sem contrato; contrato vencido = fora; contrato 3 dias na
  camera02rafae01 (24->27/07, demo) = só ela; gravador derrubou 93 buffers e ficou com 1 ffmpeg.
- GET /api/infra (admin): CPU/RAM/disco/GPUs (uso+decoder+VRAM+temp) do Xeon + métricas da
  storage lidas de /srv/corexia-gravacoes/_metrics.json via NFS.
- Storage .122: cron 1/min ~/metrics_writer.sh escreve _metrics.json (cpu, ram, ffmpeg, buffers).
- Dashboard admin: seção Infraestrutura (InfraMonitor.jsx, gauges recharts, refresh 10s).

## 2026-07-24 (rodada 3) — Diagnóstico completo + 14 bugs corrigidos
Backups: server.py.bak-20260724-diagfix | web_old anteriores
Diagnóstico: runtime das 2 máquinas OK; 24/24 testes RBAC/tenancy PASS; E2E alerta→clipe OK;
review adversarial (5 dimensões + verify) = 14 achados confirmados, TODOS corrigidos:
- [ALTO] NFS pendurado travava o executor global → pool dedicado (1 worker, single-flight) +
  timeout 3s + fallback de cache; Xeon (local) separado do storage (NFS). /api/infra e
  /listarCamerasIA/gravacoes não travam mais se a .122 cair.
- [ALTO] media token não limpo no logout (reuso entre contas no mesmo navegador) →
  clearMediaToken() no logout; chave localStorage versionada (v2).
- [MÉDIO] media token reusava WEBHOOK_SECRET → secret DEDICADO e persistente (.media_secret);
  TTL 24h→6h; sem fallback pro token de sessão na URL (não vaza credencial de 30d em log).
- [MÉDIO] contratos: servidor agora CARIMBA valor_dia (tabela), inicio/fim (relógio do
  servidor) e valida dias (7-90); renovar ESTENDE do fim atual (não perde dias pagos).
- [MÉDIO] GATE_*_ESTRITO parsing tolerante (_env_bool: 1/true/yes/on).
- [BAIXO] _contrato_bloqueia: vencido bloqueia independente do status; infra cpu/mem/gpu
  com try/except (uma GPU ruim não derruba as outras); cache com asyncio.Lock; badge de
  Contratações não quebra sem fim; disco do Xeon em GB (não "0,09 TB").
Visual: badge de saúde geral (operacional/atenção/crítico), pulse "ao vivo", ícones com
gradiente da marca, disco em GB. Validado no navegador (imagens 200, dashboard renderizando).
Residual aceito: mt1 não é revogado individualmente antes do TTL (mitigado: 6h + clear no logout).
