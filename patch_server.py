"""Insere o bloco da LP no server.py, antes do catch-all. Idempotente, com backup."""
import shutil
import sys
import time

PATH = "server.py"
MARKER = "# === LP Corexia + analytics (rotas /lp, /lp/admin, /api/lp/*) ==="
ANCHOR = '@app.get("/{full_path:path}")'
BLOCK = MARKER + '''
try:
    from lp_analytics import router as _lp_router
    app.include_router(_lp_router)
    _lp_assets_dir = os.path.join(HERE, "lp", "assets")
    if os.path.isdir(_lp_assets_dir):
        app.mount("/lp-assets", StaticFiles(directory=_lp_assets_dir), name="lp-assets")
    print("[lp_analytics] rotas /lp, /lp/admin e /api/lp/* ativas")
except Exception as _lp_err:
    print("[lp_analytics] falha ao carregar:", _lp_err)


'''

src = open(PATH, encoding="utf-8").read()
if MARKER in src:
    print("ja aplicado, nada a fazer")
    sys.exit(0)
if ANCHOR not in src:
    print("ERRO: catch-all nao encontrado em server.py")
    sys.exit(1)

bak = PATH + ".bak-lp-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(PATH, bak)
open(PATH, "w", encoding="utf-8").write(src.replace(ANCHOR, BLOCK + ANCHOR, 1))
print("aplicado; backup em", bak)
