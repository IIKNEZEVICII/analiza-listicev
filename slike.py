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

# Novo ime mape in komponente prepreči uporabo stare različice iz predpomnilnika telefona
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
  imgH = Math.max(60, Math.round(img.naturalHeight * scale));

  displayW = availW;
  displayH = Math.max(imgH + 2 * PAD_BASE, 210);
  padX = Math.floor((displayW - imgW) / 2);
  padY = Math.floor((displayH - imgH) / 2);

  mainCanvas.style.width = displayW + "px";
  mainCanvas.style.height = displayH + "px";
  mainCanvas.width = Math.round(displayW * dpr);
  mainCanvas.height = Math.round(displayH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  draw();
  updateHeight();
}

function distToSegment(px, py, x1, y1, x2, y2) {
  const vx = x2 - x1;
  const vy = y2 - y1;
  const len2 = vx * vx + vy * vy;
  let t = len2 > 0 ? ((px - x1) * vx + (py - y1) * vy) / len2 : 0.5;
  t = Math.max(0, Math.min(1, t));
  const cx = x1 + t * vx;
  const cy = y1 + t * vy;
  return { dist: Math.hypot(px - cx, py - cy), t: t };
}

function drawLoupe(fx, fy) {
  const [px, py] = normToCanvas(fx, fy);
  const R = Math.round(Math.min(62, Math.max(44, displayW * 0.175)));
  const offset = R + (isTouchDrag ? 36 : 16);

  let lx = px;
  let ly = py - offset;
  if (ly - R < 4) {
    ly = py + offset;
  }
  ly = Math.max(R + 4, Math.min(displayH - R - 4, ly));
  lx = Math.max(R + 4, Math.min(displayW - R - 4, lx));

  if (Math.hypot(lx - px, ly - py) < R + 10) {
    const shiftX = R + (isTouchDrag ? 34 : 16);
    if (px + shiftX + R <= displayW - 4) lx = px + shiftX;
    else if (px - shiftX - R >= 4) lx = px - shiftX;
  }

  // Vodilna črta od pritisnjene točke na robu do lupe
  ctx.save();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(px, py);
  ctx.lineTo(lx, ly);
  ctx.stroke();
  ctx.restore();

  // Senca pod lupo
  ctx.save();
  ctx.shadowColor = "rgba(0, 0, 0, 0.60)";
  ctx.shadowBlur = 12;
  ctx.shadowOffsetY = 3;
  ctx.beginPath();
  ctx.arc(lx, ly, R, 0, Math.PI * 2);
  ctx.fillStyle = "#111";
  ctx.fill();
  ctx.restore();

  // Povečan izsek okoli pritisnjenega oglišča / roba (4.5x)
  ctx.save();
  ctx.beginPath();
  ctx.arc(lx, ly, R, 0, Math.PI * 2);
  ctx.clip();

  ctx.fillStyle = "#111";
  ctx.fillRect(lx - R, ly - R, 2 * R, 2 * R);

  const zoomFactor = 4.5;
  const srcW = (2 * R / zoomFactor) * (img.naturalWidth / imgW);
  const srcH = (2 * R / zoomFactor) * (img.naturalHeight / imgH);
  const srcX = fx * img.naturalWidth - srcW / 2;
  const srcY = fy * img.naturalHeight - srcH / 2;

  ctx.drawImage(img, srcX, srcY, srcW, srcH, lx - R, ly - R, 2 * R, 2 * R);

  // Zelena robna črta v povečavi
  ctx.strokeStyle = "#00e676";
  ctx.lineWidth = 2;
  ctx.beginPath();
  corners.forEach((pt, i) => {
    const ux = lx + (pt[0] - fx) * imgW * zoomFactor;
    const uy = ly + (pt[1] - fy) * imgH * zoomFactor;
    if (i === 0) ctx.moveTo(ux, uy);
    else ctx.lineTo(ux, uy);
  });
  ctx.closePath();
  ctx.stroke();

  // Rdeči križec točno na robu / oglišču
  ctx.strokeStyle = "#ff1744";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(lx - 13, ly);
  ctx.lineTo(lx + 13, ly);
  ctx.moveTo(lx, ly - 13);
  ctx.lineTo(lx, ly + 13);
  ctx.stroke();

  ctx.restore();

  // Obroč lupe
  ctx.save();
  ctx.beginPath();
  ctx.arc(lx, ly, R, 0, Math.PI * 2);
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = "#00e676";
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(lx, ly, R - 2, 0, Math.PI * 2);
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
  ctx.stroke();
  ctx.restore();
}

