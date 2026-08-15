#!/usr/bin/env bash
# Backup Corexia: dados (banco+segredos+configs) local+rotacao, codigo->git(push se remote),
# dados criptografados por e-mail (se configurado em ~/.corexia_backup.env).
set -u
cd /home/tvlan/corexia-vision-ai || exit 1
STAMP=$(date +%Y%m%d-%H%M)
BK=/home/tvlan/backups
mkdir -p "$BK"
exec >>"$BK/backup.log" 2>&1
echo "==== backup $STAMP ===="

# 1) DADOS: pacote pequeno (copia consistente do banco + segredos + configs)
TMP=$(mktemp -d)
./venv/bin/python -c "import sqlite3;s=sqlite3.connect('corexia.db');d=sqlite3.connect('$TMP/corexia.db');s.backup(d);d.close();s.close()" && echo "db copiado" || echo "ERRO copia db"
cp -a .env "$TMP/env.bak" 2>/dev/null || true
cp -a vapid_private.pem "$TMP/" 2>/dev/null || true
cp -a lp_analytics.db "$TMP/" 2>/dev/null || true
cp -a /etc/caddy/Caddyfile "$TMP/Caddyfile" 2>/dev/null || true
mkdir -p "$TMP/systemd"
cp -a /etc/systemd/system/vigia*.service /etc/systemd/system/corexia*.service /etc/systemd/system/caddy.service /etc/systemd/system/mediamtx.service "$TMP/systemd/" 2>/dev/null || true
# infra extra p/ sobreviver troca de disco (crontab, mediamtx, mounts, firewall)
crontab -l > "$TMP/crontab.txt" 2>/dev/null || true
cp -a /home/tvlan/mediamtx/mediamtx.yml "$TMP/mediamtx.yml" 2>/dev/null || true
cp -a /etc/fstab "$TMP/fstab" 2>/dev/null || true
findmnt -o SOURCE,TARGET,FSTYPE,OPTIONS > "$TMP/mounts.txt" 2>/dev/null || true
sudo -n iptables-save > "$TMP/iptables.rules" 2>/dev/null || cp -a /home/tvlan/infra/iptables.rules "$TMP/iptables.rules" 2>/dev/null || true
BUND="$BK/corexia-dados-$STAMP.tar.gz"
tar czf "$BUND" -C "$TMP" . && echo "bundle: $BUND ($(du -h "$BUND"|cut -f1))"
rm -rf "$TMP"
# rotacao local: mantem os 7 mais recentes
ls -1t "$BK"/corexia-dados-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

# 2) CODIGO -> commit local (e push se houver remote 'origin')
git add -A
git commit -q -m "backup auto $STAMP" 2>/dev/null && echo "commit feito" || echo "nada novo p/ commit"
if git remote | grep -qx origin; then
  if git push -q origin HEAD 2>&1; then echo "push OK (GitHub)"; else echo "push FALHOU"; fi
else
  echo "sem remote origin (GitHub ainda nao configurado)"
fi

# 3) DADOS por e-mail (criptografado) -> se ~/.corexia_backup.env existir
CFG=/home/tvlan/.corexia_backup.env
if [ -f "$CFG" ]; then
  . "$CFG"
  ENC="$BUND.gpg"
  if echo "$BACKUP_PASS" | gpg --batch --yes --passphrase-fd 0 -c -o "$ENC" "$BUND"; then
    if ./venv/bin/python /home/tvlan/corexia-vision-ai/backup_mail.py "$ENC" "$MAIL_USER" "$MAIL_PASS" "$MAIL_TO" "Backup Corexia $STAMP"; then echo "email enviado"; else echo "email FALHOU"; fi
    rm -f "$ENC"
  else echo "ERRO ao cifrar"; fi
else
  echo "sem config de e-mail (~/.corexia_backup.env ausente)"
fi
echo "==== fim $STAMP ===="
