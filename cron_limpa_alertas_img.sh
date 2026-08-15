#!/usr/bin/env bash
# Retencao de ALERTAS (10 dias): apaga imagens em alertas_img + registros (entidade Alerta
# e tabela legada alertas) com mais de 10 dias. Roda 1x/dia pelo cron do tvlan.
DIR="/home/tvlan/corexia-vision-ai/alertas_img"
DB="/home/tvlan/corexia-vision-ai/corexia.db"
LOG="/home/tvlan/corexia-vision-ai/cron_limpa_alertas_img.log"
PY="/home/tvlan/corexia-vision-ai/venv/bin/python"
RET=10
IMG_N=0
if [ -d "$DIR" ]; then
  IMG_N=$(find "$DIR" -type f -mtime +$RET 2>/dev/null | wc -l)
  find "$DIR" -type f -mtime +$RET -delete 2>/dev/null
fi
REC=$("$PY" - "$DB" "$RET" <<'PY'
import sqlite3, sys
from datetime import datetime, timedelta
db, ret = sys.argv[1], int(sys.argv[2])
cut = datetime.now() - timedelta(days=ret)
iso = cut.strftime("%Y-%m-%dT%H:%M:%S")   # entities.created_date (com T)
sp  = cut.strftime("%Y-%m-%d %H:%M:%S")    # alertas.criado (com espaco)
c = sqlite3.connect(db)
n1 = c.execute("SELECT COUNT(*) FROM entities WHERE entity='Alerta' AND created_date < ?", (iso,)).fetchone()[0]
c.execute("DELETE FROM entities WHERE entity='Alerta' AND created_date < ?", (iso,))
n2 = 0
try:
    n2 = c.execute("SELECT COUNT(*) FROM alertas WHERE criado < ?", (sp,)).fetchone()[0]
    c.execute("DELETE FROM alertas WHERE criado < ?", (sp,))
except Exception:
    pass
c.commit(); c.close()
print("%d/%d" % (n1, n2))
PY
)
REST=$(find "$DIR" -type f 2>/dev/null | wc -l)
DISCO=$(df -h / | awk 'NR==2{print $5}')
echo "$(date '+%Y-%m-%d %H:%M') ret=${RET}d img_removidas=${IMG_N} reg_removidos(ent/tab)=${REC} img_restam=${REST} disco=${DISCO}" >> "$LOG"
tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
