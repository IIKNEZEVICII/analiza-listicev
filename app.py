import base64
import hashlib
import io
import os
import numpy as np
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Analiza lističev", layout="wide")

from slike import (
    AVTO_IZREZ_CACHE_MAX,
    preveri_rembg_napako,
    nalozi_sliko_rgb,
    poisci_in_izrezi_avtomatsko_predpomnjeno,
    izrezi_listic_rocno_predpomnjeno,
    narisi_predogled_izreza_predpomnjeno,
    prikazi_rocni_izrez,
    analiziraj_kapljice_predpomnjeno,
    obarvaj_kapljice_rdece,
    pripravi_png_slike,
)


def _uporabi_nov_parameter_sirine():
    """Preveri, ali trenutna različica Streamlit podpira width='stretch'."""
    try:
        deli = []
        for del_v in st.__version__.split(".")[:2]:
            stevke = "".join(c for c in del_v if c.isdigit())
            deli.append(int(stevke) if stevke else 0)
        return tuple(deli) >= (1, 50)
    except Exception:
        return False


_POLNA_SIRINA_KWARGS = (
    {"width": "stretch"} if _uporabi_nov_parameter_sirine() else {"use_container_width": True}
)

# --- KOMPONENTA ZA ODPIRANJE PRIVZETE KAMERE TELEFONA ---
HTML_KODA_KAMERE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin: 0; padding: 4px 0; font-family: sans-serif; background: transparent; }
  .cam-wrap { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .cam-btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: #ff4b4b; color: #ffffff; border: none; border-radius: 8px;
    padding: 11px 20px; font-size: 15px; font-weight: 600; cursor: pointer;
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.12); user-select: none;
  }
  .cam-btn:hover { background: #e63e3e; }
  .cam-btn:active { transform: scale(0.98); }
  .retry-btn {
    display: none; align-items: center; justify-content: center; gap: 6px;
    background: #4b5563; color: #ffffff; border: none; border-radius: 8px;
    padding: 11px 16px; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  .retry-btn:hover { background: #374151; }
  .status { font-size: 12px; color: #7a828e; line-height: 1.4; }
  .status.err { color: #d32f2f; }
</style>
</head>
<body>
<div class="cam-wrap">
  <div class="btn-row">
    <input id="camInput" type="file" accept="image/*" capture="environment" style="display:none">
    <label for="camInput" class="cam-btn">📷 Odpri kamero telefona</label>
    <button id="retryBtn" type="button" class="retry-btn">Poskusi znova poslati</button>
  </div>
  <div id="status" class="status">Odpre privzeto kamero telefona (ostrenje, bliskavica, povečava).</div>
</div>
<script>
const camInput = document.getElementById("camInput");
const retryBtn = document.getElementById("retryBtn");
const statusEl = document.getElementById("status");
const PRIVZET_NAPIS = "Odpre privzeto kamero telefona (ostrenje, bliskavica, povečava).";

let pendingPayload = null;
let retryTimer = null;
let lastAckTs = null;
const MAX_AVTO_POSKUSOV = 10;

function sendMessage(type, data) {
  window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, data), "*");
}

function updateHeight() {
  sendMessage("streamlit:setFrameHeight", { height: document.body.scrollHeight + 8 });
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
  statusEl.textContent = "Nalagam fotografijo ...";

  if (pendingPayload.attempt >= 3) {
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
        statusEl.textContent = "Povezava se je med slikanjem prekinila. Tapni »Poskusi znova poslati«.";
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

      pendingPayload = {
        name: file.name || "Kamera_listic.jpg",
        data_url: res.dataUrl,
        ts: Date.now(),
        attempt: 0
      };

      camInput.value = "";
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
    statusEl.textContent = "Napaka pri branju fotografije. Poskusi znova.";
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
      statusEl.className = "status";
      statusEl.textContent = PRIVZET_NAPIS;
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


@st.cache_resource
def _pridobi_komponento_kamere():
    kamera_mapa = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_kamera_komponenta")
    os.makedirs(kamera_mapa, exist_ok=True)
    index_pot = os.path.join(kamera_mapa, "index.html")
    obstojece = ""
    if os.path.exists(index_pot):
        with open(index_pot, "r", encoding="utf-8") as f:
            obstojece = f.read()
    if obstojece != HTML_KODA_KAMERE:
        with open(index_pot, "w", encoding="utf-8") as f:
            f.write(HTML_KODA_KAMERE)
    return components.declare_component("nativna_kamera", path=kamera_mapa)


@st.cache_data(max_entries=4, show_spinner=False)
def _ustvari_excel_bajte(tabela_df):
    """Ustvari urejeno Excel datoteko s samodejnimi širinami stolpcev, filtrom in zamrznjeno glavo."""
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        tabela_df.to_excel(writer, index=False, sheet_name="Rezultati")
        ws = writer.sheets["Rezultati"]

        ws.freeze_panes = "A2"
        if ws.max_row >= 1 and ws.max_column >= 1:
            ws.auto_filter.ref = ws.dimensions

        polnilo_glave = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        pisava_glave = Font(bold=True, color="FFFFFF")
        poravnava_glave = Alignment(horizontal="center", vertical="center")

        for col_idx, stolpec in enumerate(tabela_df.columns, start=1):
            celica_glave = ws.cell(row=1, column=col_idx)
            celica_glave.fill = polnilo_glave
            celica_glave.font = pisava_glave
            celica_glave.alignment = poravnava_glave

            max_dolzina = len(str(stolpec))
            for row_idx in range(2, ws.max_row + 1):
                celica = ws.cell(row=row_idx, column=col_idx)
                vrednost = celica.value
                if vrednost is not None:
                    max_dolzina = max(max_dolzina, len(str(vrednost)))
                    if stolpec == "Število odtisov" and isinstance(vrednost, (int, float)):
                        celica.number_format = "0"
                    elif stolpec in ("Odtisi / cm²", "Pokritost (%)") and isinstance(vrednost, (int, float)):
                        celica.number_format = "0.00"

            crka = get_column_letter(col_idx)
            ws.column_dimensions[crka].width = min(max(max_dolzina + 4, 12), 42)

    return excel_buffer.getvalue()


def _preberi_bajte_vira(vir):
    """Leno prebere bajte iz naložene datoteke ali že shranjenih bajtov kamere."""
    if isinstance(vir, (bytes, bytearray)):
        return bytes(vir)
    return vir.getvalue()


def _oznaci_kot_obdelano(kljuc):
    """Vzdržuje LRU evidenco že obdelanih slik, usklajeno s predpomnilnikom avtomatskega izreza."""
    seznam = st.session_state.obdelani_kljuci
    if kljuc in seznam:
        seznam.remove(kljuc)
    seznam.append(kljuc)
    if len(seznam) > AVTO_IZREZ_CACHE_MAX:
        del seznam[:-AVTO_IZREZ_CACHE_MAX]


def _premakni_listic(seznam_imen, korak):
    """Povratni klic za gumba Prejšnji / Naslednji brez napake pri spreminjanju stanja."""
    trenutno = st.session_state.get("izbrani_listic_select", seznam_imen[0])
    idx = seznam_imen.index(trenutno) if trenutno in seznam_imen else 0
    novi_idx = max(0, min(len(seznam_imen) - 1, idx + korak))
    st.session_state.izbrani_listic_select = seznam_imen[novi_idx]


def _shrani_vrstico_v_tabelo(osnovni_df, nova_vrstica, osnovno_ime):
    """Varno doda ali posodobi vrstico in ohrani številski tip stolpcev za PyArrow in Excel."""
    df = osnovni_df.reset_index(drop=True).copy()

    if df.empty:
        df = pd.DataFrame([nova_vrstica])
        je_posodobitev = False
    else:
        for stolpec in nova_vrstica:
            if stolpec not in df.columns:
                df[stolpec] = np.nan

        for stolpec in df.columns:
            if stolpec != "Ime lističa" and (
                stolpec in STOLPCI_NUM or stolpec in PRIVZETI_PARAMETRI_LETA
            ):
                df[stolpec] = pd.to_numeric(df[stolpec], errors="coerce").astype("float64")

        if "Ime lističa" in df.columns and osnovno_ime in df["Ime lističa"].values:
            idx = df.index[df["Ime lističa"] == osnovno_ime][0]
            for k, v in nova_vrstica.items():
                df.at[idx, k] = np.nan if v is None else v
            je_posodobitev = True
        else:
            novi_indeks = len(df)
            for k, v in nova_vrstica.items():
                df.loc[novi_indeks, k] = np.nan if v is None else v
            je_posodobitev = False

    for stolpec in df.columns:
        if stolpec != "Ime lističa" and (
            stolpec in STOLPCI_NUM or stolpec in PRIVZETI_PARAMETRI_LETA
        ):
            df[stolpec] = pd.to_numeric(df[stolpec], errors="coerce")

    return df.reset_index(drop=True), je_posodobitev


_komponenta_kamera = _pridobi_komponento_kamere()

STOLPCI_NUM = {"Število odtisov", "Odtisi / cm²", "Pokritost (%)"}

# Oblika: (privzeta vrednost, minimum, maksimum, korak)
PRIVZETI_PARAMETRI_LETA = {
    "Višina leta (m)": (3.0, 0.0, 200.0, 0.5),
    "Hitrost leta (m/s)": (5.0, 0.0, 50.0, 0.5),
    "Poraba škropiva (L/ha)": (20.0, 0.0, 500.0, 1.0),
    "Velikost kapljic šob (μm)": (150.0, 10.0, 2000.0, 10.0),
    "Hitrost vetra (m/s)": (1.5, 0.0, 40.0, 0.5),
    "Temperatura (°C)": (20.0, -20.0, 60.0, 0.5),
    "Relativna vlažnost (%)": (60.0, 0.0, 100.0, 1.0),
}

if "rezultati" not in st.session_state:
    st.session_state.rezultati = pd.DataFrame(
        {
            "Ime lističa": pd.Series(dtype="object"),
            "Število odtisov": pd.Series(dtype="float64"),
            "Odtisi / cm²": pd.Series(dtype="float64"),
            "Pokritost (%)": pd.Series(dtype="float64"),
        }
    )
if "zadnja_urejena_tabela" not in st.session_state:
    st.session_state.zadnja_urejena_tabela = None
if "tabela_verzija" not in st.session_state:
    st.session_state.tabela_verzija = 0
if "oglisca_slik" not in st.session_state:
    st.session_state.oglisca_slik = {}
if "reset_verzija_izreza" not in st.session_state:
    st.session_state.reset_verzija_izreza = {}
if "obdelani_kljuci" not in st.session_state or not isinstance(
    st.session_state.obdelani_kljuci, list
):
    st.session_state.obdelani_kljuci = []
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
        for naziv, (privzeta, min_v, max_v, korak) in PRIVZETI_PARAMETRI_LETA.items():
            aktiven = st.checkbox(naziv, value=True, key=f"chk_{naziv}")
            if aktiven:
                izbrani_parametri[naziv] = st.number_input(
                    naziv,
                    min_value=float(min_v),
                    max_value=float(max_v),
                    value=float(privzeta),
                    step=float(korak),
                    key=f"val_{naziv}",
                    label_visibility="collapsed",
                )

# --- NASLOV IN NALAGANJE SLIK ---
st.title("Analiza škropilnih lističev")

zav1, zav2 = st.tabs(["📁 Naloži slike", "📷 Kamera"])
with zav1:
    nalozene_datoteke = st.file_uploader(
        "Naloži ene ali več lističev",
        type=["jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff"],
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
            try:
                _, b64_podatki = data_url.split(",", 1)
                bajti_slike = base64.b64decode(b64_podatki)
                st.session_state.stevec_kamere += 1
                ime_slike = f"Kamera_listic_{st.session_state.stevec_kamere}.jpg"
                st.session_state.kamera_slike[ime_slike] = bajti_slike
                st.session_state.izbrani_listic_select = ime_slike
                st.rerun()
            except Exception as napaka_kamere:
                st.error(f"Fotografije iz kamere ni bilo mogoče prebrati: {napaka_kamere}")

    if st.session_state.kamera_slike:
        if st.button("🗑️ Počisti slike kamere"):
            st.session_state.kamera_slike.clear()
            st.rerun()

# Hranimo le sklice na datoteke (bajti se preberejo šele za izbrani listič)
vsi_viri = {}
if nalozene_datoteke:
    for dat in nalozene_datoteke:
        ime_dat = dat.name
        stevec_dup = 2
        while ime_dat in vsi_viri:
            osn, konc = os.path.splitext(dat.name)
            ime_dat = f"{osn} ({stevec_dup}){konc}"
            stevec_dup += 1
        vsi_viri[ime_dat] = dat

for ime_k, bajti_k in st.session_state.kamera_slike.items():
    vsi_viri[ime_k] = bajti_k

if vsi_viri:
    imena_listicev = list(vsi_viri.keys())
    if (
        "izbrani_listic_select" not in st.session_state
        or st.session_state.izbrani_listic_select not in vsi_viri
    ):
        st.session_state.izbrani_listic_select = imena_listicev[0]

    if len(imena_listicev) > 1:
        trenutni_idx = imena_listicev.index(st.session_state.izbrani_listic_select)
        nav_kol1, nav_kol2, nav_kol3 = st.columns([6, 1, 1], vertical_alignment="bottom")
        with nav_kol1:
            izbrano_ime = st.selectbox(
                f"Izberi listič za analizo ({trenutni_idx + 1}/{len(imena_listicev)})",
                imena_listicev,
                key="izbrani_listic_select",
            )
        with nav_kol2:
            st.button(
                "⬅️",
                help="Prejšnji listič",
                disabled=(trenutni_idx <= 0),
                on_click=_premakni_listic,
                args=(imena_listicev, -1),
                **_POLNA_SIRINA_KWARGS,
            )
        with nav_kol3:
            st.button(
                "➡️",
                help="Naslednji listič",
                disabled=(trenutni_idx >= len(imena_listicev) - 1),
                on_click=_premakni_listic,
                args=(imena_listicev, 1),
                **_POLNA_SIRINA_KWARGS,
            )
    else:
        izbrano_ime = st.selectbox(
            "Izberi listič za analizo", imena_listicev, key="izbrani_listic_select"
        )

    surovi_bajti = _preberi_bajte_vira(vsi_viri[izbrano_ime])
    kljuc_slike = hashlib.md5(surovi_bajti).hexdigest()
    osnovno_ime = os.path.splitext(izbrano_ime)[0]

    # Predpomnjeno nalaganje s samodejno korekcijo orientacije EXIF
    try:
        slika_rgb = nalozi_sliko_rgb(kljuc_slike, surovi_bajti)
    except Exception as napaka_slike:
        st.error(f"Datoteke `{izbrano_ime}` ni mogoče odpreti kot slike ({napaka_slike}).")
        st.stop()

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

    # --- OBREZOVANJE LISTIČA (PREDPOMNJENO AVTOMATSKO rembg u2net ALI ROČNO) ---
    if kljuc_slike in st.session_state.obdelani_kljuci:
        avto_listic, avto_oglisca, dejanska_maska = poisci_in_izrezi_avtomatsko_predpomnjeno(
            kljuc_slike, slika_rgb
        )
    else:
        with st.spinner("Izrezujem listič iz ozadja (rembg u2net)..."):
            avto_listic, avto_oglisca, dejanska_maska = poisci_in_izrezi_avtomatsko_predpomnjeno(
                kljuc_slike, slika_rgb
            )
    _oznaci_kot_obdelano(kljuc_slike)

    napaka_rembg = preveri_rembg_napako()
    if napaka_rembg:
        st.warning(
            f"Knjižnica `rembg` (model u2net) se ni mogla naložiti! "
            f"Podrobnost napake: `{napaka_rembg}`"
        )

    if kljuc_slike not in st.session_state.oglisca_slik:
        st.session_state.oglisca_slik[kljuc_slike] = [list(pt) for pt in avto_oglisca]
    if kljuc_slike not in st.session_state.reset_verzija_izreza:
        st.session_state.reset_verzija_izreza[kljuc_slike] = 0

    if avtomatski_izrez:
        izrezan_listic = avto_listic
        kljuc_izreza = f"avto_{kljuc_slike}"
        with st.expander("👁️ Preveri avtomatski izrez na izvirni sliki", expanded=False):
            predogled_izreza = narisi_predogled_izreza_predpomnjeno(
                kljuc_slike, slika_rgb, avto_oglisca, dejanska_maska
            )
            st.image(predogled_izreza, **_POLNA_SIRINA_KWARGS)
    else:
        if st.button("🔄 Ponastavi avtomatski izrez"):
            st.session_state.oglisca_slik[kljuc_slike] = [list(pt) for pt in avto_oglisca]
            st.session_state.reset_verzija_izreza[kljuc_slike] += 1

        oglisca = prikazi_rocni_izrez(
            slika_rgb,
            st.session_state.oglisca_slik[kljuc_slike],
            kljuc=f"izrez_{kljuc_slike}",
            reset_version=st.session_state.reset_verzija_izreza[kljuc_slike],
            kljuc_slike=kljuc_slike,
        )
        st.session_state.oglisca_slik[kljuc_slike] = oglisca
        oglisca_kljuc = tuple((round(float(pt[0]), 6), round(float(pt[1]), 6)) for pt in oglisca)
        izrezan_listic = izrezi_listic_rocno_predpomnjeno(kljuc_slike, oglisca_kljuc, slika_rgb)
        kljuc_izreza = f"rocno_{kljuc_slike}_{oglisca_kljuc}"

    maska_kapljic, st_odtisov, odtisi_cm2, pokritost = analiziraj_kapljice_predpomnjeno(
        kljuc_izreza,
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
        st.image(slika_z_odtisom, **_POLNA_SIRINA_KWARGS)

        kljuc_analize = f"{kljuc_izreza}_{standardna_dim}_{min_velikost_px}_{obcutljivost}"
        st.download_button(
            label="🖼️ Prenesi sliko z odtisi (.png)",
            data=pripravi_png_slike(kljuc_analize, prosojnost, slika_z_odtisom),
            file_name=f"{osnovno_ime}_analiza.png",
            mime="image/png",
        )

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

        if st.button("💾 Shrani v preglednico", type="primary", **_POLNA_SIRINA_KWARGS):
            nova_vrstica = {
                "Ime lističa": osnovno_ime,
                "Število odtisov": st_odtisov,
                "Odtisi / cm²": odtisi_cm2 if standardna_dim else None,
                "Pokritost (%)": pokritost,
            }
            for k, v in izbrani_parametri.items():
                nova_vrstica[k] = v

            osnovni_df = (
                st.session_state.zadnja_urejena_tabela
                if st.session_state.zadnja_urejena_tabela is not None
                else st.session_state.rezultati
            )
            novi_df, je_posodobitev = _shrani_vrstico_v_tabelo(
                osnovni_df, nova_vrstica, osnovno_ime
            )
            st.session_state.rezultati = novi_df
            st.session_state.zadnja_urejena_tabela = None
            st.session_state.tabela_verzija += 1
            st.toast(
                f"Posodobljeno v preglednici: {osnovno_ime}"
                if je_posodobitev
                else f"Shranjeno v preglednico: {osnovno_ime}"
            )

        if len(imena_listicev) > 1:
            if st.button(
                f"⚡ Analiziraj in shrani vse ({len(imena_listicev)})",
                help="Za vse naložene lističe izvede avtomatski izrez in zaznavo s trenutnimi nastavitvami.",
                **_POLNA_SIRINA_KWARGS,
            ):
                osnovni_df = (
                    st.session_state.zadnja_urejena_tabela
                    if st.session_state.zadnja_urejena_tabela is not None
                    else st.session_state.rezultati
                )
                napredek = st.progress(0, text="Začenjam paketno analizo ...")
                neuspesni = []
                skupaj = len(imena_listicev)

                for idx_p, ime_p in enumerate(imena_listicev, start=1):
                    napredek.progress(
                        idx_p / float(skupaj),
                        text=f"Analiziram ({idx_p}/{skupaj}): {ime_p}",
                    )
                    try:
                        bajti_p = _preberi_bajte_vira(vsi_viri[ime_p])
                        kljuc_p = hashlib.md5(bajti_p).hexdigest()
                        rgb_p = nalozi_sliko_rgb(kljuc_p, bajti_p)
                        avto_p, _, _ = poisci_in_izrezi_avtomatsko_predpomnjeno(kljuc_p, rgb_p)
                        _oznaci_kot_obdelano(kljuc_p)
                        _, st_p, cm2_p, pok_p = analiziraj_kapljice_predpomnjeno(
                            f"avto_{kljuc_p}",
                            avto_p,
                            standardna_dimenzija=standardna_dim,
                            min_velikost_px=min_velikost_px,
                            obcutljivost=obcutljivost,
                        )
                        osn_p = os.path.splitext(ime_p)[0]
                        vrstica_p = {
                            "Ime lističa": osn_p,
                            "Število odtisov": st_p,
                            "Odtisi / cm²": cm2_p if standardna_dim else None,
                            "Pokritost (%)": pok_p,
                        }
                        for k, v in izbrani_parametri.items():
                            vrstica_p[k] = v
                        osnovni_df, _ = _shrani_vrstico_v_tabelo(osnovni_df, vrstica_p, osn_p)
                    except Exception:
                        neuspesni.append(ime_p)

                napredek.empty()
                st.session_state.rezultati = osnovni_df
                st.session_state.zadnja_urejena_tabela = None
                st.session_state.tabela_verzija += 1

                if neuspesni:
                    st.warning(
                        f"Paketna analiza končana; napaka pri branju: {', '.join(neuspesni)}"
                    )
                else:
                    st.toast(f"Shranjenih vseh {skupaj} lističev v preglednico.")

# --- SPODNJA UREJEVALNA PREGLEDNICA IN IZVOZ V EXCEL ---
if not st.session_state.rezultati.empty:
    st.divider()
    urejena_tabela = st.data_editor(
        st.session_state.rezultati,
        num_rows="dynamic",
        hide_index=True,
        key=f"urejevalnik_rezultatov_{st.session_state.tabela_verzija}",
        **_POLNA_SIRINA_KWARGS,
    )
    st.session_state.zadnja_urejena_tabela = urejena_tabela

    if not urejena_tabela.empty:
        st.download_button(
            label="📥 Izvozi v Excel (.xlsx)",
            data=_ustvari_excel_bajte(urejena_tabela),
            file_name="Analiza_listicev.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
