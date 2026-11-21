import base64
import io
import os
import numpy as np
import pandas as pd
from PIL import Image, ImageOps
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Analiza lističev", layout="wide")

from slike import (
    preveri_rembg_napako,
    poisci_in_izrezi_avtomatsko,
    izrezi_listic_rocno,
    narisi_predogled_izreza,
    prikazi_rocni_izrez,
    analiziraj_kapljice,
    obarvaj_kapljice_rdece,
)

# --- KOMPONENTA ZA ODPIRANJE PRIVZETE KAMERE TELEFONA S POTRDITVIJO SPREJEMA ---
KAMERA_MAPA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kamera_komponenta")
os.makedirs(KAMERA_MAPA, exist_ok=True)
KAMERA_INDEX_POT = os.path.join(KAMERA_MAPA, "index.html")

HTML_KODA_KAMERE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin: 0; padding: 4px 0; font-family: sans-serif; background: transparent; }
  .cam-wrap { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .cam-btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: #ff4b4b; color: #ffffff; border: none; border-radius: 8px;
    padding: 12px 22px; font-size: 15px; font-weight: 600; cursor: pointer;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15); user-select: none;
  }
  .cam-btn:hover { background: #e63e3e; }
  .cam-btn:active { transform: scale(0.98); }
  .retry-btn {
    display: none; align-items: center; justify-content: center; gap: 6px;
    background: #00c853; color: #ffffff; border: none; border-radius: 8px;
    padding: 12px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  .retry-btn:hover { background: #00b248; }
  .preview-row { display: none; align-items: center; gap: 10px; }
  .preview-img {
    width: 72px; height: 72px; object-fit: cover; border-radius: 6px;
    border: 1px solid #d0d7de; background: #111;
  }
  .status { font-size: 13px; color: #7a828e; line-height: 1.4; }
  .status.ok { color: #00a844; font-weight: 600; }
  .status.err { color: #d32f2f; font-weight: 600; }
</style>
</head>
<body>
<div class="cam-wrap">
  <div class="btn-row">
    <input id="camInput" type="file" accept="image/*" capture="environment" style="display:none">
    <label for="camInput" class="cam-btn">📷 Odpri kamero telefona</label>
    <button id="retryBtn" type="button" class="retry-btn">📤 Pošlji sliko v analizo</button>
  </div>
  <div id="previewRow" class="preview-row">
    <img id="previewImg" class="preview-img" alt="Predogled">
    <div id="status" class="status"></div>
  </div>
  <div id="hint" class="status">Odpre privzeto kamero telefona (ostrenje, bliskavica, povečava).</div>
</div>
<script>
const camInput = document.getElementById("camInput");
const retryBtn = document.getElementById("retryBtn");
const previewRow = document.getElementById("previewRow");
const previewImg = document.getElementById("previewImg");
const statusEl = document.getElementById("status");
const hintEl = document.getElementById("hint");

let pendingPayload = null;
let retryTimer = null;
let lastAckTs = null;
const MAX_AVTO_POSKUSOV = 10;

function sendMessage(type, data) {
  window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data), "*");
}

function updateHeight() {
  sendMessage("streamlit:setFrameHeight", { height: document.body.scrollHeight + 12 });
}

function ustaviPonavljanje() {
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function narisiInStisni(img, maxStranica, kakovost) {
  const origW = img.naturalWidth || img.width;
  const origH = img.naturalHeight || img.height;
  const merilo = Math.min(1, maxStranica / Math.max(origW, origH));
  const nw = Math.max(1, Math.round(origW * merilo));
  const nh = Math.max(1, Math.round(origH * merilo));

  const canvas = document.createElement("canvas");
  canvas.width = nw;
  canvas.height = nh;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0, nw, nh);
  return { dataUrl: canvas.toDataURL("image/jpeg", kakovost), nw: nw, nh: nh };
}

function posljiTrenutno(rocno) {
  if (!pendingPayload) return;
  if (lastAckTs !== null && lastAckTs === pendingPayload.ts) {
    ustaviPonavljanje();
    return;
  }

  pendingPayload.attempt = (pendingPayload.attempt || 0) + 1;

  sendMessage("streamlit:setComponentValue", {
    value: {
      name: pendingPayload.name,
      data_url: pendingPayload.data_url,
      ts: pendingPayload.ts,
      attempt: pendingPayload.attempt
    }
  });

  statusEl.className = "status";
  statusEl.textContent =
    "Pošiljam fotografijo (" + pendingPayload.info + ") – poskus " + pendingPayload.attempt + " ...";

  if (pendingPayload.attempt >= 2) {
    retryBtn.style.display = "inline-flex";
  }
  updateHeight();

  ustaviPonavljanje();
  if (pendingPayload.attempt < MAX_AVTO_POSKUSOV || rocno) {
    retryTimer = setTimeout(() => {
      if (pendingPayload && lastAckTs !== pendingPayload.ts && pendingPayload.attempt < MAX_AVTO_POSKUSOV) {
        posljiTrenutno(false);
      } else if (pendingPayload && lastAckTs !== pendingPayload.ts) {
        statusEl.className = "status err";
        statusEl.textContent = "Povezava se je med slikanjem prekinila. Tapni zeleni gumb za pošiljanje.";
        retryBtn.style.display = "inline-flex";
        updateHeight();
      }
    }, 2200);
  }
}

retryBtn.addEventListener("click", () => {
  if (!pendingPayload) return;
  pendingPayload.attempt = 0;
  posljiTrenutno(true);
});

camInput.addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;

  ustaviPonavljanje();
  hintEl.style.display = "none";
  previewRow.style.display = "flex";
  retryBtn.style.display = "none";
  statusEl.className = "status";
  statusEl.textContent = "Pripravljam fotografijo ...";
  updateHeight();

  const objectUrl = URL.createObjectURL(file);
  const img = new Image();

  img.onload = () => {
    try {
      let res = narisiInStisni(img, 2000, 0.80);
      if (res.dataUrl.length > 650000) {
        res = narisiInStisni(img, 1800, 0.70);
      }
      if (res.dataUrl.length > 650000) {
        res = narisiInStisni(img, 1450, 0.65);
      }
      URL.revokeObjectURL(objectUrl);

      const velKB = Math.round((res.dataUrl.length * 3) / 4 / 1024);
      previewImg.src = res.dataUrl;

      pendingPayload = {
        name: file.name || "Kamera_listic.jpg",
        data_url: res.dataUrl,
        ts: Date.now(),
        attempt: 0,
        info: res.nw + "×" + res.nh + " px, " + velKB + " KB"
      };

      camInput.value = "";
      // Kratek zamik (500 ms), da mobilni brskalnik po vrnitvi iz kamere znova vzpostavi povezavo
      retryTimer = setTimeout(() => posljiTrenutno(false), 500);
      updateHeight();
    } catch (err) {
      URL.revokeObjectURL(objectUrl);
      statusEl.className = "status err";
      statusEl.textContent = "Napaka pri pripravi slike: " + err.message;
      camInput.value = "";
      updateHeight();
    }
  };

  img.onerror = () => {
    URL.revokeObjectURL(objectUrl);
    statusEl.className = "status err";
    statusEl.textContent = "Napaka pri branju fotografije. Poskusi znova ali uporabi zavihek Naloži slike.";
    camInput.value = "";
    updateHeight();
  };

  img.src = objectUrl;
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && pendingPayload && lastAckTs !== pendingPayload.ts) {
    ustaviPonavljanje();
    retryTimer = setTimeout(() => posljiTrenutno(false), 700);
  }
});

window.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "streamlit:render") return;
  const args = event.data.args || {};
  if (args.ack_ts !== undefined && args.ack_ts !== null) {
    lastAckTs = args.ack_ts;
    if (pendingPayload && lastAckTs === pendingPayload.ts) {
      ustaviPonavljanje();
      retryBtn.style.display = "none";
      statusEl.className = "status ok";
      statusEl.textContent = "✅ Fotografija sprejeta (" + pendingPayload.info + "). Lahko slikaš naslednji listič.";
    }
  }
  updateHeight();
});

