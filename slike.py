import base64
import hashlib
import io
import math
import os
import cv2
import numpy as np
from PIL import Image
import streamlit as st
import streamlit.components.v1 as components

# Konstante za standardni listič (76 x 22 mm)
STANDARDNA_DOLZINA_MM = 76.0
STANDARDNA_SIRINA_MM = 22.0
STANDARDNA_POVRSINA_CM2 = (STANDARDNA_DOLZINA_MM * STANDARDNA_SIRINA_MM) / 100.0
MIN_PREMER_STANDARD_UM = 50.0
PRIVZETA_OBCUTLJIVOST_STANDARD = 75.0

# Ohranjanje ločljivosti med 20 px/mm (1520 px) in 40 px/mm (3040 px),
# da se drobne kapljice velikosti 50 μm pri skenih ne izgubijo s pomanjševanjem.
MIN_STANDARDNA_DOLZINA_PX = 1520
MAX_STANDARDNA_DOLZINA_PX = 3040


@st.cache_resource
def _pridobi_rembg_sejo():
    """Leni uvoz in predpomnjenje modela u2net (ne blokira zagona aplikacije)."""
    try:
        from rembg import new_session
        return new_session("u2net"), None
    except Exception as napaka:
        return None, str(napaka)


def preveri_rembg_napako():
    """Vrne besedilo napake, če knjižnica rembg ni delovala, sicer None."""
    _, napaka = _pridobi_rembg_sejo()
    return napaka


@st.cache_data(show_spinner=False)
def _odstrani_ozadje_rembg(slika_png_bajti, kljuc_slike):
    """
    Izolacija ozadja z nevronsko mrežo rembg (u2net).
    """
    vhodna_slika = Image.open(io.BytesIO(slika_png_bajti))
    seja, napaka = _pridobi_rembg_sejo()
    if seja is not None and napaka is None:
        from rembg import remove
        slika_rgba = remove(vhodna_slika, session=seja)
        return np.array(slika_rgba)

    arr = np.array(vhodna_slika.convert("RGB"))
    polni_alpha = np.full((arr.shape[0], arr.shape[1], 1), 255, dtype=np.uint8)
    return np.concatenate([arr, polni_alpha], axis=2)


def pridobi_izoliran_rgba(slika_rgb):
    kljuc = hashlib.md5(slika_rgb.tobytes()).hexdigest()
    buf = io.BytesIO()
    Image.fromarray(slika_rgb).save(buf, format="PNG")
    return _odstrani_ozadje_rembg(buf.getvalue(), kljuc)


# =====================================================================
# INTERAKTIVNA KOMPONENTA ZA ROČNI IZREZ (PRILAGOJENA TELEFONU + LUPA NA ROBU)
# =====================================================================

KOMPONENTA_MAPA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_izrez_komponenta_v2")
os.makedirs(KOMPONENTA_MAPA, exist_ok=True)
INDEX_POT = os.path.join(KOMPONENTA_MAPA, "index.html")

HTML_KODA_IZREZA = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; width: 100%; max-width: 100%;
    overflow-x: hidden; font-family: sans-serif;
    background: transparent; user-select: none; -webkit-user-select: none;
  }
  .wrapper {
    display: flex; flex-direction: column; align-items: center;
    width: 100%; max-width: 100%; gap: 6px;
  }
  .canvas-box {
    width: 100%; max-width: 100%; display: flex; justify-content: center;
    border-radius: 8px; overflow: hidden; border: 1px solid #d0d7de; background: #141414;
  }
  canvas {
    display: block; max-width: 100%; cursor: crosshair; outline: none;
    touch-action: none; -webkit-touch-callout: none;
  }
  .hint {
    width: 100%; font-size: 12px; color: #7a828e;
    line-height: 1.35; text-align: left;
  }
</style>
</head>
<body>
<div class="wrapper" id="wrapper">
  <div class="canvas-box" id="canvasBox">
    <canvas id="mainCanvas" tabindex="0"></canvas>
  </div>
  <div class="hint">Pritisni in povleci oglišče ali katerikoli rob – med držanjem se prikaže lupa.</div>
</div>

<script>
const wrapperEl = document.getElementById("wrapper");
const mainCanvas = document.getElementById("mainCanvas");
const ctx = mainCanvas.getContext("2d");

let img = new Image();
let currentB64 = "";
let currentResetVersion = -1;
let corners = [[0.15, 0.15], [0.85, 0.15], [0.85, 0.85], [0.15, 0.85]];
let activeIdx = 0;
let dragMode = null;
let draggingIdx = -1;
let dragEdgeT = 0.5;
let dragStartPos = [0, 0];
let dragStartCorners = null;
let isTouchDrag = false;
let keyLoupeTimer = null;
let showKeyLoupe = false;

const PAD_BASE = 14;
let padX = PAD_BASE;
let padY = PAD_BASE;
let imgW = 280;
let imgH = 200;
let displayW = 308;
let displayH = 228;

function sendMessage(type, data) {
  window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data), "*");
}

function updateHeight() {
  const h = Math.ceil(wrapperEl.getBoundingClientRect().height) + 8;
  sendMessage("streamlit:setFrameHeight", { height: Math.max(120, h) });
}

function clamp01(v) { return Math.max(0, Math.min(1, v)); }

function normToCanvas(nx, ny) {
  return [padX + nx * imgW, padY + ny * imgH];
}

function getAvailableWidth() {
  const vals = [
    document.documentElement ? document.documentElement.clientWidth : 0,
    window.innerWidth || 0,
    wrapperEl ? Math.floor(wrapperEl.getBoundingClientRect().width) : 0
  ].filter(v => v > 120);
  const w = vals.length ? Math.min.apply(null, vals) : 320;
  return Math.max(220, Math.min(580, w - 2));
}

function resizeCanvas() {
  if (!img.naturalWidth) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const availW = getAvailableWidth();
  const maxImgW = Math.max(160, availW - 2 * PAD_BASE);
  const maxImgH = Math.round(Math.min(540, Math.max(300, availW * 1.45)));
  const scale = Math.min(maxImgW / img.naturalWidth, maxImgH / img.naturalHeight, 1);

  imgW = Math.max(100, Math.round(img.naturalWidth * scale));
  imgH