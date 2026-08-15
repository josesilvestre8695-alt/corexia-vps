#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
chmod +x backup_corexia.sh
# gitignore: nao versionar scripts scratch nem o log
grep -qxF '_*.sh' .gitignore 2>/dev/null || printf '_*.sh\nbackup.log\n' >> .gitignore
echo ">> RODANDO backup uma vez (so local, sem GitHub/e-mail ainda)"
./backup_corexia.sh
echo ">> conteudo de /home/tvlan/backups:"
ls -lh /home/tvlan/backups/ | tail -5
echo ">> ultimas linhas do log:"
tail -14 /home/tvlan/backups/backup.log
echo ">> instala cron diario (03:30)"
( crontab -l 2>/dev/null | grep -v 'backup_corexia.sh'; echo "30 3 * * * /home/tvlan/corexia-vision-ai/backup_corexia.sh" ) | crontab -
echo ">> cron atual:"; crontab -l | grep backup_corexia