function draw() {
  if (!img.naturalWidth) return;
  ctx.clearRect(0, 0, displayW, displayH);
  ctx.fillStyle = "#141414";
  ctx.fillRect(0, 0, displayW, displayH);
  ctx.drawImage(img, padX, padY, imgW, imgH);

  // Zatemnitev zunaj izbranega štirikotnika
  ctx.save();
  ctx.fillStyle = "rgba(0, 0, 0, 0.45)";
  ctx.beginPath();
  ctx.rect(padX, padY, imgW, imgH);
  const [x0, y0] = normToCanvas(corners[0][0], corners[0][1]);
  ctx.moveTo(x0, y0);
  for (let i = 1; i < 4; i++) {
    const [xi, yi] = normToCanvas(corners[i][0], corners[i][1]);
    ctx.lineTo(xi, yi);
  }
  ctx.closePath();
  ctx.fill("evenodd");
  ctx.restore();

  // Zelena meja izreza
  ctx.strokeStyle = "#00e676";
  ctx.lineWidth = 2;
  ctx.beginPath();
  corners.forEach((pt, i) => {
    const [x, y] = normToCanvas(pt[0], pt[1]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.closePath();
  ctx.stroke();

  // Ročke na sredini robov
  for (let i = 0; i < 4; i++) {
    const p1 = corners[i];
    const p2 = corners[(i + 1) % 4];
    const [mx, my] = normToCanvas(0.5 * (p1[0] + p2[0]), 0.5 * (p1[1] + p2[1]));
    ctx.beginPath();
    ctx.rect(mx - 5, my - 5, 10, 10);
    ctx.fillStyle = (dragMode === "edge" && draggingIdx === i) ? "#ffab00" : "#ffffff";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "#00c853";
    ctx.stroke();
  }

  // 4 oglišča
  corners.forEach((pt, i) => {
    const [x, y] = normToCanvas(pt[0], pt[1]);
    const r = (i === activeIdx) ? 8.5 : 7;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = (i === activeIdx) ? "#ff1744" : "#00e676";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
  });

  // Lupa se pojavi izključno na pritisnjenem oglišču ali robu med držanjem
  if (draggingIdx >= 0) {
    if (dragMode === "corner") {
      drawLoupe(corners[draggingIdx][0], corners[draggingIdx][1]);
    } else if (dragMode === "edge") {
      const p1 = corners[draggingIdx];
      const p2 = corners[(draggingIdx + 1) % 4];
      const fx = p1[0] + dragEdgeT * (p2[0] - p1[0]);
      const fy = p1[1] + dragEdgeT * (p2[1] - p1[1]);
      drawLoupe(fx, fy);
    }
  } else if (showKeyLoupe && activeIdx >= 0) {
    drawLoupe(corners[activeIdx][0], corners[activeIdx][1]);
  }
}

function getEventCanvasPos(e) {
  const rect = mainCanvas.getBoundingClientRect();
  const src = (e.touches && e.touches.length) ? e.touches[0] : e;
  return [
    (src.clientX - rect.left) * (displayW / Math.max(1, rect.width)),
    (src.clientY - rect.top) * (displayH / Math.max(1, rect.height))
  ];
}

function canvasToNorm(cx, cy) {
  return [
    clamp01((cx - padX) / Math.max(1, imgW)),
    clamp01((cy - padY) / Math.max(1, imgH))
  ];
}

function onDown(e) {
  isTouchDrag = Boolean(e.touches && e.touches.length);
  const [cx, cy] = getEventCanvasPos(e);
  const [nx, ny] = canvasToNorm(cx, cy);
  mainCanvas.focus();

  const hitCorner = isTouchDrag ? 36 : 24;
  const hitEdge = isTouchDrag ? 26 : 16;

  // 1. Preverimo dotik oglišča
  let bestDist = 9999;
  let bestIdx = -1;
  corners.forEach((pt, i) => {
    const [px, py] = normToCanvas(pt[0], pt[1]);
    const d = Math.hypot(px - cx, py - cy);
    if (d < bestDist) { bestDist = d; bestIdx = i; }
  });
  if (bestDist < hitCorner) {
    activeIdx = bestIdx;
    dragMode = "corner";
    draggingIdx = bestIdx;
    e.preventDefault();
    draw();
    return;
  }

  // 2. Preverimo dotik kjerkoli vzdolž kateregakoli roba
  let bestEdgeDist = 9999;
  let bestEdgeIdx = -1;
  let bestEdgeT = 0.5;
  for (let i = 0; i < 4; i++) {
    const [x1, y1] = normToCanvas(corners[i][0], corners[i][1]);
    const [x2, y2] = normToCanvas(corners[(i + 1) % 4][0], corners[(i + 1) % 4][1]);
    const seg = distToSegment(cx, cy, x1, y1, x2, y2);
    if (seg.dist < bestEdgeDist) {
      bestEdgeDist = seg.dist;
      bestEdgeIdx = i;
      bestEdgeT = seg.t;
    }
  }
  if (bestEdgeDist < hitEdge) {
    dragMode = "edge";
    draggingIdx = bestEdgeIdx;
    dragEdgeT = bestEdgeT;
    dragStartPos = [nx, ny];
    dragStartCorners = corners.map(pt => [pt[0], pt[1]]);
    activeIdx = bestEdgeIdx;
    e.preventDefault();
    draw();
  }
}

function onMove(e) {
  if (draggingIdx < 0) return;
  e.preventDefault();
  const [cx, cy] = getEventCanvasPos(e);
  const [nx, ny] = canvasToNorm(cx, cy);
  if (dragMode === "corner") {
    corners[draggingIdx] = [nx, ny];
  } else if (dragMode === "edge" && dragStartCorners) {
    const dx = nx - dragStartPos[0];
    const dy = ny - dragStartPos[1];
    const i1 = draggingIdx;
    const i2 = (draggingIdx + 1) % 4;
    corners[i1] = [clamp01(dragStartCorners[i1][0] + dx), clamp01(dragStartCorners[i1][1] + dy)];
    corners[i2] = [clamp01(dragStartCorners[i2][0] + dx), clamp01(dragStartCorners[i2][1] + dy)];
  }
  draw();
}

function onUp() {
  if (draggingIdx >= 0) {
    draggingIdx = -1;
    dragMode = null;
    dragStartCorners = null;
    draw();
    sendMessage("streamlit:setComponentValue", { value: corners });
  }
}

mainCanvas.addEventListener("keydown", (e) => {
  if (activeIdx < 0) return;
  const stepPx = e.shiftKey ? 5.0 : 1.0;
  const dx = stepPx / Math.max(1, img.naturalWidth);
  const dy = stepPx / Math.max(1, img.naturalHeight);
  let moved = true;
  if (e.key === "ArrowLeft") corners[activeIdx][0] = clamp01(corners[activeIdx][0] - dx);
  else if (e.key === "ArrowRight") corners[activeIdx][0] = clamp01(corners[activeIdx][0] + dx);
  else if (e.key === "ArrowUp") corners[activeIdx][1] = clamp01(corners[activeIdx][1] - dy);
  else if (e.key === "ArrowDown") corners[activeIdx][1] = clamp01(corners[activeIdx][1] + dy);
  else moved = false;

  if (moved) {
    e.preventDefault();
    isTouchDrag = false;
    showKeyLoupe = true;
    if (keyLoupeTimer) clearTimeout(keyLoupeTimer);
    keyLoupeTimer = setTimeout(() => { showKeyLoupe = false; draw(); }, 1000);
    draw();
    sendMessage("streamlit:setComponentValue", { value: corners });
  }
});

mainCanvas.addEventListener("mousedown", onDown);
window.addEventListener("mousemove", onMove);
window.addEventListener("mouseup", onUp);
mainCanvas.addEventListener("touchstart", onDown, { passive: false });
window.addEventListener("touchmove", onMove, { passive: false });
window.addEventListener("touchend", onUp);
window.addEventListener("touchcancel", onUp);
window.addEventListener("resize", resizeCanvas);

if (typeof ResizeObserver !== "undefined") {
  const ro = new ResizeObserver(() => resizeCanvas());
  ro.observe(wrapperEl);
}

window.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "streamlit:render") return;
  const args = event.data.args;
  const needImageReload = args.image_b64 !== currentB64;
  const needCornerReset = (args.reset_version !== undefined && args.reset_version !== currentResetVersion);

  if (needCornerReset && args.initial_corners) {
    currentResetVersion = args.reset_version;
    corners = args.initial_corners.map(pt => [pt[0], pt[1]]);
    if (!needImageReload) draw();
  }

  if (needImageReload) {
    currentB64 = args.image_b64;
    if (args.reset_version !== undefined) currentResetVersion = args.reset_version;
    if (args.initial_corners) corners = args.initial_corners.map(pt => [pt[0], pt[1]]);
    img.onload = () => {
      resizeCanvas();
      setTimeout(resizeCanvas, 150);
    };
    img.src = "data:image/jpeg;base64," + currentB64;
  } else {
    resizeCanvas();
  }
});

