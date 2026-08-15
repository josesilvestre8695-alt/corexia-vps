import time
from nvdec_source import NvdecSource

REF = "https://analitico.grupocorexia.com.br/"
URL = "https://live41.analitico.app.br/stream/get/cameratest2og0g.m3u8?token=cameratest2og0g"

print("abrindo fonte NVDEC...")
src = NvdecSource(URL, referer=REF, gpu=0, out_w=640, out_h=640, fps=4, name="teste2k").start()
ok = 0
for i in range(9):
    time.sleep(2)
    frame, ts = src.read()
    if frame is None:
        print(f"[{i}] sem frame ainda... {src.stats()}")
    else:
        print(f"[{i}] shape={frame.shape} dtype={frame.dtype} mean={frame.mean():.1f} std={frame.std():.1f} | {src.stats()}")
        if frame.shape == (640, 640, 3) and 5 < frame.mean() < 250 and frame.std() > 3:
            ok += 1
src.stop()
print(f"FIM — frames validos: {ok}/8  =>", "OK" if ok >= 5 else "FALHOU")
