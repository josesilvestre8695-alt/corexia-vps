# Mapa Viggia (Base44) → Corexia

Fonte: export do repo `grupo-viggia` (Base44) em /home/tvlan/base44-viggia. Baixado local em scratchpad/viggia.
23 entidades, ~68 funções backend (TS), 39 telas React. NENHUM secret nos schemas de entidade; secrets ficam em env das functions (Asaas, NFE.io, Z-API, Resend, credenciais do painel de câmeras).

## DESCOBERTA-CHAVE DE ARQUITETURA
- Viggia é **single-tenant** (uma ISP). NENHUMA entidade tem `provedor_id`. No Corexia (multi-tenant), TODA entidade de negócio precisa ganhar `provedor_id`.
- As funções `blockUserInCorexia`/`unblockUserInCorexia` da Viggia fazem LOGIN + scraping em **`analitico.grupocorexia.com.br`** (painel Laravel) para bloquear/desbloquear o usuário final por inadimplência. Ou seja: o "Corexia" no código da Viggia = o PAINEL DE CÂMERAS real do usuário. Viggia é a camada de GESTÃO (Base44) por cima do painel de câmeras da Corexia. No novo sistema unificado da Corexia, bloquear/desbloquear vira chamada INTERNA (sem scraping).
- Asaas na Viggia é só nível-2 (provedor→cliente). O nível-1 (Corexia→provedor) é exclusivo do Corexia.
- Comissão NÃO usa split do Asaas — é ledger interno + pagamento manual (status pending→paid).

## 3 BALDES
### A) JÁ TEMOS / ADAPTAR
- Cliente, Plano, Fatura, Proposta, ContractTemplate, Vendedor, Comissionamento, Contas a Pagar, Camera, Alerta, PreferenciaAlerta → mapeiam 1:1; adicionar `provedor_id`.
- Asaas: criar customer+subscription (unificar as 4 variantes da Viggia numa só), sync payments→faturas, webhook (RECEBIDO/CONFIRMADO→paid, OVERDUE→overdue), overdue→bloqueio (regra 10 dias), comissão na confirmação (percentage: amount*val/100; fixed: val; vencimento dia 20 do mês seguinte).

### B) NOVO E VALE MUITO
1. **NFS-e (NFE.io)** — emitir/verificarStatus/verificarPendentes/listCompanies sobre `serviceinvoices`. Parametrizar cityServiceCode (Viggia hardcoded PE '140201.501'), issRate, IBGE por município/tenant. Gate: fatura paga. Guardar nfse_id/number/status/pdf_url/sent_*.
2. **Chamados/tickets** — entidade Chamado (tipo suporte/financeiro/atendimento; status aberto/em_andamento/resolvido/fechado; resposta). Tela cliente "Abrir Chamado" + notifica plantão via WhatsApp + tela de gestão. (Viggia não usa em_andamento nem responde cliente no resolve — melhorar.)
3. **Plantão (NumeroPlantao)** — lista de números on-call (nome, telefone, ativo, acesso_tela, user_id). ativos recebem TODOS os alertas e chamados. Sem escala/turno na Viggia.
4. **Fan-out de alerta multi-destinatário** — por alerta notifica (a) cliente se pref permite, (b) sub-users com receber_alertas_whatsapp (pref própria ou fallback do cliente), (c) todos plantão ativos (sem filtro). Fire-and-forget por destinatário.
5. **SubUser** — logins secundários sob um Cliente (condomínio/multi-unidade) com ACL fina: allowed_cameras (live), allowed_recording_cameras, allowed_mosaics.
6. **Mosaic** — grade de câmeras (máx 4).
7. **WhatsApp inbound + inbox** — webhookZapiMensagens (ReceivedCallback, drop waitingMessage, dedup zapi_message_id, extração por tipo de mídia) + WhatsAppMessage + tela WhatsAppMonitor (conversas por telefone, responder).
8. **Wallet/conciliação (WalletConfig+WalletTransaction)** — ledger de caixa (invoice_received/expense/initial_balance; pending_conciliation→conciliated). Trigger: fatura paga cria linha pendente.
9. **Visitas de vendedor (Visit+VisitLocation)** — check-in/out GPS + reembolso por KM.
10. **Payment promise** — payment_promise_date/active/set_by/set_at no cliente; segura bloqueio se promessa ativa e futura; limpa ao pagar.
11. **Monitor de saúde Z-API** (cron GET /status; alerta ops se connected!=true).
12. **Dunning** — lembretes T-1/T0/overdue.

### C) RISCOS / HARDENING (fazer melhor que a Viggia)
- **Conta PRIME compartilhada**: TODO webhook/sync precisa filtrar por externalReference/customer→provedor_id, senão ingere pagamentos de outros produtos. Usar externalReference = id local + tag de provedor.
- **Webhook Asaas sem validação na Viggia** → validar header `asaas-access-token`.
- **Webhook de alerta com secret "soft"** (passa se omitir) → tornar secret obrigatório e por-tenant.
- **Assimetria block/unblock**: bloqueio é chamado direto; desbloqueio só mexe no DB (depende de automação). Corexia: fiar desbloqueio no pagamento.
- **Assinatura**: código 6 dígitos em texto puro, sem TTL, sem rate-limit, nunca expira; captura só IP (geo é marketing, não implementado); contract_signed_url definido mas nunca gravado. Melhorar: TTL, throttle, gravar PDF assinado, capturar IP+user-agent.
- **confianca inconsistente**: UI trata 0–1, texto WhatsApp trata 0–100. Escolher UMA convenção.

