import base64
import hashlib
import io
import math
import os
import cv2
import numpy as np
from PIL import Image, ImageOps
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

# Usklajena zgornja meja predpomnilnika za varno porabo pomnilnika RAM
AVTO_IZREZ_CACHE_MAX = 5


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


@st.cache_data(max_entries=6, show_spinner=False)
def nalozi_sliko_rgb(kljuc_slike, _surovi_bajti):
    """Odpre sliko (tudi 16-bitni TIFF), popravi orientacijo EXIF in vrne 8-bitno polje RGB."""
    with Image.open(io.BytesIO(_surovi_bajti)) as img:
        pil_slika = ImageOps.exif_transpose(img)
        if pil_slika.mode.startswith("I;16") or pil_slika.mode == "I":
            arr = np.array(pil_slika, dtype=np.float32)
            max_val = float(arr.max())
            if max_val > 255.0:
                arr = np.clip(arr * (255.0 / max_val), 0.0, 255.0)
            pil_slika = Image.fromarray(arr.astype(np.uint8)).convert("RGB")
        else:
            pil_slika = pil_slika.convert("RGB")
        return np.array(pil_slika)


def pridobi_izoliran_rgba(slika_rgb):
    """Izolacija ozadja z nevronsko mrežo rembg (u2net) brez nepotrebnega kodiranja v PNG."""
    seja, napaka = _pridobi_rembg_sejo()
    if seja is not None and napaka is None:
        from rembg import remove
        slika_rgba = remove(Image.fromarray(slika_rgb), session=seja)
        return np.array(slika_rgba)

    polni_alpha = np.full((slika_rgb.shape[0], slika_rgb.shape[1], 1), 255, dtype=np.uint8)
    return np.concatenate([slika_rgb, polni_alpha], axis=2)


# =====================================================================
# INTERAKTIVNA KOMPONENTA ZA ROČNI IZREZ (PRILAGOJENA TELEFONU + LUPA NA ROBU)
# =====================================================================

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

  ctx.save();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  ctx.moveTo(px, py);
  ctx.lineTo(lx, ly);
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.shadowColor = "rgba(0, 0, 0, 0.60)";
  ctx.shadowBlur = 12;
  ctx.shadowOffsetY = 3;
  ctx.beginPath();
  ctx.arc(lx, ly, R, 0, Math.PI * 2);
  ctx.fillStyle = "#111";
  ctx.fill();
  ctx.restore();

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

  ctx.strokeStyle = "#ff1744";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(lx - 13, ly);
  ctx.lineTo(lx + 13, ly);
  ctx.moveTo(lx, ly - 13);
  ctx.lineTo(lx, ly + 13);
  ctx.stroke();

  ctx.restore();

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


@st.cache_resource
def _pridobi_komponento_izreza():
    komponenta_mapa = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_izrez_komponenta_v2")
    os.makedirs(komponenta_mapa, exist_ok=True)
    index_pot = os.path.join(komponenta_mapa, "index.html")
    obstojece = ""
    if os.path.exists(index_pot):
        with open(index_pot, "r", encoding="utf-8") as f:
            obstojece = f.read()
    if obstojece != HTML_KODA_IZREZA:
        with open(index_pot, "w", encoding="utf-8") as f:
            f.write(HTML_KODA_IZREZA)
    return components.declare_component("stirikotnik_izrez_v2", path=komponenta_mapa)


# =====================================================================
# OBREZOVANJE LISTIČA (DEJANSKA KONTURA + ODSTRANJEVANJE SENCE + MIN. ODREZ)
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


