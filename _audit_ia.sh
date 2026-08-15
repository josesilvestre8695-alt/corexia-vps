#!/usr/bin/env bash
cd /home/tvlan/corexia-vision-ai || exit 1
echo "===== MODELOS no .env (chaves mascaradas) ====="
grep -iE 'MODEL|FACE|EPI|FIRE|KNIFE|PLATE|ROBOFLOW|DETECT|TIPOS_ATIVOS' .env | sed -E 's/(KEY|TOKEN|SECRET|PASS)=.*/\1=***/I'
echo
echo "===== model ids / classes no detector_saas.py ====="
grep -nE 'MODEL_ID|MODEL_TO_RUN|EXTRA_MODELS' detector_saas.py | sed -E 's/=.*getenv\("([A-Z_]+)".*/-> env \1/'
echo "--- CLASS_MAP (bloco) ---"
sed -n '/CLASS_MAP *= *{/,/}/p' detector_saas.py
echo
echo "===== arquivos .py do projeto (sem venv) ====="
ls *.py 2>/dev/null | tr '\n' ' '; echo
echo
echo "===== FACIAL / reconhecimento ====="
grep -rilE 'facial|reconhecimento|insightface|deepface|arcface|face_recog|embedding|desaparec' --include=*.py . | grep -viE '/venv/|site-packages'
echo "--- refs no server.py ---"; grep -niE 'facial|reconhec|desaparec|/face|album|leitura.*face' server.py | head
echo
echo "===== EPI / capacete / colete ====="
grep -rilE '\bepi\b|capacete|helmet|colete|reflet|luva|oculos' --include=*.py . | grep -viE '/venv/|site-packages'
echo
echo "===== LPR / placa / VMS ====="
grep -rilE '\blpr\b|placa|plate|\bvms\b' --include=*.py . | grep -viE '/venv/|site-packages' | tr '\n' ' '; echo
echo
echo "===== analiticos avancados (zona/linha/permanencia/abandono/caida/aglomer/intrus) ====="
grep -rilE 'zona|poligono|linha_virtual|virtual_line|permanencia|loitering|abandon|pessoa_caida|\bfall\b|aglomer|intrus|congestion' --include=*.py . | grep -viE '/venv/|site-packages' | tr '\n' ' '; echo
echo
echo "===== o modelo base (MODEL_TO_RUN) e quais classes ele traz? (amostra de CLASS_MAP keys) ====="
grep -oE '"[a-z_ ]+":' detector_saas.py | sort -u | head -60