sendMessage("streamlit:componentReady", { apiVersion: 1 });
</script>
</body>
</html>
"""

_obstojece = ""
if os.path.exists(INDEX_POT):
    with open(INDEX_POT, "r", encoding="utf-8") as f:
        _obstojece = f.read()
if _obstojece != HTML_KODA_IZREZA:
    with open(INDEX_POT, "w", encoding="utf-8") as f:
        f.write(HTML_KODA_IZREZA)

_komponenta_izrez = components.declare_component("stirikotnik_izrez_v2", path=KOMPONENTA_MAPA)


# =====================================================================
# OBREZOVANJE LISTIČA (rembg u2net + senca + zanesljiv notranji odrez roba)
# =====================================================================

def uredi_oglisca(tocke_pravokotnika):
    """Uredi 4 oglišča: zgoraj levo, zgoraj desno, spodaj desno, spodaj levo."""
    pts = np.array(tocke_pravokotnika, dtype=np.float32).reshape(4, 2)
    vsota = pts.sum(axis=1)
    razlika = np.diff(pts, axis=1)

    idx = [
        int(np.argmin(vsota)),
        int(np.argmin(razlika)),
        int(np.argmax(vsota)),
        int(np.argmax(razlika)),
    ]
    if len(set(idx)) == 4:
        return pts[idx].astype(np.float32)

    center = np.mean(pts, axis=0)
    koti = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    urejene = pts[np.argsort(koti)]
    zacetni_idx = int(np.argmin(urejene[:, 0] + urejene[:, 1]))
    return np.roll(urejene, -zacetni_idx, axis=0).astype(np.float32)


def _prilagodi_locljivost_listica(poravnan_listic):
    """Ohrani visoko ločljivost skenov (do 3040 px), da 50 μm kapljice ne izginejo."""
    trenutna_visina, trenutna_sirina = poravnan_listic.shape[:2]
    razmerje = trenutna_visina / float(trenutna_sirina)
    ciljna_sirina = int(
        np.clip(trenutna_sirina, MIN_STANDARDNA_DOLZINA_PX, MAX_STANDARDNA_DOLZINA_PX)
    )
    nova_visina = max(50, int(round(ciljna_sirina * razmerje)))

    if ciljna_sirina == trenutna_sirina:
        return poravnan_listic

    metoda = cv2.INTER_AREA if ciljna_sirina < trenutna_sirina else cv2.INTER_LINEAR
    return cv2.resize(poravnan_listic, (ciljna_sirina, nova_visina), interpolation=metoda)


def _zamanjsaj_okvir_navznoter(pts1, w_orig, h_orig, delez_krajse=0.04, delez_daljse=0.02):
    """
    Po osnovnem izrezu in odstranitvi sence dodatno odreže 4 % krajše stranice
    (~0,9 mm zgoraj in spodaj) ter 2 % daljše stranice (~1,5 mm levo in desno),
    da rob lističa nikoli ni zaznan kot odtis.
    """
    zl, zd, sd, sl = pts1
    sirina = max(float(np.linalg.norm(sd - sl)), float(np.linalg.norm(zd - zl)), 50.0)
    visina = max(float(np.linalg.norm(zd - sd)), float(np.linalg.norm(zl - sl)), 50.0)

    if sirina <= visina:
        odmik_x = min(max(3.0, sirina * delez_krajse), sirina * 0.10)
        odmik_y = min(max(3.0, visina * delez_daljse), visina * 0.08)
    else:
        odmik_x = min(max(3.0, sirina * delez_daljse), sirina * 0.08)
        odmik_y = min(max(3.0, visina * delez_krajse), visina * 0.10)

    pts2 = np.float32([
        [0.0, 0.0],
        [sirina - 1.0, 0.0],
        [sirina - 1.0, visina - 1.0],
        [0.0, visina - 1.0],
    ])
    matrika = cv2.getPerspectiveTransform(pts1, pts2)
    matrika_inv = np.linalg.inv(matrika)

    notranji = np.float32([
        [odmik_x, odmik_y],
        [sirina - 1.0 - odmik_x, odmik_y],
        [sirina - 1.0 - odmik_x, visina - 1.0 - odmik_y],
        [odmik_x, visina - 1.0 - odmik_y],
    ])
    novi = cv2.perspectiveTransform(notranji.reshape(-1, 1, 2), matrika_inv).reshape(4, 2)
    novi[:, 0] = np.clip(novi[:, 0], 0.0, float(w_orig - 1))
    novi[:, 1] = np.clip(novi[:, 1], 0.0, float(h_orig - 1))
    return uredi_oglisca(novi)


def _robustna_premica(tocke_a, tocke_b, privzeta_vrednost):
    """
    Prileganje premice b = m * a + c z medianama leve/desne polovice in odstranjevanjem
    osamelcev, da posamezni temni odtisi ali luknjice ob robu ne potegnejo roba navznoter.
    """
    if len(tocke_a) < 8:
        return 0.0, float(privzeta_vrednost)

    a = np.asarray(tocke_a, dtype=np.float64)
    b = np.asarray(tocke_b, dtype=np.float64)
    vrstni_red = np.argsort(a)
    a = a[vrstni_red]
    b = b[vrstni_red]

    pol = len(a) // 2
    a1, b1 = float(np.median(a[:pol])), float(np.median(b[:pol]))
    a2, b2 = float(np.median(a[pol:])), float(np.median(b[pol:]))

    if abs(a2 - a1) < 1e-3:
        return 0.0, float(np.median(b))

    m0 = (b2 - b1) / (a2 - a1)
    c0 = b1 - m0 * a1

    ostanki = np.abs(b - (m0 * a + c0))
    mad = float(np.median(ostanki))
    prag = max(2.5, 2.2 * mad)
    veljavne = ostanki <= prag

    if int(np.sum(veljavne)) >= 6:
        m, c = np.polyfit(a[veljavne], b[veljavne], 1)
        m, c = float(m), float(c)
    else:
        m, c = float(m0), float(c0)

    if abs(m) > 0.12:
        return 0.0, float(np.median(b))
    return m, c


def _najdi_robno_premico(je_senca, smer, max_odrez):
    """
    Skenira samo od zunanjega roba maske navznoter (največ max_odrez pikslov) in najde
    prva 2 zaporedna piksla lističa, nato skozi najdene točke prilega ravno robno premico.
    """
    h, w = je_senca.shape
    if smer in ("top", "bottom"):
        max_o = min(max_odrez, max(1, h // 2 - 2))
        zac = max(0, int(round(0.06 * w)))
        kon = min(w, int(round(0.94 * w)))
        korak = max(1, (kon - zac) // 140)
        vzorec_x = list(range(zac, kon, korak))
        xs, ys = [], []

        if smer == "top":
            for x in vzorec_x:
                for y in range(0, max_o):
                    if not je_senca[y, x] and not je_senca[y + 1, x]:
                        xs.append(x)
                        ys.append(y)
                        break
            if len(xs) < max(8, int(0.25 * len(vzorec_x))):
                return 0.0, 0.0
            return _robustna_premica(xs, ys, 0.0)
        else:
            for x in vzorec_x:
                for y in range(h - 1, h - 1 - max_o, -1):
                    if not je_senca[y, x] and not je_senca[y - 1, x]:
                        xs.append(x)
                        ys.append(y)
                        break
            if len(xs) < max(8, int(0.25 * len(vzorec_x))):
                return 0.0, float(h - 1)
            return _robustna_premica(xs, ys, float(h - 1))
    else:
        max_o = min(max_odrez, max(1, w // 2 - 2))
        zac = max(0, int(round(0.06 * h)))
        kon = min(h, int(round(0.94 * h)))
        korak = max(1, (kon - zac) // 140)
        vzorec_y = list(range(zac, kon, korak))
        ys, xs = [], []

        if smer == "left":
            for y in vzorec_y:
                for x in range(0, max_o):
                    if not je_senca[y, x] and not je_senca[y, x + 1]:
                        ys.append(y)
                        xs.append(x)
                        break
            if len(ys) < max(8, int(0.25 * len(vzorec_y))):
                return 0.0, 0.0
            return _robustna_premica(ys, xs, 0.0)
        else:
            for y in vzorec_y:
                for x in range(w - 1, w - 1 - max_o, -1):
                    if not je_senca[y, x] and not je_senca[y, x - 1]:
                        ys.append(y)
                        xs.append(x)
                        break
            if len(ys) < max(8, int(0.25 * len(vzorec_y))):
                return 0.0, float(w - 1)
            return _robustna_premica(ys, xs, float(w - 1))


def _presek_robov(premica_h, premica_v):
    """Presek vodoravne premice y = mh*x + ch in navpične premice x = mv*y + cv."""
    mh, ch = premica_h
    mv, cv = premica_v
    imenovalec = 1.0 - mh * mv
    if abs(imenovalec) < 1e-6:
        imenovalec = 1.0
    y = (mh * cv + ch) / imenovalec
    x = mv * y + cv
    return [float(x), float(y)]


def _odstrani_senco_na_robovih(slika_rgb, alpha_kanal, zacetna_maska, pts1, max_sirina, max_visina):
    """
    Odstrani samo zunanjo kontaktno in sivo senco vzdolž 4 ravnih robov lističa,
    brez poseganja v notranjost lističa ali v modre/temne kapljice ob robu.
    Vrne popravljena 4 oglišča v koordinatah izvirne slike.
    """
    if max_sirina < 40 or max_visina < 40:
        return pts1

    pts2 = np.float32([
        [0, 0],
        [max_sirina - 1, 0],
        [max_sirina - 1, max_visina - 1],
        [0, max_visina - 1],
    ])
    matrika = cv2.getPerspectiveTransform(pts1, pts2)

    poravnan_rgb = cv2.warpPerspective(
        slika_rgb, matrika, (max_sirina, max_visina), flags=cv2.INTER_LINEAR
    )
    poravnan_alpha = cv2.warpPerspective(
        alpha_kanal, matrika, (max_sirina, max_visina), flags=cv2.INTER_LINEAR
    )
    poravnan_maska = cv2.warpPerspective(
        zacetna_maska, matrika, (max_sirina, max_visina), flags=cv2.INTER_NEAREST
    )

    lab = cv2.cvtColor(poravnan_rgb, cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(poravnan_rgb, cv2.COLOR_RGB2HSV)

    L = lab[:, :, 0].astype(np.float32)
    a_lab = lab[:, :, 1].astype(np.float32)
    b_lab = lab[:, :, 2].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)

    R = poravnan_rgb[:, :, 0].astype(np.float32)
    B = poravnan_rgb[:, :, 2].astype(np.float32)

    # Notranje jedro za referenčno barvo rumenega papirja (varno oddaljeno od robnih senc)
    rob_odmik = max(4, int(round(0.18 * min(max_sirina, max_visina))))
    notranjost = np.zeros((max_visina, max_sirina), dtype=bool)
    if (max_visina - 2 * rob_odmik) >= 10 and (max_sirina - 2 * rob_odmik) >= 10:
        notranjost[rob_odmik : max_visina - rob_odmik, rob_odmik : max_sirina - rob_odmik] = True
    notranjost &= (poravnan_maska > 127)
    if np.sum(notranjost) < 50:
        notranjost = poravnan_maska > 127
    if np.sum(notranjost) < 50:
        return pts1

    # Varnostno preverjanje: če slika ni rumeni listič, oglišč ne spreminjamo
    p85_b = float(np.percentile(b_lab[notranjost], 85))
    if p85_b < 136.0:
        return pts1

    je_rumeno_jedro = notranjost & (b_lab >= max(136.0, p85_b - 16.0))
    if np.sum(je_rumeno_jedro) < 30:
        je_rumeno_jedro = notranjost

    ref_L = float(np.median(L[je_rumeno_jedro]))
    ref_a = float(np.median(a_lab[je_rumeno_jedro]))
    ref_b = float(np.median(b_lab[je_rumeno_jedro]))

    # Lokalna svetlost rumenega papirja (ščiti pred neenakomerno osvetlitvijo lističa)
    rumeno_za_mapo = (poravnan_maska > 127) & (b_lab >= max(136.0, ref_b - 18.0)) & (L >= ref_L * 0.50)
    if np.sum(rumeno_za_mapo) >= 50:
        ref_L_mapa = _oceni_lokalno_ozadje(L, rumeno_za_mapo, notranjost)
        ref_L_mapa = np.clip(ref_L_mapa, ref_L * 0.60, ref_L * 1.15)
    else:
        ref_L_mapa = np.full_like(L, ref_L)

    # Barva ozadja neposredno zunaj lističa (za zaznavo sence na dejanski podlagi)
    h_orig, w_orig = slika_rgb.shape[:2]
    merilo_bg = min(1.0, 600.0 / float(max(h_orig, w_orig)))
    if merilo_bg < 1.0:
        s_bg = cv2.resize(
            slika_rgb,
            (max(16, int(round(w_orig * merilo_bg))), max(16, int(round(h_orig * merilo_bg)))),
            interpolation=cv2.INTER_AREA,
        )
        m_bg = cv2.resize(
            zacetna_maska, (s_bg.shape[1], s_bg.shape[0]), interpolation=cv2.INTER_NEAREST
        )
    else:
        s_bg = slika_rgb
        m_bg = zacetna_maska

    k_zunanji = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    k_notranji = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    obroc_ozadja = (cv2.dilate(m_bg, k_zunanji) > 0) & (cv2.dilate(m_bg, k_notranji) == 0)

    ozadje_razlicno = False
    bg_L, bg_a, bg_b = 128.0, 128.0, 128.0
    if np.sum(obroc_ozadja) >= 50:
        lab_bg = cv2.cvtColor(s_bg, cv2.COLOR_RGB2LAB)
        bg_L = float(np.median(lab_bg[:, :, 0][obroc_ozadja]))
        bg_a = float(np.median(lab_bg[:, :, 1][obroc_ozadja]))
        bg_b = float(np.median(lab_bg[:, :, 2][obroc_ozadja]))
        ozadje_razlicno = (math.hypot(bg_a - ref_a, bg_b - ref_b) > 18.0) or (abs(bg_L - ref_L) > 35.0)

    # Modri in temno-modri odtisi kapljic NIKOLI niso označeni kot senca
    je_moder = ((B - R) >= 7.0) | (b_lab <= 124.0)

    # 1. Zunaj maske rembg ali mehak prosojen rob
    je_zunaj = (poravnan_maska < 128) | (poravnan_alpha < 135)

    # 2. Temna kontaktna senca tik pod robom papirja (temnejša od lokalnega rumenega papirja, ni modra)
    je_temna_senca = (~je_moder) & (L < (ref_L_mapa * 0.60)) & (L < 145.0)

    # 3. Nevtralna siva senca (nizka nasičenost, ni ne rumena ne modra)
    je_siva_senca = (~je_moder) & (S < 44.0) & (b_lab <= 137.0) & (L < (ref_L_mapa * 0.90))

    # 4. Senca na barvi zaznanega ozadja
    if ozadje_razlicno:
        barvna_razd = np.hypot(a_lab - bg_a, b_lab - bg_b)
        je_senca_ozadja = (
            (~je_moder)
            & (barvna_razd < 13.0)
            & (L <= (bg_L + 14.0))
            & (b_lab < (ref_b - 14.0))
        )
    else:
        je_senca_ozadja = np.zeros_like(je_zunaj)

    je_senca = je_zunaj | je_temna_senca | je_siva_senca | je_senca_ozadja

    # Največja dovoljena debelina sence ob kateremkoli robu (16 % krajše stranice)
    max_odrez = max(6, int(round(0.16 * min(max_sirina, max_visina))))

    p_top = _najdi_robno_premico(je_senca, "top", max_odrez)
    p_bot = _najdi_robno_premico(je_senca, "bottom", max_odrez)
    p_left = _najdi_robno_premico(je_senca, "left", max_odrez)
    p_right = _najdi_robno_premico(je_senca, "right", max_odrez)

    tl = _presek_robov(p_top, p_left)
    tr = _presek_robov(p_top, p_right)
    br = _presek_robov(p_bot, p_right)
    bl = _presek_robov(p_bot, p_left)

    novi_pts2 = np.array([tl, tr, br, bl], dtype=np.float32)
    novi_pts2[:, 0] = np.clip(novi_pts2[:, 0], 0.0, float(max_sirina - 1))
    novi_pts2[:, 1] = np.clip(novi_pts2[:, 1], 0.0, float(max_visina - 1))

    # Varnostna preverjanja geometrije in površine
    if (
        novi_pts2[0, 0] >= novi_pts2[1, 0] - 10
        or novi_pts2[3, 0] >= novi_pts2[2, 0] - 10
        or novi_pts2[0, 1] >= novi_pts2[3, 1] - 10
        or novi_pts2[1, 1] >= novi_pts2[2, 1] - 10
    ):
        return pts1

    orig_povrsina = float((max_sirina - 1) * (max_visina - 1))
    nova_povrsina = float(cv2.contourArea(novi_pts2))
    if nova_povrsina < 0.65 * orig_povrsina:
        return pts1

    matrika_inv = np.linalg.inv(matrika)
    novi_pts1 = cv2.perspectiveTransform(novi_pts2.reshape(-1, 1, 2), matrika_inv).reshape(4, 2)
    novi_pts1[:, 0] = np.clip(novi_pts1[:, 0], 0.0, float(w_orig - 1))
    novi_pts1[:, 1] = np.clip(novi_pts1[:, 1], 0.0, float(h_orig - 1))
    return uredi_oglisca(novi_pts1)


def poisci_in_izrezi_avtomatsko(slika_rgb):
    """
    Izolira listič z rembg (u2net), obreže zunanjo senco vzdolž 4 ravnih robov,
    nato dodatno odreže varen notranji pas (4 % krajše in 2 % daljše stranice)
    neposredno iz čiste RGB slike brez črnega roba ter listič poravna.
    Vrne: (koncni_listic_rgba, norm_oglisca, dejanska_maska_izreza)
    """
    h, w = slika_rgb.shape[:2]
    izoliran_listic = pridobi_izoliran_rgba(slika_rgb)
    alpha_kanal = izoliran_listic[:, :, 3]

    _, vidna_maska = cv2.threshold(alpha_kanal, 128, 255, cv2.THRESH_BINARY)
    konture, _ = cv2.findContours(vidna_maska, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not konture:
        konture, _ = cv2.findContours(alpha_kanal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not konture:
        privzeta = [[0.15, 0.15], [0.85, 0.15], [0.85, 0.85], [0.15, 0.85]]
        polna_maska = np.full((h, w), 255, dtype=np.uint8)
        return izoliran_listic, privzeta, polna_maska

    najvecja_kontura = max(konture, key=cv2.contourArea)
    zacetna_maska = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(zacetna_maska, [najvecja_kontura], -1, 255, thickness=-1)

    pravokotnik = cv2.minAreaRect(najvecja_kontura)
    tocke_pravokotnika = cv2.boxPoints(pravokotnik)
    pts1_zacetna = uredi_oglisca(tocke_pravokotnika)
    (zl, zd, sd, sl) = pts1_zacetna

    max_sirina_z = max(int(np.linalg.norm(sd - sl)), int(np.linalg.norm(zd - zl)), 50)
    max_visina_z = max(int(np.linalg.norm(zd - sd)), int(np.linalg.norm(zl - sl)), 50)

    # 1. Odstranimo zunanjo senco vzdolž 4 robov
    pts1_brez_sence = _odstrani_senco_na_robovih(
        slika_rgb, alpha_kanal, zacetna_maska, pts1_zacetna, max_sirina_z, max_visina_z
    )
    # 2. Dodaten notranji odrez (4 % na krajši stranici, 2 % na daljši), da rob ne ostane v izrezu
    pts1 = _zamanjsaj_okvir_navznoter(
        pts1_brez_sence, w, h, delez_krajse=0.04, delez_daljse=0.02
    )
    (zgoraj_levo, zgoraj_desno, spodaj_desno, spodaj_levo) = pts1

    sirina_a = np.linalg.norm(spodaj_desno - spodaj_levo)
    sirina_b = np.linalg.norm(zgoraj_desno - zgoraj_levo)
    max_sirina = max(int(sirina_a), int(sirina_b), 50)

    visina_a = np.linalg.norm(zgoraj_desno - spodaj_desno)
    visina_b = np.linalg.norm(zgoraj_levo - spodaj_levo)
    max_visina = max(int(visina_a), int(visina_b), 50)

    pts2 = np.float32([
        [0, 0],
        [max_sirina - 1, 0],
        [max_sirina - 1, max_visina - 1],
        [0, max_visina - 1],
    ])
    matrika = cv2.getPerspectiveTransform(pts1, pts2)

    # Perspektivni izrez izvajamo neposredno iz izvirne RGB slike, da preprečimo
    # prelivanje črnega ozadja modela rembg na robove lističa
    maska_stirikotnika = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(maska_stirikotnika, [np.int32(np.round(pts1))], 255)
    pov_stirikotnika = max(1, cv2.countNonZero(maska_stirikotnika))
    pokritost_znotraj = cv2.countNonZero(cv2.bitwise_and(maska_stirikotnika, zacetna_maska)) / float(
        pov_stirikotnika
    )

    poravnan_rgb = cv2.warpPerspective(
        slika_rgb, matrika, (max_sirina, max_visina), flags=cv2.INTER_LINEAR
    )

    if pokritost_znotraj >= 0.90:
        dejanska_maska = maska_stirikotnika
        poravnan_alpha = np.full((max_visina, max_sirina), 255, dtype=np.uint8)
    else:
        zozena_zacetna = cv2.erode(zacetna_maska, np.ones((3, 3), np.uint8), iterations=1)
        dejanska_maska = cv2.bitwise_and(maska_stirikotnika, zozena_zacetna)
        if cv2.countNonZero(dejanska_maska) < 100:
            dejanska_maska = maska_stirikotnika
        poravnan_alpha = cv2.warpPerspective(
            dejanska_maska, matrika, (max_sirina, max_visina), flags=cv2.INTER_NEAREST
        )

    poravnan_listic = np.concatenate([poravnan_rgb, poravnan_alpha[:, :, None]], axis=2)

    # Vodoravna poravnava (daljša stranica je širina)
    if poravnan_listic.shape[0] > poravnan_listic.shape[1]:
        poravnan_listic = cv2.rotate(poravnan_listic, cv2.ROTATE_90_CLOCKWISE)

    koncni_listic = _prilagodi_locljivost_listica(poravnan_listic)

    norm_oglisca = [
        [float(np.clip(x / w, 0.0, 1.0)), float(np.clip(y / h, 0.0, 1.0))]
        for x, y in pts1
    ]
    return koncni_listic, norm_oglisca, dejanska_maska


def izrezi_listic_rocno(slika_rgb, norm_oglisca):
    """Perspektivni izrez za ročni način iz 4 ročno nastavljenih oglišč."""
    h, w = slika_rgb.shape[:2]
    tocke = np.array([[pt[0] * w, pt[1] * h] for pt in norm_oglisca], dtype=np.float32)
    pts1 = uredi_oglisca(tocke)
    (zgoraj_levo, zgoraj_desno, spodaj_desno, spodaj_levo) = pts1

    sirina_a = np.linalg.norm(spodaj_desno - spodaj_levo)
    sirina_b = np.linalg.norm(zgoraj_desno - zgoraj_levo)
    max_sirina = max(int(sirina_a), int(sirina_b), 50)

    visina_a = np.linalg.norm(zgoraj_desno - spodaj_desno)
    visina_b = np.linalg.norm(zgoraj_levo - spodaj_levo)
    max_visina = max(int(visina_a), int(visina_b), 50)

    pts2 = np.float32([
        [0, 0],
        [max_sirina - 1, 0],
        [max_sirina - 1, max_visina - 1],
        [0, max_visina - 1],
    ])
    matrika = cv2.getPerspectiveTransform(pts1, pts2)
    poravnan_rgb = cv2.warpPerspective(
        slika_rgb, matrika, (max_sirina, max_visina), flags=cv2.INTER_LINEAR
    )
    polni_alpha = np.full((max_visina, max_sirina, 1), 255, dtype=np.uint8)
    poravnan_listic = np.concatenate([poravnan_rgb, polni_alpha], axis=2)

    if poravnan_listic.shape[0] > poravnan_listic.shape[1]:
        poravnan_listic = cv2.rotate(poravnan_listic, cv2.ROTATE_90_CLOCKWISE)

    return _prilagodi_locljivost_listica(poravnan_listic)


def narisi_predogled_izreza(slika_rgb, norm_oglisca, dejanska_maska=None):
    """
    Nariše točen predogled avtomatskega izreza na izvirni sliki:
    zatemni ozadje zunaj dejanske maske in obkroži izrezan listič z zeleno črto.
    """
    h, w = slika_rgb.shape[:2]
    max_dim = 1000.0
    merilo = min(1.0, max_dim / float(max(w, h)))
    pw, ph = int(round(w * merilo)), int(round(h * merilo))
    predogled = (
        cv2.resize(slika_rgb, (pw, ph), interpolation=cv2.INTER_AREA)
        if merilo < 1.0
        else slika_rgb.copy()
    )

    zatemnjena = (predogled.astype(np.float32) * 0.42).astype(np.uint8)
    debelina = max(2, int(round(min(pw, ph) * 0.004)))

    if dejanska_maska is not None:
        maska_p = (
            cv2.resize(dejanska_maska, (pw, ph), interpolation=cv2.INTER_NEAREST)
            if merilo < 1.0
            else dejanska_maska
        )
        rezultat = np.where(maska_p[:, :, None] > 127, predogled, zatemnjena)
        konture, _ = cv2.findContours(
            (maska_p > 127).astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if konture:
            cv2.drawContours(
                rezultat, konture, -1, (0, 230, 118), thickness=debelina, lineType=cv2.LINE_AA
            )
        return rezultat

    pts = uredi_oglisca(np.array([[pt[0] * pw, pt[1] * ph] for pt in norm_oglisca], dtype=np.float32))
    pts_int = np.int32(np.round(pts))

    maska = np.zeros((ph, pw), dtype=np.uint8)
    cv2.fillPoly(maska, [pts_int], 255)
    rezultat = np.where(maska[:, :, None] > 0, predogled, zatemnjena)
    cv2.polylines(rezultat, [pts_int], isClosed=True, color=(0, 230, 118), thickness=debelina, lineType=cv2.LINE_AA)
    return rezultat


def prikazi_rocni_izrez(slika_rgb, zacetna_oglisca, kljuc, reset_version=0):
    h, w = slika_rgb.shape[:2]
    max_dim = 900
    merilo = min(1.0, max_dim / max(w, h))
    predogled = cv2.resize(slika_rgb, (int(w * merilo), int(h * merilo)), interpolation=cv2.INTER_AREA)

    pil_img = Image.fromarray(predogled)
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=88)
    b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")

    rezultat = _komponenta_izrez(
        image_b64=b64_str,
        initial_corners=zacetna_oglisca,
        reset_version=int(reset_version),
        key=kljuc,
    )
    return rezultat if rezultat is not None else zacetna_oglisca


# =====================================================================
# ANALIZA KAPLJIC (USKLAJENI PRAGOVI + BREZ OBARVANJA VMESNIH PROSTOROV)
# =====================================================================

def _oceni_lokalno_ozadje(kanal, maska_cisto_ozadje, veljavni_indeksi):
    """
    Z normalizirano konvolucijo oceni gladko 2D površino lokalnega čistega ozadja samo iz
    nepoškropljenih rumenih pikslov, zato velike kapljice ne potegnejo ozadja navzdol.
    """
    h, w = kanal.shape
    ds = max(8, int(round(max(h, w) / 200.0)))
    w_s = max(16, w // ds)
    h_s = max(16, h // ds)

    kanal_s = cv2.resize(kanal, (w_s, h_s), interpolation=cv2.INTER_AREA)
    utez_s = cv2.resize(
        maska_cisto_ozadje.astype(np.float32), (w_s, h_s), interpolation=cv2.INTER_AREA
    )
    utez_s = (utez_s > 0.75).astype(np.float32)

    if np.sum(utez_s) < 12:
        utez_s = cv2.resize(
            maska_cisto_ozadje.astype(np.float32), (w_s, h_s), interpolation=cv2.INTER_AREA
        )

    globalna_ref = float(
        np.median(kanal[maska_cisto_ozadje])
        if np.any(maska_cisto_ozadje)
        else np.percentile(kanal[veljavni_indeksi], 85)
    )

    stevec_1 = cv2.GaussianBlur(kanal_s * utez_s, (0, 0), sigmaX=10.0)
    imenov_1 = cv2.GaussianBlur(utez_s, (0, 0), sigmaX=10.0)

    stevec_2 = cv2.GaussianBlur(kanal_s * utez_s, (0, 0), sigmaX=32.0)
    imenov_2 = cv2.GaussianBlur(utez_s, (0, 0), sigmaX=32.0)

    ozadje_s = np.where(
        imenov_1 > 0.05,
        stevec_1 / np.maximum(imenov_1, 1e-5),
        np.where(
            imenov_2 > 0.01,
            stevec_2 / np.maximum(imenov_2, 1e-5),
            globalna_ref,
        ),
    ).astype(np.float32)

    return cv2.resize(ozadje_s, (w, h), interpolation=cv2.INTER_LINEAR)


def _smerni_tophat_za_podolgovate(signal, dolzina=9):
    """Poudari tanke podolgovate odtise v 4 smereh brez morfološkega širjenja robov."""
    dolzina = max(5, int(dolzina) | 1)

    k_vodoravno = np.ones((1, dolzina), dtype=np.uint8)
    k_navpicno = np.ones((dolzina, 1), dtype=np.uint8)
    k_diag1 = np.eye(dolzina, dtype=np.uint8)
    k_diag2 = np.fliplr(k_diag1)

    th_v = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_navpicno)
    th_h = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_vodoravno)
    th_d1 = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_diag1)
    th_d2 = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_diag2)

    return np.maximum(np.maximum(th_v, th_h), np.maximum(th_d1, th_d2))


def _smerni_blackhat_za_doline(signal, dolzina=13):
    """Zazna ozke rumene doline in sedla med dvema sosednjima kapljicama v 4 smereh."""
    dolzina = max(5, int(dolzina) | 1)

    k_vodoravno = np.ones((1, dolzina), dtype=np.uint8)
    k_navpicno = np.ones((dolzina, 1), dtype=np.uint8)
    k_diag1 = np.eye(dolzina, dtype=np.uint8)
    k_diag2 = np.fliplr(k_diag1)

    bh_v = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_navpicno)
    bh_h = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_vodoravno)
    bh_d1 = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_diag1)
    bh_d2 = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_diag2)

    return np.maximum(np.maximum(bh_v, bh_h), np.maximum(bh_d1, bh_d2))


def analiziraj_kapljice(izoliran_rgba, standardna_dimenzija=True, min_velikost_px=1, obcutljivost=None):
    """
    Zazna velike, zelo majhne (nad 50 μm pri standardnih dimenzijah) in tanke podolgovate
    odtise po celotni površini lističa brez lažnega obarvanja rumenega prostora med kapljicami.
    Standardni način in nestandardni način pri občutljivosti 75 % uporabljata enake pragove.
    """
    if izoliran_rgba.shape[2] == 4:
        slika_rgb = cv2.cvtColor(izoliran_rgba, cv2.COLOR_RGBA2RGB)
        alpha_kanal = izoliran_rgba[:, :, 3]
    else:
        slika_rgb = izoliran_rgba
        alpha_kanal = np.full(slika_rgb.shape[:2], 255, dtype=np.uint8)

    h, w = slika_rgb.shape[:2]
    daljsa_stran_px = float(max(w, h))
    krajsa_stran_px = float(min(w, h))
    faktor_loc = float(np.clip(daljsa_stran_px / float(MIN_STANDARDNA_DOLZINA_PX), 1.0, 2.5))

    # 1. Tesna maska lističa (3 px erozije odreže skrajne robne piksle tudi pri ročnem izrezu)
    _, maska_surova = cv2.threshold(alpha_kanal, 160, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(maska_surova) < 100:
        _, maska_surova = cv2.threshold(alpha_kanal, 100, 255, cv2.THRESH_BINARY)

    konture_l, _ = cv2.findContours(maska_surova, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if konture_l:
        najvecja_l = max(konture_l, key=cv2.contourArea)
        maska_surova = np.zeros_like(maska_surova)
        cv2.drawContours(maska_surova, [najvecja_l], -1, 255, thickness=-1)

    eroz_rob = max(3, int(round(3.0 * faktor_loc)))
    okvir = np.zeros_like(maska_surova)
    if h > 2 * eroz_rob + 4 and w > 2 * eroz_rob + 4:
        okvir[eroz_rob : h - eroz_rob, eroz_rob : w - eroz_rob] = 255
    else:
        okvir[1 : h - 1, 1 : w - 1] = 255

    maska_listica = cv2.bitwise_and(
        cv2.erode(maska_surova, np.ones((3, 3), np.uint8), iterations=eroz_rob),
        okvir,
    )

    # Ozek (~0.2 mm) robni pas za preprečevanje lažnega obarvanja zelenega/senčnega roba
    rob_vel = max(5, int(round(9 * faktor_loc)) | 1)
    maska_notranjost = cv2.erode(maska_listica, np.ones((rob_vel, rob_vel), np.uint8), iterations=1)
    je_robni_pas = (maska_listica > 0) & (maska_notranjost == 0)

    veljavni_indeksi = maska_listica > 0
    skupni_piksli = float(np.sum(veljavni_indeksi))
    if skupni_piksli < 100:
        maska_listica = okvir
        maska_notranjost = okvir
        je_robni_pas = np.zeros_like(veljavni_indeksi)
        veljavni_indeksi = maska_listica > 0
        skupni_piksli = float(np.sum(veljavni_indeksi))

    # 2. Oster kanal (brez JPEG barvnega razmazanja) in barvni kanal rumenosti
    r = slika_rgb[:, :, 0].astype(np.float32)
    g = slika_rgb[:, :, 1].astype(np.float32)
    b = slika_rgb[:, :, 2].astype(np.float32)
    lab = cv2.cvtColor(slika_rgb, cv2.COLOR_RGB2LAB)
    a_lab = lab[:, :, 1].astype(np.float32)
    b_lab = lab[:, :, 2].astype(np.float32)

    oster_kanal = 0.75 * r + 0.25 * g
    barvni_kanal = 0.55 * b_lab + 0.45 * (r - b)

    # 3. Identifikacija čistega rumenega ozadja za lokalno referenco (na notranjosti)
    ref_maska = (maska_notranjost > 0) if np.sum(maska_notranjost > 0) > 100 else veljavni_indeksi
    b_veljavni = b_lab[ref_maska]
    oster_veljavni = oster_kanal[ref_maska]
    ima_barvni_kontrast = (
        (float(np.percentile(b_veljavni, 90)) > 136.0)
        or ((float(np.percentile(b_veljavni, 95)) - float(np.percentile(b_veljavni, 1))) > 8.0)
    )

    if ima_barvni_kontrast:
        p90_b = float(np.percentile(b_veljavni, 90))
        maska_cisto = ref_maska & (b_lab >= (p90_b - 18.0))
    else:
        p80_o = float(np.percentile(oster_veljavni, 75))
        maska_cisto = ref_maska & (oster_kanal >= p80_o)

    if np.sum(maska_cisto) < 0.05 * float(np.sum(ref_maska)):
        p75_b = float(np.percentile(barvni_kanal[ref_maska], 75))
        maska_cisto = ref_maska & (barvni_kanal >= p75_b)

    med_b_cisto = float(np.median(b_lab[maska_cisto])) if np.any(maska_cisto) else 165.0

    ozadje_oster = _oceni_lokalno_ozadje(oster_kanal, maska_cisto, ref_maska)
    ozadje_barva = _oceni_lokalno_ozadje(barvni_kanal, maska_cisto, ref_maska)

    padec_oster = np.clip(ozadje_oster - oster_kanal, 0.0, 255.0)
    padec_barva = np.clip(ozadje_barva - barvni_kanal, 0.0, 255.0)

    # Izničimo signal zunaj maske lističa, da ozadje ne zaduši kapljic ob robu
    padec_oster[~veljavni_indeksi] = 0.0
    padec_barva[~veljavni_indeksi] = 0.0

    # Omejitev barvnega prispevka z ostrim kanalom prepreči JPEG prelivanje barve v vmesne prostore
    signal = 0.65 * padec_oster + 0.35 * np.minimum(padec_barva, padec_oster * 1.65)
    signal[~veljavni_indeksi] = 0.0

    # 4. Robustna ocena šuma ozadja (MAD za svetlostni in barvni kanal na čistih rumenih pikslih)
    visokofrekvencni_sum = np.abs(padec_oster - cv2.GaussianBlur(padec_oster, (3, 3), 0))
    visokofrekvencni_sum_barva = np.abs(padec_barva - cv2.GaussianBlur(padec_barva, (3, 3), 0))
    p70_sig = float(np.percentile(signal[ref_maska], 70))
    ozadje_za_sum = ref_maska & (signal <= max(p70_sig, 8.0))
    if np.sum(ozadje_za_sum) > 50:
        sigma_sum = max(1.0, 1.4826 * float(np.median(visokofrekvencni_sum[ozadje_za_sum])))
        sigma_barva = max(1.0, 1.4826 * float(np.median(visokofrekvencni_sum_barva[ozadje_za_sum])))
    else:
        sigma_sum = 2.0
        sigma_barva = 2.0

    skala = float(np.clip(np.median(ozadje_oster[ref_maska]) / 200.0, 0.45, 1.25))

    # Enotna kalibracija za oba načina: pri občutljivosti 75 % so pragovi identični standardnemu načinu
    obc_efektivna = (
        PRIVZETA_OBCUTLJIVOST_STANDARD
        if (standardna_dimenzija or obcutljivost is None)
        else float(np.clip(obcutljivost, 1.0, 100.0))
    )
    if obc_efektivna >= PRIVZETA_OBCUTLJIVOST_STANDARD:
        delez = (obc_efektivna - PRIVZETA_OBCUTLJIVOST_STANDARD) / (100.0 - PRIVZETA_OBCUTLJIVOST_STANDARD)
        f_obc = 1.0 - 0.45 * delez
        f_sum = 1.0 - 0.25 * delez
    else:
        delez = (PRIVZETA_OBCUTLJIVOST_STANDARD - obc_efektivna) / (PRIVZETA_OBCUTLJIVOST_STANDARD - 1.0)
        f_obc = 1.0 + 1.80 * delez
        f_sum = 1.0 + 1.20 * delez

    prag_jedro = max(3.6 * f_sum * sigma_sum, 17.5 * f_obc * skala)
    prag_drobni = max(2.4 * f_sum * sigma_sum, 7.5 * f_obc * skala)
    prag_dog = max(1.8 * f_sum * sigma_sum, 4.2 * f_obc * skala)
    prag_podolgovati = max(2.1 * f_sum * sigma_sum, 5.5 * f_obc * skala)
    prag_barva_komp = max(1.6 * f_sum * sigma_barva, 4.0 * f_obc * skala)
    prag_barva_drobni = max(1.25 * f_sum * sigma_barva, 2.8 * f_obc * skala)

    # Samo dejansko modro/temno jedro kapljice (nikoli zasenčen rumeni prostor med kapljicami)
    prag_absolutno_moder = max(prag_jedro * 3.0, 95.0 * skala)
    prag_b_moder = min(134.0, med_b_cisto - 25.0)
    if ima_barvni_kontrast:
        je_absolutno_moder = (
            (signal >= prag_absolutno_moder)
            & (padec_barva >= prag_absolutno_moder * 0.70)
            & (b_lab <= prag_b_moder)
        )
    else:
        je_absolutno_moder = signal >= prag_absolutno_moder

    # 5. Anti-halo večstopenjska detekcija brez prelivanja v vmesne doline in sedla
    okolica_vel = max(11, int(round(13 * faktor_loc)) | 1)
    jedro_okolica = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (okolica_vel, okolica_vel))
    lokalni_vrh = cv2.dilate(signal, jedro_okolica)
    gladko_ozko = cv2.GaussianBlur(signal, (0, 0), sigmaX=1.1 * faktor_loc)
    gladko_rob = cv2.GaussianBlur(signal, (0, 0), sigmaX=1.3 * faktor_loc)

    # Rob na 56 % lokalnega maksimuma odreže zunanjo avro in prepreči mostičke med kapljicami
    ni_zunanji_halo = (signal >= 0.56 * lokalni_vrh) | je_absolutno_moder
    maska_jedra = np.where((signal >= prag_jedro) & ni_zunanji_halo, 255, 0).astype(np.uint8)

    # Drobni odtisi morajo biti pravi lokalni vrhovi (signal > gladko_ozko)
    dog_drobni = signal - cv2.GaussianBlur(signal, (0, 0), sigmaX=1.4 * faktor_loc)
    dog_srednji = signal - cv2.GaussianBlur(signal, (0, 0), sigmaX=3.0 * faktor_loc)
    maska_drobna = np.where(
        (signal >= prag_drobni)
        & (signal >= gladko_ozko * 1.01)
        & ((dog_drobni >= prag_dog) | (dog_srednji >= (prag_dog * 1.45))),
        255,
        0,
    ).astype(np.uint8)

    # Podolgovati tanki odtisi
    smerni_odziv = _smerni_tophat_za_podolgovate(signal, dolzina=int(round(9 * faktor_loc)) | 1)
    maska_podolgovata = np.where(
        (smerni_odziv >= prag_podolgovati)
        & (signal >= prag_drobni)
        & (signal >= gladko_rob * 1.00),
        255,
        0,
    ).astype(np.uint8)

    surova_maska = cv2.bitwise_or(maska_jedra, maska_drobna)
    surova_maska = cv2.bitwise_or(surova_maska, maska_podolgovata)
    surova_maska = cv2.bitwise_and(surova_maska, maska_listica)

    # Ciljno čiščenje rumenih vmesnih dolin in sedel med dvema bližnjima kapljicama
    dolina_vel = max(11, int(round(13 * faktor_loc)) | 1)
    dolina_odziv = _smerni_blackhat_za_doline(signal, dolzina=dolina_vel)
    je_oster_vrh = (dog_drobni >= prag_dog * 1.35) & (signal >= gladko_ozko * 1.04)
    je_vmesna_dolina = (
        (~je_absolutno_moder)
        & (~je_oster_vrh)
        & (dolina_odziv >= np.maximum(prag_dog * 2.0, signal * 0.35))
    )
    surova_maska[je_vmesna_dolina] = 0

    # Nežna zaščita skrajnega robnega pasu pred zeleno travo, rumeno senco roba in temnimi črtami
    if ima_barvni_kontrast and np.any(je_robni_pas):
        med_a_cisto = float(np.median(a_lab[maska_cisto])) if np.any(maska_cisto) else 124.0
        je_zelena_trava = (a_lab < (med_a_cisto - 6.0)) & (g > (r + 5.0)) & (g > (b + 10.0))
        je_rumena_senca = (padec_barva < np.maximum(prag_barva_komp * 1.4, 0.35 * padec_oster))
        je_temna_nevtralna_crta = (
            (r < 55.0) & (g < 55.0) & (b < 60.0) & ((b - r) < 10.0) & ((b - g) < 8.0)
        )
        surova_maska[je_robni_pas & (je_zelena_trava | je_rumena_senca | je_temna_nevtralna_crta)] = 0

    # Zapolnimo samo drobne modre/temne odbleske znotraj kapljic, nikoli rumenih žepkov med njimi
    max_luknja = 8.0 * (faktor_loc ** 2)
    konture, hierarhija = cv2.findContours(surova_maska, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarhija is not None:
        for idx, kontura in enumerate(konture):
            if hierarhija[0][idx][3] != -1 and cv2.contourArea(kontura) <= max_luknja:
                x_l, y_l, w_l, h_l = cv2.boundingRect(kontura)
                luknja_m = np.zeros((h_l, w_l), dtype=np.uint8)
                pomaknjena = kontura - np.array([[[x_l, y_l]]], dtype=kontura.dtype)
                cv2.drawContours(luknja_m, [pomaknjena], -1, 255, thickness=-1)
                luknja_sig = signal[y_l : y_l + h_l, x_l : x_l + w_l][luknja_m > 0]
                luknja_b = b_lab[y_l : y_l + h_l, x_l : x_l + w_l][luknja_m > 0]
                je_temna_luknja = (
                    luknja_sig.size > 0
                    and float(np.median(luknja_sig)) >= (prag_jedro * 2.0)
                    and ((not ima_barvni_kontrast) or float(np.median(luknja_b)) <= (prag_b_moder + 3.0))
                )
                if je_temna_luknja:
                    cv2.drawContours(surova_maska, konture, idx, 255, thickness=-1)

    # 6. Filtriranje po velikosti (natančen prag 50 μm za standardni listič) in barvna potrditev
    if standardna_dimenzija:
        px_na_mm_x = daljsa_stran_px / STANDARDNA_DOLZINA_MM
        px_na_mm_y = krajsa_stran_px / STANDARDNA_SIRINA_MM
        premer_mm = MIN_PREMER_STANDARD_UM / 1000.0
        teoreticna_povrsina_px = math.pi * ((premer_mm / 2.0) ** 2) * px_na_mm_x * px_na_mm_y
        # Faktor 0.85 upošteva diskretizacijo roba 50 μm kapljice na mreži pikslov
        min_povrsina_px = max(1.0, round(teoreticna_povrsina_px * 0.85, 2))
    else:
        min_povrsina_px = float(min_velikost_px)

    prag_vecji_komp = int(round(12.0 * (faktor_loc ** 2)))
    st_oznak, oznake, statistika, _ = cv2.connectedComponentsWithStats(surova_maska, connectivity=8)

    filtrirana_maska = np.zeros((h, w), dtype=np.uint8)
    stevilo_odtisov = 0
    povrsina_kapljic_px = 0

    for i in range(1, st_oznak):
        povrsina = statistika[i, cv2.CC_STAT_AREA]
        x0 = statistika[i, cv2.CC_STAT_LEFT]
        y0 = statistika[i, cv2.CC_STAT_TOP]
        sirina_komp = statistika[i, cv2.CC_STAT_WIDTH]
        visina_komp = statistika[i, cv2.CC_STAT_HEIGHT]

        daljsa_k = max(sirina_komp, visina_komp)
        krajsa_k = max(1, min(sirina_komp, visina_komp))
        je_podolgovat = (daljsa_k >= 3) and ((daljsa_k / float(krajsa_k)) >= 2.2)

        if not (
            povrsina >= min_povrsina_px
            or (je_podolgovat and povrsina >= max(1.0, min_povrsina_px * 0.65))
        ):
            continue

        komp_maska = oznake[y0 : y0 + visina_komp, x0 : x0 + sirina_komp] == i

        # Zavrnemo samo izrazito dolge tanke črte, ki tečejo izključno po zunanjem robu
        delez_na_robu = float(
            np.mean(je_robni_pas[y0 : y0 + visina_komp, x0 : x0 + sirina_komp][komp_maska])
        )
        if (
            delez_na_robu > 0.88
            and daljsa_k > (35.0 * faktor_loc)
            and (povrsina / float(daljsa_k)) <= (3.8 * faktor_loc)
        ):
            continue

        if ima_barvni_kontrast:
            barva_izsek = padec_barva[y0 : y0 + visina_komp, x0 : x0 + sirina_komp]
            if povrsina >= prag_vecji_komp:
                if float(np.mean(barva_izsek[komp_maska])) < prag_barva_komp:
                    continue
            else:
                # Drobni 50–150 μm odtisi: potrdi modri odtenek (tudi ob JPEG podvzorčenju) ali močan temen odtis
                y1_o = max(0, y0 - 1)
                y2_o = min(h, y0 + visina_komp + 1)
                x1_o = max(0, x0 - 1)
                x2_o = min(w, x0 + sirina_komp + 1)
                max_barva_okolica = float(np.max(padec_barva[y1_o:y2_o, x1_o:x2_o]))
                max_oster = float(np.max(padec_oster[y0 : y0 + visina_komp, x0 : x0 + sirina_komp][komp_maska]))
                if max_barva_okolica < prag_barva_drobni and max_oster < (prag_jedro * 1.25):
                    continue

        filtrirana_maska[y0 : y0 + visina_komp, x0 : x0 + sirina_komp][komp_maska] = 255
        stevilo_odtisov += 1
        povrsina_kapljic_px += povrsina

    pokritost_proc = round((povrsina_kapljic_px / skupni_piksli) * 100.0, 2)
    odtisi_na_cm2 = round(stevilo_odtisov / STANDARDNA_POVRSINA_CM2, 2) if standardna_dimenzija else None

    return filtrirana_maska, stevilo_odtisov, odtisi_na_cm2, pokritost_proc


def obarvaj_kapljice_rdece(izoliran_rgba, maska, prosojnost_proc):
    prosojnost_rdece = np.clip(prosojnost_proc / 100.0, 0.0, 1.0)
    rezultat = izoliran_rgba.copy().astype(np.float32)

    if rezultat.shape[2] == 4:
        rdeca_barva = np.array([255.0, 0.0, 0.0, 255.0], dtype=np.float32)
    else:
        rdeca_barva = np.array([255.0, 0.0, 0.0], dtype=np.float32)

    idx = maska > 0
    rezultat[idx] = rezultat[idx] * (1.0 - prosojnost_rdece) + rdeca_barva * prosojnost_rdece
    return rezultat.astype(np.uint8)