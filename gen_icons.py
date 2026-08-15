"""Gera os icones PWA do Corexia (PNG) com cv2/numpy — roda na Xeon (venv tem opencv)."""
import os
import cv2
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
os.makedirs(OUT, exist_ok=True)


def gradiente(size):
    """Fundo com gradiente laranja (topo claro -> base escura), BGR."""
    img = np.zeros((size, size, 3), np.uint8)
    c1 = (60, 146, 251)   # #fb923c
    c2 = (12, 88, 234)    # #ea580c
    for y in range(size):
        t = y / size
        img[y, :] = [int(a + (b - a) * t) for a, b in zip(c1, c2)]
    return img


def mascara_arredondada(size, r):
    m = np.zeros((size, size), np.uint8)
    cv2.rectangle(m, (r, 0), (size - r, size), 255, -1)
    cv2.rectangle(m, (0, r), (size, size - r), 255, -1)
    for cx, cy in [(r, r), (size - r, r), (r, size - r), (size - r, size - r)]:
        cv2.circle(m, (cx, cy), r, 255, -1)
    return m


def pts_escudo(size, escala):
    s = size * 0.30 * escala
    cx, cy = size / 2, size / 2 * 0.97
    pts = [(cx - s, cy - s * 0.85), (cx + s, cy - s * 0.85),
           (cx + s, cy + s * 0.25), (cx, cy + s * 1.05), (cx - s, cy + s * 0.25)]
    return np.array(pts, np.int32)


def desenha_escudo(img, size, escala=1.0):
    # escudo branco preenchido
    cv2.fillPoly(img, [pts_escudo(size, escala)], (255, 255, 255), lineType=cv2.LINE_AA)
    # escudo interno na cor do fundo (vira contorno grosso)
    cor_meio = tuple(int(v) for v in img[int(size * 0.52), int(size * 0.2)])
    cv2.fillPoly(img, [pts_escudo(size, escala * 0.74)], cor_meio, lineType=cv2.LINE_AA)
    # "check" branco dentro do escudo
    s = size * 0.30 * escala
    cx, cy = size / 2, size / 2 * 0.97
    chk = np.array([(cx - s * 0.42, cy - s * 0.02), (cx - s * 0.10, cy + s * 0.32),
                    (cx + s * 0.48, cy - s * 0.38)], np.int32)
    cv2.polylines(img, [chk], False, (255, 255, 255),
                  max(2, int(size * 0.055)), lineType=cv2.LINE_AA)
    return img


def salvar(nome, size, arredondado=True, escala=1.0):
    img = desenha_escudo(gradiente(size), size, escala)
    if arredondado:
        m = mascara_arredondada(size, int(size * 0.22))
        bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = m
        cv2.imwrite(os.path.join(OUT, nome), bgra)
    else:
        cv2.imwrite(os.path.join(OUT, nome), img)
    print("gerado:", nome)


img512 = desenha_escudo(gradiente(512), 512)
salvar("icon-512.png", 512, arredondado=True)
salvar("icon-512-maskable.png", 512, arredondado=False, escala=0.78)
# 192 = resize do 512 arredondado
big = cv2.imread(os.path.join(OUT, "icon-512.png"), cv2.IMREAD_UNCHANGED)
cv2.imwrite(os.path.join(OUT, "icon-192.png"), cv2.resize(big, (192, 192), interpolation=cv2.INTER_AREA))
print("gerado: icon-192.png")