## DETALHES DE INTEGRAÇÃO (para a fase de build)
- **Asaas** base https://api.asaas.com/v3 header access_token. subscription: billingType BOLETO (hardcoded — tornar PIX/config), cycle MONTHLY, maxPayments=contract months (36 cnpj/12 cpf), nextDueDate +5d, fine 2% / interest 1%, externalReference=id local. status map RECEIVED/CONFIRMED→paid, OVERDUE→overdue, PENDING→pending, else cancelled. dedup fatura por invoiceNumber||id.
- **NFE.io** base https://api.nfe.io/v1 header Authorization: <key> (SEM Bearer). POST /companies/{id}/serviceinvoices, poll GET .../{nfeId} ≤8×2s, GET .../{nfeId}/pdf. borrower LegalEntity/NaturalPerson por document_type.
- **Z-API** base https://api.z-api.io/instances/{INSTANCE}/token/{TOKEN} header Client-Token. POST /send-text {phone,message}, /send-image {phone,image,caption}, GET /status. phone: só dígitos, prefixa 55.
- **Assinatura**: sendSignatureCode gera código 6díg + signature_token(UUID), envia por email(Resend)+WhatsApp. validateSignature exige token+código, bloqueia re-assinatura, captura IP, cria Client, envia email de comprovante com PDF.

## ENTIDADES VIGGIA (resumo)
Client, Plan, Invoice, Proposal, ContractTemplate, SalesRepresentative, Commission, CommissionPayment, AccountPayable, Camera, Alerta, PreferenciaAlerta, User, SubUser, NumeroPlantao, Chamado, WhatsAppMessage, Mosaic, Visit, VisitLocation, WalletConfig, WalletTransaction.
(Client tem status active/blocked/suspended/cancelled + block_reason + payment_promise_*. Camera tem analitico_id = FK externo p/ painel de câmeras.)

## TELAS (39) — inventário por perfil e ordem de build

ROLES Viggia: role=admin (admin puro), user_type ∈ {admin,manager,salesrep,demonstrador} (operador do painel), client, subuser, user (fallback). Gating de tela é client-side em Layout.getFilteredMenu() + accessiblePages Set + páginas re-checam auth (defense-in-depth). Campos: role, user_type, permissions{} (dashboard,proposals,plans,contracts,cameras,mosaics,viewCameras,viewMosaics,clients,invoices,accountsPayable,salesReps,tracking,visits,gravacoes,alertas,preferenciasAlerta,webhookAnalitico,myCameras,...,cameraMap,viggiaMap), vínculos client_id/subuser_id + allowed_cameras/allowed_mosaics. No Corexia tudo já é escopado por provedor_id.

### PORTAL DO PROVEDOR (/provedor)
JÁ TEM (MVP): Dashboard (blueprint Dashboard.jsx), Meus Clientes (Clients.jsx + ClientSubUsers.jsx), Cobrança (Invoices.jsx + ContasAReceber.jsx).

FALTA (com blueprint Viggia):
1. **Câmeras & IA** — Cameras.jsx (CRUD), AdminCameraViewer.jsx (player/ZoomableStream), Mosaics.jsx + AdminMosaicViewer.jsx (MosaicCameraPlayer), CameraMap.jsx (Leaflet), Gravacoes.jsx (deep-link analitico.grupocorexia.com.br), WebhookAnalitico.jsx (gera webhook IA/Protenet por câmera). NÚCLEO.
2. **Alertas** — Alertas.jsx (feed IA + alarme sonoro/TTS), PreferenciasAlerta.jsx (regras por cliente/subuser), AlertasPlantao.jsx + NumerosPlantao.jsx (plantão). Alimentado pelo WebhookAnalitico.
3. **Minha Marca (white-label)** — SEM página na Viggia; blueprint = bloco de branding do Layout.jsx (logo + .viggia-gradient/.viggia-accent) + branding de docs em ContractTemplates.jsx. Tela NOVA: form por provedor_id (logo, cores, domínio) → injeta CSS vars + PDFs. (Engine white-label do Corexia já existe no SPA; falta o self-service.)
4. **Vendas/Propostas** — Proposals.jsx, ProposalView.jsx, ProposalSignature.jsx (OTP), ContractTemplates.jsx; campo: ViggiaMap.jsx + Visits.jsx + LiveTracking.jsx. Muito reaproveitável de /comercial.
5. **Comissão** — Commissions.jsx (config+pagamentos), SalesRepresentatives.jsx. Depende de Vendas.

ORDEM SUGERIDA: 1) Câmeras & IA  2) Alertas  3) Minha Marca  4) Vendas/Propostas  5) Comissão.

### ÁREA DO CLIENTE (novo no Corexia): MyCameras, MyMosaic, MyGravacoes, MyInvoices, AbrirChamado, MySubUsers (subuser: só MyCameras/MyMosaic/MyGravacoes; MyInvoices negado).
### /comercial (admin) — extras Viggia candidatos: WhatsAppMonitor, AtendimentosCliente, WalletConciliation, ClientRanking, UserManagement. Públicas (fora dos consoles): LandingPage, Home.
### PÁGINAS POR LINK: ProposalView, ProposalSignature, ClientSubUsers (operador gerencia subusers de um cliente).