def _odstrani_senco_po_konturi(slika_rgb, alpha_kanal, zacetna_maska, pts1, max_sirina, max_visina):
    """
    Odstrani zunanjo kontaktno in sivo senco neposredno po dejanski konturi lističa,
    zapolni morebitne zareze od temnih robnih kapljic in vrne očiščeno masko v koordinatah izvirne slike.
    """
    if max_sirina < 40 or max_visina < 40:
        return zacetna_maska

    pts2 = np.float32([
        [0, 0],
        [max_sirina - 1, 0],
        [max_sirina - 1, max_visina - 1],
        [0, max_visina - 1],
    ])
    matrika = cv2.getPerspectiveTransform(pts1, pts2)

    poravnan_rgb = cv2.warpPerspective(
        slika_rgb,
        matrika,
        (max_sirina, max_visina),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    poravnan_alpha = cv2.warpPerspective(
        alpha_kanal,
        matrika,
        (max_sirina, max_visina),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    poravnan_maska = cv2.warpPerspective(
        zacetna_maska,
        matrika,
        (max_sirina, max_visina),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    dist_od_roba = cv2.distanceTransform((poravnan_maska > 127).astype(np.uint8), cv2.DIST_L2, 5)
    max_dist = float(dist_od_roba.max())
    if max_dist < 8.0:
        return zacetna_maska

    lab = cv2.cvtColor(poravnan_rgb, cv2.COLOR_RGB2LAB)
    hsv = cv2.cvtColor(poravnan_rgb, cv2.COLOR_RGB2HSV)

    L = lab[:, :, 0].astype(np.float32)
    a_lab = lab[:, :, 1].astype(np.float32)
    b_lab = lab[:, :, 2].astype(np.float32)
    S = hsv[:, :, 1].astype(np.float32)

    R = poravnan_rgb[:, :, 0].astype(np.float32)
    B = poravnan_rgb[:, :, 2].astype(np.float32)

    # Notranje jedro za referenčno barvo rumenega papirja (varno oddaljeno od robnih senc)
    notranjost = dist_od_roba >= (0.35 * max_dist)
    if np.sum(notranjost) < 50:
        notranjost = poravnan_maska > 127
    if np.sum(notranjost) < 50:
        return zacetna_maska

    # Varnostno preverjanje: če slika ni rumeni listič, maske ne spreminjamo
    p85_b = float(np.percentile(b_lab[notranjost], 85))
    if p85_b < 136.0:
        return zacetna_maska

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

    # Luščimo SAMO zunanjo senco, povezano z zunanjostjo lističa in znotraj 16 % krajše stranice
    max_odrez = max(6.0, 0.16 * float(min(max_sirina, max_visina)))
    senca_kandidat = ((poravnan_maska < 128) | (je_senca & (dist_od_roba <= max_odrez))).astype(np.uint8)
    senca_pad = cv2.copyMakeBorder(senca_kandidat, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
    _, oznake_s = cv2.connectedComponents(senca_pad, connectivity=4)
    zunanja_senca = oznake_s[1:-1, 1:-1] == oznake_s[0, 0]

    cista_warp = np.where(~zunanja_senca, 255, 0).astype(np.uint8)

    # Morfološko zapiranje z obrobo zapolni vbočene zareze od robnih kapljic, ne da bi vrnilo senco
    k_glad = max(15, min(61, int(round(min(max_sirina, max_visina) * 0.14)) | 1))
    jedro_glad = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_glad, k_glad))
    cista_pad = cv2.copyMakeBorder(
        cista_warp, k_glad, k_glad, k_glad, k_glad, cv2.BORDER_CONSTANT, value=0
    )
    cista_pad = cv2.morphologyEx(cista_pad, cv2.MORPH_CLOSE, jedro_glad)
    cista_warp = cista_pad[k_glad:-k_glad, k_glad:-k_glad]

    konture_c, _ = cv2.findContours(cista_warp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not konture_c:
        return zacetna_maska

    najvecja_c = max(konture_c, key=cv2.contourArea)
    cista_warp = np.zeros_like(cista_warp)
    cv2.drawContours(cista_warp, [najvecja_c], -1, 255, thickness=-1)

    if cv2.countNonZero(cista_warp) < 0.65 * max(1, cv2.countNonZero(poravnan_maska)):
        return zacetna_maska

    try:
        matrika_inv = np.linalg.inv(matrika)
    except np.linalg.LinAlgError:
        return zacetna_maska

    cista_orig = cv2.warpPerspective(
        cista_warp,
        matrika_inv,
        (w_orig, h_orig),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    _, cista_orig = cv2.threshold(cista_orig, 127, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(cista_orig) < 100:
        return zacetna_maska
    return cista_orig


def poisci_in_izrezi_avtomatsko(slika_rgb):
    """
    Izolira listič po njegovi dejanski konturi z rembg (u2net), po obodu odstrani zunanjo
    senco ter celotno konturo minimalno skrči navznoter (0,8 % krajše stranice ≈ 0,18 mm).
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

    pravokotnik_z = cv2.minAreaRect(najvecja_kontura)
    pts1_zacetna = uredi_oglisca(cv2.boxPoints(pravokotnik_z))
    (zl, zd, sd, sl) = pts1_zacetna

    max_sirina_z = max(int(np.linalg.norm(sd - sl)), int(np.linalg.norm(zd - zl)), 50)
    max_visina_z = max(int(np.linalg.norm(zd - sd)), int(np.linalg.norm(zl - sl)), 50)

    # 1. Odstranimo zunanjo senco neposredno po dejanski konturi lističa
    maska_brez_sence = _odstrani_senco_po_konturi(
        slika_rgb, alpha_kanal, zacetna_maska, pts1_zacetna, max_sirina_z, max_visina_z
    )

    # 2. Minimalen enakomeren notranji odrez vzdolž dejanske konture (0,8 % krajše stranice)
    krajsa_z = float(min(max_sirina_z, max_visina_z))
    odmik_px = min(max(1.5, krajsa_z * 0.008), krajsa_z * 0.02)
    dist_maska = cv2.distanceTransform(maska_brez_sence, cv2.DIST_L2, 5)
    dejanska_maska = np.where(dist_maska > odmik_px, 255, 0).astype(np.uint8)

    konture_d, _ = cv2.findContours(dejanska_maska, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if konture_d:
        najvecja_koncna = max(konture_d, key=cv2.contourArea)
        dejanska_maska = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(dejanska_maska, [najvecja_koncna], -1, 255, thickness=-1)
    else:
        dejanska_maska = maska_brez_sence
        konture_f, _ = cv2.findContours(dejanska_maska, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        najvecja_koncna = max(konture_f, key=cv2.contourArea) if konture_f else najvecja_kontura

    # Tesen poravnalni okvir okoli dejanske konture (ohrani ukrivljenost prek alpha kanala)
    pravokotnik = cv2.minAreaRect(najvecja_koncna)
    pts1 = uredi_oglisca(cv2.boxPoints(pravokotnik))
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

    # Barve beremo iz čiste izvirne RGB slike (brez črnega ozadja rembg), obliko pa iz dejanske konture
    poravnan_rgb = cv2.warpPerspective(
        slika_rgb,
        matrika,
        (max_sirina, max_visina),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    poravnan_alpha = cv2.warpPerspective(
        dejanska_maska,
        matrika,
        (max_sirina, max_visina),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    _, poravnan_alpha = cv2.threshold(poravnan_alpha, 127, 255, cv2.THRESH_BINARY)
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


@st.cache_data(max_entries=AVTO_IZREZ_CACHE_MAX, show_spinner=False)
def poisci_in_izrezi_avtomatsko_predpomnjeno(kljuc_slike, _slika_rgb):
    """Predpomnjen ovoj okoli avtomatskega izreza (prepreči ponavljanje ob premiku drsnikov)."""
    return poisci_in_izrezi_avtomatsko(_slika_rgb)


def izrezi_listic_rocno(slika_rgb, norm_oglisca):
    """Perspektivni izrez za ročni način iz 4 ročno nastavljenih oglišč."""
    h, w = slika_rgb.shape[:2]
    tocke = np.array([[float(pt[0]) * w, float(pt[1]) * h] for pt in norm_oglisca], dtype=np.float32)
    pts1 = uredi_oglisca(tocke)

    # Varnost pred popolnoma prekrivajočimi se ali kolinearnimi oglišči
    if cv2.contourArea(pts1) < 16.0:
        pts1 = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])

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
    try:
        matrika = cv2.getPerspectiveTransform(pts1, pts2)
        poravnan_rgb = cv2.warpPerspective(
            slika_rgb, matrika, (max_sirina, max_visina), flags=cv2.INTER_LINEAR
        )
    except cv2.error:
        poravnan_rgb = slika_rgb.copy()
        max_visina, max_sirina = poravnan_rgb.shape[:2]

    polni_alpha = np.full((max_visina, max_sirina, 1), 255, dtype=np.uint8)
    poravnan_listic = np.concatenate([poravnan_rgb, polni_alpha], axis=2)

    if poravnan_listic.shape[0] > poravnan_listic.shape[1]:
        poravnan_listic = cv2.rotate(poravnan_listic, cv2.ROTATE_90_CLOCKWISE)

    return _prilagodi_locljivost_listica(poravnan_listic)


@st.cache_data(max_entries=6, show_spinner=False)
def izrezi_listic_rocno_predpomnjeno(kljuc_slike, oglisca_kljuc, _slika_rgb):
    """Predpomnjen ovoj za ročni izrez."""
    return izrezi_listic_rocno(_slika_rgb, oglisca_kljuc)


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


@st.cache_data(max_entries=5, show_spinner=False)
def narisi_predogled_izreza_predpomnjeno(kljuc_slike, _slika_rgb, _norm_oglisca, _dejanska_maska):
    """Predpomnjen ovoj za risanje predogleda avtomatskega izreza."""
    return narisi_predogled_izreza(_slika_rgb, _norm_oglisca, dejanska_maska=_dejanska_maska)


@st.cache_data(max_entries=8, show_spinner=False)
def _pripravi_predogled_b64(kljuc_slike, _slika_rgb):
    h, w = _slika_rgb.shape[:2]
    max_dim = 900
    merilo = min(1.0, max_dim / max(w, h))
    predogled = (
        cv2.resize(_slika_rgb, (int(w * merilo), int(h * merilo)), interpolation=cv2.INTER_AREA)
        if merilo < 1.0
        else _slika_rgb
    )

    pil_img = Image.fromarray(predogled)
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def prikazi_rocni_izrez(slika_rgb, zacetna_oglisca, kljuc, reset_version=0, kljuc_slike=None):
    if kljuc_slike is None:
        kljuc_slike = hashlib.md5(slika_rgb.tobytes()).hexdigest()

    b64_str = _pripravi_predogled_b64(kljuc_slike, slika_rgb)
    komponenta_izrez = _pridobi_komponento_izreza()

    rezultat = komponenta_izrez(
        image_b64=b64_str,
        initial_corners=zacetna_oglisca,
        reset_version=int(reset_version),
        key=f"{kljuc}_v{int(reset_version)}",
    )
    if isinstance(rezultat, list) and len(rezultat) == 4:
        return rezultat
    return zacetna_oglisca


# =====================================================================
# ANALIZA KAPLJIC (POLNA ZAZNAVA VSE DO DEJANSKE KONTURE LISTIČA)
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


def _razsiri_rob_signala(signal, veljavni_indeksi, sigma_razsiritev=4.0):
    """
    Zunanje neveljavne piksle okoli ukrivljene konture gladko zapolni s sosednjimi
    vrednostmi signala, da na robu konture ne nastane umeten skok na 0.
    """
    if np.all(veljavni_indeksi):
        return signal
    sig = signal.copy()
    sig[~veljavni_indeksi] = 0.0
    utez = veljavni_indeksi.astype(np.float32)
    stevec = cv2.GaussianBlur(sig, (0, 0), sigmaX=sigma_razsiritev)
    imenov = cv2.GaussianBlur(utez, (0, 0), sigmaX=sigma_razsiritev)
    zunaj_blizu = (~veljavni_indeksi) & (imenov > 1e-4)
    sig[zunaj_blizu] = stevec[zunaj_blizu] / imenov[zunaj_blizu]
    return sig


def _smerni_tophat_za_podolgovate(signal, dolzina=9):
    """Poudari tanke podolgovate odtise v 4 smereh brez morfološkega širjenja robov."""
    dolzina = max(5, int(dolzina) | 1)

    k_vodoravno = np.ones((1, dolzina), dtype=np.uint8)
    k_navpicno = np.ones((dolzina, 1), dtype=np.uint8)
    k_diag1 = np.eye(dolzina, dtype=np.uint8)
    k_diag2 = np.fliplr(k_diag1)

    th_v = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_navpicno, borderType=cv2.BORDER_REPLICATE)
    th_h = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_vodoravno, borderType=cv2.BORDER_REPLICATE)
    th_d1 = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_diag1, borderType=cv2.BORDER_REPLICATE)
    th_d2 = cv2.morphologyEx(signal, cv2.MORPH_TOPHAT, k_diag2, borderType=cv2.BORDER_REPLICATE)

    return np.maximum(np.maximum(th_v, th_h), np.maximum(th_d1, th_d2))


def _smerni_blackhat_za_doline(signal, dolzina=13):
    """Zazna ozke rumene doline in sedla med dvema sosednjima kapljicama v 4 smereh."""
    dolzina = max(5, int(dolzina) | 1)

    k_vodoravno = np.ones((1, dolzina), dtype=np.uint8)
    k_navpicno = np.ones((dolzina, 1), dtype=np.uint8)
    k_diag1 = np.eye(dolzina, dtype=np.uint8)
    k_diag2 = np.fliplr(k_diag1)

    bh_v = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_navpicno, borderType=cv2.BORDER_REPLICATE)
    bh_h = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_vodoravno, borderType=cv2.BORDER_REPLICATE)
    bh_d1 = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_diag1, borderType=cv2.BORDER_REPLICATE)
    bh_d2 = cv2.morphologyEx(signal, cv2.MORPH_BLACKHAT, k_diag2, borderType=cv2.BORDER_REPLICATE)

    return np.maximum(np.maximum(bh_v, bh_h), np.maximum(bh_d1, bh_d2))


def analiziraj_kapljice(izoliran_rgba, standardna_dimenzija=True, min_velikost_px=1, obcutljivost=None):
    """
    Zazna velike, zelo majhne (nad 50 μm pri standardnih dimenzijah) in tanke podolgovate
    odtise po celotni površini izrezanega lističa vse do zadnjega roba dejanske konture.
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

    # 1. Polna maska dejanske konture izrezanega lističa (brez dodatnega izključevanja roba)
    _, maska_listica = cv2.threshold(alpha_kanal, 128, 255, cv2.THRESH_BINARY)
    if cv2.countNonZero(maska_listica) < 100:
        _, maska_listica = cv2.threshold(alpha_kanal, 64, 255, cv2.THRESH_BINARY)

    konture_l, _ = cv2.findContours(maska_listica, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if konture_l:
        najvecja_l = max(konture_l, key=cv2.contourArea)
        maska_listica = np.zeros_like(maska_listica)
        cv2.drawContours(maska_listica, [najvecja_l], -1, 255, thickness=-1)

    veljavni_indeksi = maska_listica > 0
    skupni_piksli = float(np.sum(veljavni_indeksi))
    if skupni_piksli < 100:
        maska_listica = np.full((h, w), 255, dtype=np.uint8)
        veljavni_indeksi = maska_listica > 0
        skupni_piksli = float(np.sum(veljavni_indeksi))

    # 2. Oster kanal (brez JPEG barvnega razmazanja) in barvni kanal rumenosti
    r = slika_rgb[:, :, 0].astype(np.float32)
    g = slika_rgb[:, :, 1].astype(np.float32)
    b = slika_rgb[:, :, 2].astype(np.float32)
    lab = cv2.cvtColor(slika_rgb, cv2.COLOR_RGB2LAB)
    b_lab = lab[:, :, 2].astype(np.float32)

    oster_kanal = 0.75 * r + 0.25 * g
    barvni_kanal = 0.55 * b_lab + 0.45 * (r - b)

    # 3. Identifikacija čistega rumenega ozadja za lokalno referenco
    ref_maska = veljavni_indeksi
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

    padec_oster[~veljavni_indeksi] = 0.0
    padec_barva[~veljavni_indeksi] = 0.0

    # Omejitev barvnega prispevka z ostrim kanalom prepreči JPEG prelivanje barve v vmesne prostore
    signal = 0.65 * padec_oster + 0.35 * np.minimum(padec_barva, padec_oster * 1.65)
    signal[~veljavni_indeksi] = 0.0

    # Gladko podaljšanje signala čez rob konture prepreči umetne robne artefakte pri filtrih
    signal_pad = _razsiri_rob_signala(signal, veljavni_indeksi, sigma_razsiritev=4.0 * faktor_loc)

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
    lokalni_vrh = cv2.dilate(signal_pad, jedro_okolica, borderType=cv2.BORDER_REPLICATE)
    gladko_ozko = cv2.GaussianBlur(signal_pad, (0, 0), sigmaX=1.1 * faktor_loc)
    gladko_rob = cv2.GaussianBlur(signal_pad, (0, 0), sigmaX=1.3 * faktor_loc)

    # Rob na 56 % lokalnega maksimuma odreže zunanjo avro in prepreči mostičke med kapljicami
    ni_zunanji_halo = (signal >= 0.56 * lokalni_vrh) | je_absolutno_moder
    maska_jedra = np.where((signal >= prag_jedro) & ni_zunanji_halo, 255, 0).astype(np.uint8)

    # Drobni odtisi morajo biti pravi lokalni vrhovi (signal > gladko_ozko)
    dog_drobni = signal_pad - cv2.GaussianBlur(signal_pad, (0, 0), sigmaX=1.4 * faktor_loc)
    dog_srednji = signal_pad - cv2.GaussianBlur(signal_pad, (0, 0), sigmaX=3.0 * faktor_loc)
    maska_drobna = np.where(
        (signal >= prag_drobni)
        & (signal >= gladko_ozko * 1.01)
        & ((dog_drobni >= prag_dog) | (dog_srednji >= (prag_dog * 1.45))),
        255,
        0,
    ).astype(np.uint8)

    # Podolgovati tanki odtisi
    smerni_odziv = _smerni_tophat_za_podolgovate(signal_pad, dolzina=int(round(9 * faktor_loc)) | 1)
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
    dolina_odziv = _smerni_blackhat_za_doline(signal_pad, dolzina=dolina_vel)
    je_oster_vrh = (dog_drobni >= prag_dog * 1.35) & (signal >= gladko_ozko * 1.04)
    je_vmesna_dolina = (
        (~je_absolutno_moder)
        & (~je_oster_vrh)
        & (dolina_odziv >= np.maximum(prag_dog * 2.0, signal * 0.35))
    )
    surova_maska[je_vmesna_dolina] = 0

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

    veljavne_oznake = np.zeros(st_oznak, dtype=bool)
    stevilo_odtisov = 0
    povrsina_kapljic_px = 0

    for i in range(1, st_oznak):
        povrsina = int(statistika[i, cv2.CC_STAT_AREA])
        x0 = int(statistika[i, cv2.CC_STAT_LEFT])
        y0 = int(statistika[i, cv2.CC_STAT_TOP])
        sirina_komp = int(statistika[i, cv2.CC_STAT_WIDTH])
        visina_komp = int(statistika[i, cv2.CC_STAT_HEIGHT])

        daljsa_k = max(sirina_komp, visina_komp)
        krajsa_k = max(1, min(sirina_komp, visina_komp))
        je_podolgovat = (daljsa_k >= 3) and ((daljsa_k / float(krajsa_k)) >= 2.2)

        if not (
            povrsina >= min_povrsina_px
            or (je_podolgovat and povrsina >= max(1.0, min_povrsina_px * 0.65))
        ):
            continue

        if ima_barvni_kontrast:
            komp_maska = oznake[y0 : y0 + visina_komp, x0 : x0 + sirina_komp] == i
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

        veljavne_oznake[i] = True
        stevilo_odtisov += 1
        povrsina_kapljic_px += povrsina

    filtrirana_maska = np.where(veljavne_oznake[oznake], 255, 0).astype(np.uint8)
    pokritost_proc = round((povrsina_kapljic_px / skupni_piksli) * 100.0, 2)
    odtisi_na_cm2 = round(stevilo_odtisov / STANDARDNA_POVRSINA_CM2, 2) if standardna_dimenzija else None

    return filtrirana_maska, stevilo_odtisov, odtisi_na_cm2, pokritost_proc


@st.cache_data(max_entries=8, show_spinner=False)
def analiziraj_kapljice_predpomnjeno(
    kljuc_izreza, _izoliran_rgba, standardna_dimenzija=True, min_velikost_px=1, obcutljivost=None
):
    """Predpomnjen ovoj za analizo kapljic."""
    return analiziraj_kapljice(
        _izoliran_rgba,
        standardna_dimenzija=standardna_dimenzija,
        min_velikost_px=min_velikost_px,
        obcutljivost=obcutljivost,
    )


def obarvaj_kapljice_rdece(izoliran_rgba, maska, prosojnost_proc):
    prosojnost_rdece = float(np.clip(prosojnost_proc / 100.0, 0.0, 1.0))
    rezultat = izoliran_rgba.copy()
    if prosojnost_rdece <= 0.0:
        return rezultat

    idx = maska > 0
    if not np.any(idx):
        return rezultat

    if rezultat.shape[2] == 4:
        rdeca_barva = np.array([255.0, 0.0, 0.0, 255.0], dtype=np.float32)
    else:
        rdeca_barva = np.array([255.0, 0.0, 0.0], dtype=np.float32)

    obarvani = (
        rezultat[idx].astype(np.float32) * (1.0 - prosojnost_rdece)
        + rdeca_barva * prosojnost_rdece
    )
    rezultat[idx] = np.clip(obarvani, 0.0, 255.0).astype(np.uint8)
    return rezultat


@st.cache_data(max_entries=4, show_spinner=False)
def pripravi_png_slike(kljuc_analize, prosojnost_proc, _slika_z_odtisom):
    """Hitro ustvari bajte PNG za prenos kontrolne slike z označenimi odtisi."""
    buf = io.BytesIO()
    Image.fromarray(_slika_z_odtisom).save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
