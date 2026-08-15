#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
R=$(systemctl show vigia0 -p Restart --value 2>/dev/null)
echo "vigia0 Restart=$R"
if [ "$R" = "always" ] || [ "$R" = "on-failure" ]; then
  echo ">> reiniciando vigia0 (kill detector_saas.py -> respawn com .env novo)"
  pkill -f detector_saas.py; sleep 12
  echo ">> processos detector_saas apos respawn:"; pgrep -af detector_saas.py | head
  echo ">> is-active vigia0: $(systemctl is-active vigia0)"
  echo ">> confirma tipos no log (se acessivel):"
  journalctl -u vigia0 -n 40 --no-pager 2>/dev/null | grep -iE 'SaaS detector|tipos=' | tail -3
else
  echo ">> ATENCAO: vigia0 sem Restart automatico ($R). NAO vou matar (precisaria systemctl start c/ sudo)."
fi
