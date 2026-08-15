"""Mede o delay do stream: captura frames como o detector (cv2+referer) e grava
o horario do SERVIDOR no nome — compara com o relogio queimado no frame (OSD)."""
import os, time, datetime
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "headers;Referer: https://analitico.grupocorexia.com.br/\r\n"
import cv2

URL = "https://live41.analitico.app.br/stream/get/cameratest2og0g.m3u8?token=cameratest2og0g"

def snap(tag):
    t0 = datetime.datetime.now()
    cap = cv2.VideoCapture(URL)
    ok, frame = cap.read()
    t1 = datetime.datetime.now()
    cap.release()
    if not ok:
        print(f"{tag}: SEM FRAME"); return
    p = f"/tmp/lat_{tag}.jpg"
    cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    print(f"{tag}: frame lido | servidor={t1.strftime('%H:%M:%S')} (abertura levou {(t1-t0).total_seconds():.1f}s) -> {p}")

snap("a")
time.sleep(15)
snap("b")
