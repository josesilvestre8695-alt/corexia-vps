"""
Corexia — fonte de video NVDEC (decode na GPU) para o detector_nvdec.

Decodifica um stream HLS/RTMP via ffmpeg do SISTEMA com NVDEC, faz downscale na GPU
(scale_cuda) e entrega frames BGR (numpy HxWx3) prontos pro modelo roboflow.

Estrategia drop-oldest: uma thread le o pipe continuamente e guarda SO o frame mais novo.
O consumidor (inferencia) pega o ultimo frame quando quiser — nunca bloqueia o ffmpeg no
pipe (o que travaria o decode e dispararia respawn). Auto-heal: se o stream cai, o ffmpeg
e reaberto com backoff. NVDEC pode falhar em codecs raros -> fallback CPU automatico.

Comando NVDEC provado na maquina (2K 105 frames OK):
  ffmpeg -referer <ref> -hwaccel cuda -hwaccel_device N -hwaccel_output_format cuda -i <url>
         -vf "fps=F,scale_cuda=W:H,hwdownload,format=nv12,format=bgr24" -f rawvideo -
"""
import os, subprocess, threading, time
import numpy as np

FFMPEG = os.getenv("FFMPEG_BIN", "/usr/bin/ffmpeg")


class NvdecSource:
    def __init__(self, url, referer="", gpu=0, out_w=640, out_h=640, fps=4,
                 nvdec=True, name="", skip_nonref=False, surfaces=None):
        self.url = url
        self.referer = referer
        self.gpu = str(gpu)
        self.out_w, self.out_h = int(out_w), int(out_h)
        self.fps = fps
        self.nvdec = nvdec
        self.name = name or url[-24:]
        self.skip_nonref = skip_nonref
        # nº de surfaces do NVDEC: baixo = MUITO menos VRAM por câmera (chave pra escala)
        self.surfaces = surfaces if surfaces is not None else int(os.getenv("NVDEC_SURFACES", "6"))
        self._frame_bytes = self.out_w * self.out_h * 3

        self.proc = None
        self._thread = None
        self._latest = None
        self._lock = threading.RLock()   # reentrante: stats() chama age() que tb pega o lock
        self._stop = False
        self._frame_ts = 0.0        # ts do ultimo frame recebido (liveness/watchdog)
        self._frames = 0            # total de frames recebidos
        self._starts = 0            # quantas vezes o ffmpeg (re)abriu
        self._nvdec_failed = False  # caiu pro fallback CPU?

    # ---- comando ffmpeg ----
    def _cmd(self, use_nvdec):
        c = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
        _http = self.url.startswith("http")
        if self.referer and _http:
            c += ["-referer", self.referer]   # -referer/-reconnect sao opcoes HTTP: invalidas em RTSP
        c += ["-fflags", "nobuffer", "-flags", "low_delay"]
        if _http:
            c += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if self.url.startswith("rtsp"):
            c += ["-rtsp_transport", "tcp"]   # TCP = confiavel atraves de NAT/internet publica
        if self.skip_nonref:
            c += ["-skip_frame", "nonref"]   # dropa B-frames -> alivia parcial o decode
        if use_nvdec:
            # -c:v h264_cuvid + -surfaces baixo = corta MUITO a VRAM por câmera (escala p/ 130).
            # streams do analitico sao todos H.264; se vier HEVC, o cuvid falha e cai no fallback CPU.
            c += ["-hwaccel", "cuda", "-hwaccel_device", self.gpu, "-hwaccel_output_format", "cuda",
                  "-c:v", "h264_cuvid", "-surfaces", str(self.surfaces), "-i", self.url,
                  "-vf", f"fps={self.fps},scale_cuda={self.out_w}:{self.out_h},hwdownload,format=nv12,format=bgr24"]
        else:  # fallback: decode/scale na CPU
            c += ["-i", self.url,
                  "-vf", f"fps={self.fps},scale={self.out_w}:{self.out_h},format=bgr24"]
        c += ["-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        return c

    # ---- ciclo de vida ----
    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=f"nvdec-{self.name}", daemon=True)
        self._thread.start()
        return self

    def _spawn(self):
        use_nvdec = self.nvdec and not self._nvdec_failed
        try:
            # ffmpeg escolhe a GPU FISICA por -hwaccel_device; entao removemos CUDA_VISIBLE_DEVICES
            # do ambiente dele (o worker seta =WORKER_GPU pros modelos, mas isso remapearia o
            # indice fisico do ffmpeg e a placa 1 do worker 1 sumiria).
            _env = dict(os.environ)
            _env.pop("CUDA_VISIBLE_DEVICES", None)
            self.proc = subprocess.Popen(self._cmd(use_nvdec), stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=self._frame_bytes, env=_env)
            self._starts += 1
            return True
        except Exception as e:
            print(f"[nvdec:{self.name}] falha ao abrir ffmpeg: {e}", flush=True)
            return False

    def _kill_proc(self):
        """mata o ffmpeg de forma CONFIAVEL (SIGKILL) e faz reap (evita vazamento/zumbi)."""
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            pass

    def _read_exact(self, n):
        """le exatamente n bytes do pipe (pipes podem devolver leituras parciais)."""
        buf = bytearray()
        rd = self.proc.stdout
        while len(buf) < n:
            chunk = rd.read(n - len(buf))
            if not chunk:
                return None       # EOF -> stream caiu
            buf += chunk
        return buf

    def _run(self):
        backoff = 1.0
        while not self._stop:
            if not self._spawn():
                time.sleep(backoff); backoff = min(backoff * 2, 30); continue
            got_any = False
            while not self._stop:
                buf = self._read_exact(self._frame_bytes)
                if buf is None:
                    break   # stream caiu -> respawn
                frame = np.frombuffer(bytes(buf), np.uint8).reshape(self.out_h, self.out_w, 3)
                with self._lock:
                    self._latest = frame
                    self._frame_ts = time.time()
                    self._frames += 1
                got_any = True
                backoff = 1.0
            # saiu do loop de leitura: mata o ffmpeg de forma confiavel (evita vazamento)
            self._kill_proc()
            if self._stop:
                break
            # se NUNCA recebeu frame com NVDEC, o codec pode nao ser suportado -> tenta CPU
            if self.nvdec and not self._nvdec_failed and not got_any and self._frames == 0:
                self._nvdec_failed = True
                print(f"[nvdec:{self.name}] NVDEC nao produziu frame -> fallback CPU", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    # ---- consumo ----
    def read(self):
        """(frame_bgr, ts) do frame mais novo, ou (None, 0.0)."""
        with self._lock:
            return (self._latest, self._frame_ts) if self._latest is not None else (None, 0.0)

    def age(self):
        """segundos desde o ultimo frame (grande = stream travado/caido)."""
        with self._lock:
            return 1e9 if self._frame_ts == 0 else (time.time() - self._frame_ts)

    def stats(self):
        with self._lock:
            return {"name": self.name, "frames": self._frames, "starts": self._starts,
                    "nvdec": self.nvdec and not self._nvdec_failed, "age": round(self.age(), 1)}

    def stop(self):
        self._stop = True
        self._kill_proc()