sendMessage("streamlit:componentReady", { apiVersion: 1 });
updateHeight();
</script>
</body>
</html>
"""

_obstojeca_kamera = ""
if os.path.exists(KAMERA_INDEX_POT):
    with open(KAMERA_INDEX_POT, "r", encoding="utf-8") as f:
        _obstojeca_kamera = f.read()
if _obstojeca_kamera != HTML_KODA_KAMERE:
    with open(KAMERA_INDEX_POT, "w", encoding="utf-8") as f:
        f.write(HTML_KODA_KAMERE)

_komponenta_kamera = components.declare_component("nativna_kamera", path=KAMERA_MAPA)

PRIVZETI_PARAMETRI_LETA = {
    "Višina leta (m)": 3.0,
    "Hitrost leta (m/s)": 5.0,
    "Poraba škropiva (L/ha)": 20.0,
    "Velikost kapljic šob (μm)": 150,
    "Hitrost vetra (m/s)": 1.5,
    "Temperatura (°C)": 20.0,
    "Relativna vlažnost (%)": 60,
}

if "rezultati" not in st.session_state:
    st.session_state.rezultati = pd.DataFrame(
        columns=["Ime lističa", "Število odtisov", "Odtisi / cm²", "Pokritost (%)"]
    )
if "oglisca_slik" not in st.session_state:
    st.session_state.oglisca_slik = {}
if "reset_verzija_izreza" not in st.session_state:
    st.session_state.reset_verzija_izreza = {}
if "kamera_slike" not in st.session_state:
    st.session_state.kamera_slike = {}
if "zadnji_kamera_ts" not in st.session_state:
    st.session_state.zadnji_kamera_ts = None
if "stevec_kamere" not in st.session_state:
    st.session_state.stevec_kamere = 0

# --- DRSNO STRANSKO OKNO: PARAMETRI LETA ---
with st.sidebar:
    st.subheader("Parametri leta")
    vkljuci_parametre = st.toggle("Dodaj parametre leta v Excel", value=False)
    izbrani_parametri = {}

    if vkljuci_parametre:
        st.divider()
        for naziv, privzeta_vrednost in PRIVZETI_PARAMETRI_LETA.items():
            kol1, kol2 = st.columns([1, 2])
            with kol1:
                aktiven = st.checkbox(
                    naziv, value=True, key=f"chk_{naziv}", label_visibility="collapsed"
                )
            with kol2:
                st.caption(naziv)
            if aktiven:
                izbrani_parametri[naziv] = st.number_input(
                    naziv,
                    value=float(privzeta_vrednost),
                    step=0.5,
                    key=f"val_{naziv}",
                    label_visibility="collapsed",
                )

# --- NASLOV IN NALAGANJE SLIK ---
st.title("Analiza škropilnih lističev")

zav1, zav2 = st.tabs(["📁 Naloži slike", "📷 Kamera"])
with zav1:
    nalozene_datoteke = st.file_uploader(
        "Naloži ene ali več lističev",
        type=["jpg", "jpeg", "png", "bmp", "webp"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )
with zav2:
    slika_kamere_podatki = _komponenta_kamera(
        ack_ts=st.session_state.zadnji_kamera_ts,
        key="nativna_kamera",
    )
    if (
        isinstance(slika_kamere_podatki, dict)
        and slika_kamere_podatki.get("ts") != st.session_state.zadnji_kamera_ts
        and slika_kamere_podatki.get("data_url")
    ):
        st.session_state.zadnji_kamera_ts = slika_kamere_podatki["ts"]
        data_url = slika_kamere_podatki["data_url"]
        if "," in data_url:
            _, b64_podatki = data_url.split(",", 1)
            st.session_state.stevec_kamere += 1
            ime_slike = f"Kamera_listic_{st.session_state.stevec_kamere}.jpg"
            st.session_state.kamera_slike[ime_slike] = base64.b64decode(b64_podatki)
            st.session_state.izbrani_listic_select = ime_slike
            st.rerun()

    if st.session_state.kamera_slike:
        st.caption(f"Zajetih fotografij iz kamere: **{len(st.session_state.kamera_slike)}**")
        if st.button("🗑️ Počisti slike iz kamere"):
            st.session_state.kamera_slike = {}
            st.session_state.zadnji_kamera_ts = None
            st.rerun()

vsi_listici = {}
if nalozene_datoteke:
    for dat in nalozene_datoteke:
        vsi_listici[dat.name] = dat
for ime_k, bajti_k in st.session_state.kamera_slike.items():
    vsi_listici[ime_k] = io.BytesIO(bajti_k)

if vsi_listici:
    imena_listicev = list(vsi_listici.keys())
    if (
        "izbrani_listic_select" in st.session_state
        and st.session_state.izbrani_listic_select not in vsi_listici
    ):
        st.session_state.izbrani_listic_select = imena_listicev[0]

    izbrano_ime = st.selectbox(
        "Izberi listič za analizo", imena_listicev, key="izbrani_listic_select"
    )
    izbrana_datoteka = vsi_listici[izbrano_ime]
    osnovno_ime = os.path.splitext(izbrano_ime)[0]

    # Samodejna korekcija EXIF orientacije fotografij s telefona
    pil_slika = ImageOps.exif_transpose(Image.open(izbrana_datoteka)).convert("RGB")
    slika_rgb = np.array(pil_slika)

    st.divider()

    # --- DVA PREKLOPNA GUMBA (TOGGLE BARA) ---
    t_kol1, t_kol2 = st.columns(2)
    with t_kol1:
        standardna_dim = st.toggle("Standardne dimenzije (76 × 22 mm)", value=True)
        if standardna_dim:
            st.caption("Samodejna zaznava vseh odtisov nad **50 μm**")
            min_velikost_px = 1
            obcutljivost = None
        else:
            min_velikost_px = st.slider(
                "Najmanjša velikost zaznane kapljice (px)",
                min_value=1,
                max_value=150,
                value=1,
            )

    with t_kol2:
        avtomatski_izrez = st.toggle("Avtomatski izrez iz ozadja", value=True)
        if not standardna_dim:
            obcutljivost = st.slider(
                "Občutljivost zaznave odtisov (%)",
                min_value=1,
                max_value=100,
                value=75,
            )

    # --- OBREZOVANJE LISTIČA (AVTOMATSKO rembg u2net ALI ROČNO) ---
    with st.spinner("Izrezujem listič iz ozadja (rembg u2net)..."):
        avto_listic, avto_oglisca, dejanska_maska = poisci_in_izrezi_avtomatsko(slika_rgb)
        napaka_rembg = preveri_rembg_napako()

    if napaka_rembg:
        st.warning(
            f"Knjižnica `rembg` (model u2net) se ni mogla naložiti! "
            f"Podrobnost napake: `{napaka_rembg}`"
        )

    if izbrano_ime not in st.session_state.oglisca_slik:
        st.session_state.oglisca_slik[izbrano_ime] = avto_oglisca
    if izbrano_ime not in st.session_state.reset_verzija_izreza:
        st.session_state.reset_verzija_izreza[izbrano_ime] = 0

    if avtomatski_izrez:
        izrezan_listic = avto_listic
        with st.expander("👁️ Preveri avtomatski izrez na izvirni sliki", expanded=False):
            st.image(
                narisi_predogled_izreza(slika_rgb, avto_oglisca, dejanska_maska=dejanska_maska),
                use_container_width=True,
            )
    else:
        if st.button("🔄 Ponastavi avtomatski izrez"):
            st.session_state.oglisca_slik[izbrano_ime] = avto_oglisca
            st.session_state.reset_verzija_izreza[izbrano_ime] += 1

        oglisca = prikazi_rocni_izrez(
            slika_rgb,
            st.session_state.oglisca_slik[izbrano_ime],
            kljuc=f"izrez_{izbrano_ime}",
            reset_version=st.session_state.reset_verzija_izreza[izbrano_ime],
        )
        st.session_state.oglisca_slik[izbrano_ime] = oglisca
        izrezan_listic = izrezi_listic_rocno(slika_rgb, oglisca)

    maska_kapljic, st_odtisov, odtisi_cm2, pokritost = analiziraj_kapljice(
        izrezan_listic,
        standardna_dimenzija=standardna_dim,
        min_velikost_px=min_velikost_px,
        obcutljivost=obcutljivost,
    )

    st.divider()

    # --- PRIKAZ IZREZANEGA LISTIČA IN REZULTATOV ---
    prikaz_kol, rezultati_kol = st.columns([3, 2])

    with prikaz_kol:
        prosojnost = st.slider("Prosojnost zaznanih kapljic (%)", 0, 100, 75)
        slika_z_odtisom = obarvaj_kapljice_rdece(izrezan_listic, maska_kapljic, prosojnost)
        st.image(slika_z_odtisom, use_container_width=True)

    with rezultati_kol:
        if standardna_dim:
            m1, m2, m3 = st.columns(3)
            m1.metric("Vsi odtisi", f"{st_odtisov}")
            m2.metric("Odtisi / cm²", f"{odtisi_cm2}")
            m3.metric("Pokritost", f"{pokritost} %")
        else:
            m1, m2 = st.columns(2)
            m1.metric("Vsi odtisi", f"{st_odtisov}")
            m2.metric("Pokritost", f"{pokritost} %")

        if st.button("💾 Shrani v preglednico", use_container_width=True, type="primary"):
            nova_vrstica = {
                "Ime lističa": osnovno_ime,
                "Število odtisov": st_odtisov,
                "Odtisi / cm²": odtisi_cm2 if standardna_dim else "-",
                "Pokritost (%)": pokritost,
            }
            for k, v in izbrani_parametri.items():
                nova_vrstica[k] = v

            df = st.session_state.rezultati
            if not df.empty and osnovno_ime in df["Ime lističa"].values:
                idx = df.index[df["Ime lističa"] == osnovno_ime][0]
                for k, v in nova_vrstica.items():
                    df.loc[idx, k] = v
                st.session_state.rezultati = df
            else:
                st.session_state.rezultati = pd.concat(
                    [df, pd.DataFrame([nova_vrstica])], ignore_index=True
                )

# --- SPODNJA UREJEVALNA PREGLEDNICA IN IZVOZ V EXCEL ---
if not st.session_state.rezultati.empty:
    st.divider()
    urejena_tabela = st.data_editor(
        st.session_state.rezultati,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key="urejevalnik_rezultatov",
    )
    st.session_state.rezultati = urejena_tabela

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        urejena_tabela.to_excel(writer, index=False, sheet_name="Rezultati")

    st.download_button(
        label="📥 Izvozi v Excel (.xlsx)",
        data=excel_buffer.getvalue(),
        file_name="Analiza_listicev.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )