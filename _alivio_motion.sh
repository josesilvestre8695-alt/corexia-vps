#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo "== antes =="; grep '^TIPOS_ATIVOS=' .env
cp -a .env .env.bak-motionoff
# remove SOMENTE 'movimento' da lista global (mantem arma_fogo,arma_branca,fogo,placa)
sed -i 's/^TIPOS_ATIVOS=.*/TIPOS_ATIVOS=arma_fogo,arma_branca,fogo,placa/' .env
echo "== depois =="; grep '^TIPOS_ATIVOS=' .env
echo "== reinicia o detector (kill -> Restart=always respawna) =="
pkill -f detector_nvdec.py; sleep 12
echo "== processos detector apos respawn =="; pgrep -af detector_nvdec.py | head
echo "== confirma 'tipos' no log de inicializacao (se journal acessivel) =="
journalctl -u 'vigia_nvdec@*' -n 60 --no-pager 2>/dev/null | grep -iE 'SaaS detector|tipos=' | tail -4
