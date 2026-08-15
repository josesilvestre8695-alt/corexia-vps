#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo ">> aguardando nvdec voltar..."; sleep 55
echo "   nvdec@0=$(systemctl is-active vigia_nvdec@0) nvdec@1=$(systemctl is-active vigia_nvdec@1)"
echo "   procs:"; pgrep -af detector_nvdec.py | head
echo ">> VRAM baseline:"; nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader | sed 's/^/   /'
echo ">> adiciona 'epi' na config da camera de teste (valida modelo EPI on-demand)"
./venv/bin/python - <<'PY'
import sqlite3, json
c=sqlite3.connect("corexia.db")
for rid,data in c.execute("SELECT id,data FROM entities WHERE entity='ConfigAnalitico'").fetchall():
    d=json.loads(data)
    for h in d.get("horarios",[]):
        if "epi" not in (h.get("analiticos") or []): h.setdefault("analiticos",[]).append("epi")
    if "epi" not in (d.get("analiticos_padrao") or []): d.setdefault("analiticos_padrao",[]).append("epi")
    c.execute("UPDATE entities SET data=? WHERE id=?", (json.dumps(d), rid))
    print("   config '%s' -> padrao agora: %s" % (d.get("camera_nome"), d.get("analiticos_padrao")))
c.commit(); c.close()
PY
echo ">> detector pega no proximo sync (~120s); modelos carregam no 1o frame apos."
