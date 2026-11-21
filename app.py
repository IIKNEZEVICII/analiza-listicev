import hashlib
import io
import os
import cv2
import numpy as np
import pandas as pd
from PIL import Image, ImageOps
import streamlit as st

st.set_page_config(page_title="Analiza lističev", layout="wide")

from slike import (
    preveri_rembg_napako,
    poisci_in_izrezi_avtomatsko,
    izrezi_listic_rocno,
    narisi_predogled_izreza,
    prikazi_rocni_izrez,
    analiziraj_kapljice,
    obarvaj_kapljice_rdece,
    preveri_kakovost_izreza,
)

OSNOVNI_STOLPCI = ["Ime lističa", "Število odtisov", "Odtisi / cm²", "Pokritost (%)"]

PRIVZETI_PARAMETRI_LETA = {
    "Višina leta (m)": 3.0,
    "Hitrost leta (m/s)": 5.0,
    "Poraba škropiva (L/ha)": 20.0,
    "Velikost kapljic šob (μm)": 150,
    "Hitrost vetra (m/s)": 1.5,
    "Temperatura (°C)": 20.0,
    "Relativna vlažnost (%)": 60,
}


@st.cache_data(max_entries=5, show_spinner=False)
def nalozi_sliko_rgb(bajti_slike, kljuc_slike):
    """Enkratno dekodiranje in EXIF poravnava naložene slike."""
    pil_slika = ImageOps.exif_transpose(Image.open(io.BytesIO(bajti_slike))).convert("RGB")
    return np.array(pil_slika)


@st.cache_data(max_entries=5, show_spinner=False)
def pripravi_predogled_izreza_jpg(_slika_rgb, oglisca_kljuc, _dejanska_maska, kljuc_slike):
    """Predpomnjen predogled avtomatskega izreza kot lahek JPEG."""
    predogled = narisi_predogled_izreza(_slika_rgb, oglisca_kljuc, dejanska_maska=_dejanska_maska)
    buf = io.BytesIO()
    Image.fromarray(predogled).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@st.cache_data(max_entries=6, show_spinner=False)
def pripravi_podlago_za_prikaz(_izrezan_listic, _maska_kapljic, kljuc_prikaza):
    """
    Pripravi zaslonsko različico lističa (do 1400 px širine) in maske za takojšen odziv
    drsnika prosojnosti. INTER_AREA + (> 0) na maski ohrani vidnost vseh drobnih 50 μm pik.
    """
    if _izrezan_listic.shape[2] == 4:
        rgb = _izrezan_listic[:, :, :3].copy()
        rgb[_izrezan_listic[:, :, 3] < 128] = 20
    else:
        rgb = _izrezan_listic

    h, w = rgb.shape[:2]
    max_prikaz_w = 1400
    if w > max_prikaz_w:
        merilo = max_prikaz_w / float(w)
        nw = max_prikaz_w
        nh = max(20, int(round(h * merilo)))
        rgb_m = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        maska_m = (
            cv2.resize(_maska_kapljic, (nw, nh), interpolation=cv2.INTER_AREA) > 0
        ).astype(np.uint8) * 255
        return rgb_m, maska_m

    return rgb, _maska_kapljic


@st.fragment
def prikazi_interaktivni_prekriv(slika_prikaz_rgb, maska_prikaz):
    """Fragment omogoča takojšnje spreminjanje prosojnosti brez ponovnega zagona celotne strani."""
    prosojnost = st.slider(
        "Prosojnost zaznanih kapljic (%)", 0, 100, 75, key="prosojnost_kapljic"
    )
    slika_z_odtisom = obarvaj_kapljice_rdece(slika_prikaz_rgb, maska_prikaz, prosojnost)
    buf = io.BytesIO()
    Image.fromarray(slika_z_odtisom).save(buf, format="JPEG", quality=92, subsampling=0)
    st.image(buf.getvalue(), width="stretch")


# --- INICIALIZACIJA STANJA SEJE ---
if "rezultati_baza" not in st.session_state:
    st.session_state.rezultati_baza = pd.DataFrame(columns=OSNOVNI_STOLPCI)
if "zadnja_tabela" not in st.session_state:
    st.session_state.zadnja_tabela = st.session_state.rezultati_baza.copy()
if "tabela_verzija" not in st.session_state:
    st.session_state.tabela_verzija = 0
if "oglisca_slik" not in st.session_state:
    st.session_state.oglisca_slik = {}
if "reset_verzija_izreza" not in st.session_state:
    st.session_state.reset_verzija_izreza = {}
if "kamera_slike" not in st.session_state:
    st.session_state.kamera_slike = {}

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
    slika_kamere = st.camera_input("Slikaj listič", label_visibility="collapsed")

vsi_listici = {}
if nalozene_datoteke:
    for dat in nalozene_datoteke:
        bajti = dat.getvalue()
        kljuc = hashlib.md5(bajti).hexdigest()
        ime = dat.name
        if ime in vsi_listici:
            osnova, koncnica = os.path.splitext(ime)
            st_kopije = 2
            while f"{osnova}_{st_kopije}{koncnica}" in vsi_listici:
                st_kopije += 1
            ime = f"{osnova}_{st_kopije}{koncnica}"
        vsi_listici[ime] = (kljuc, bajti)

if slika_kamere:
    bajti_kam = slika_kamere.getvalue()
    kljuc_kam = hashlib.md5(bajti_kam).hexdigest()
    if kljuc_kam not in st.session_state.kamera_slike:
        zap_st = len(st.session_state.kamera_slike) + 1
        st.session_state.kamera_slike[kljuc_kam] = (f"Kamera_{zap_st}.jpg", bajti_kam)

for kljuc_kam, (ime_kam, bajti_kam) in st.session_state.kamera_slike.items():
    vsi_listici[ime_kam] = (kljuc_kam, bajti_kam)

if vsi_listici:
    izbrano_ime = st.selectbox("Izberi listič za analizo", list(vsi_listici.keys()))
    kljuc_slike, izbrani_bajti = vsi_listici[izbrano_ime]
    osnovno_ime = os.path.splitext(izbrano_ime)[0]

    slika_rgb = nalozi_sliko_rgb(izbrani_bajti, kljuc_slike)

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
        avto_listic, avto_oglisca, dejanska_maska = poisci_in_izrezi_avtomatsko(
            slika_rgb, kljuc_slike=kljuc_slike
        )
        napaka_rembg = preveri_rembg_napako()

    if napaka_rembg:
        st.warning(
            f"Knjižnica `rembg` (model u2net) se ni mogla naložiti! "
            f"Podrobnost napake: `{napaka_rembg}`"
        )

    if kljuc_slike not in st.session_state.oglisca_slik:
        st.session_state.oglisca_slik[kljuc_slike] = avto_oglisca
    if kljuc_slike not in st.session_state.reset_verzija_izreza:
        st.session_state.reset_verzija_izreza[kljuc_slike] = 0

    if avtomatski_izrez:
        izrezan_listic = avto_listic
        aktivna_oglisca = avto_oglisca
        kljuc_izreza = f"{kljuc_slike}_auto"
        with st.expander("👁️ Preveri avtomatski izrez na izvirni sliki", expanded=False):
            predogled_jpg = pripravi_predogled_izreza_jpg(
                slika_rgb,
                tuple(tuple(round(v, 4) for v in pt) for pt in avto_oglisca),
                dejanska_maska,
                kljuc_slike,
            )
            st.image(predogled_jpg, width="stretch")
    else:
        if st.button("🔄 Ponastavi avtomatski izrez"):
            st.session_state.oglisca_slik[kljuc_slike] = avto_oglisca
            st.session_state.reset_verzija_izreza[kljuc_slike] += 1

        oglisca = prikazi_rocni_izrez(
            slika_rgb,
            st.session_state.oglisca_slik[kljuc_slike],
            kljuc=f"izrez_{kljuc_slike}",
            reset_version=st.session_state.reset_verzija_izreza[kljuc_slike],
            kljuc_slike=kljuc_slike,
        )
        st.session_state.oglisca_slik[kljuc_slike] = oglisca
        aktivna_oglisca = oglisca
        oglisca_podpis = tuple((round(p[0], 4), round(p[1], 4)) for p in oglisca)
        kljuc_izreza = f"{kljuc_slike}_rocno_{oglisca_podpis}"
        izrezan_listic = izrezi_listic_rocno(slika_rgb, oglisca)

    # --- PREVERJANJE KAKOVOSTI IZREZA (RAZMERJE STRANIC IN LOČLJIVOST) ---
    if standardna_dim:
        kakovost = preveri_kakovost_izreza(slika_rgb, aktivna_oglisca)
        if kakovost["razmerje"] < 2.7 or kakovost["razmerje"] > 4.3:
            st.warning(
                f"Razmerje stranic izreza je **{kakovost['razmerje']} : 1** "
                f"(standardni listič 76 × 22 mm ima razmerje **3,45 : 1**). "
                f"Če je izrezan le del lističa, izklopi standardne dimenzije ali popravi izrez."
            )
        if kakovost["px_na_mm"] < 15.0:
            st.info(
                f"Izvirna ločljivost izrezanega lističa je nizka (**{kakovost['px_na_mm']} px/mm**). "
                f"Najdrobnejši odtisi okoli 50 μm morda niso fizično razločljivi."
            )

    maska_kapljic, st_odtisov, odtisi_cm2, pokritost = analiziraj_kapljice(
        izrezan_listic,
        standardna_dimenzija=standardna_dim,
        min_velikost_px=min_velikost_px,
        obcutljivost=obcutljivost,
        kljuc_izreza=kljuc_izreza,
    )

    kljuc_prikaza = f"{kljuc_izreza}_{standardna_dim}_{min_velikost_px}_{obcutljivost}"
    slika_prikaz_rgb, maska_prikaz = pripravi_podlago_za_prikaz(
        izrezan_listic, maska_kapljic, kljuc_prikaza
    )

    st.divider()

    # --- PRIKAZ IZREZANEGA LISTIČA IN REZULTATOV ---
    prikaz_kol, rezultati_kol = st.columns([3, 2])

    with prikaz_kol:
        prikazi_interaktivni_prekriv(slika_prikaz_rgb, maska_prikaz)

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

        if st.button("💾 Shrani v preglednico", width="stretch", type="primary"):
            df = st.session_state.zadnja_tabela.copy()
            for stolpec in OSNOVNI_STOLPCI:
                if stolpec not in df.columns:
                    df[stolpec] = np.nan

            nova_vrstica = {
                "Ime lističa": osnovno_ime,
                "Število odtisov": int(st_odtisov),
                "Odtisi / cm²": float(odtisi_cm2) if (standardna_dim and odtisi_cm2 is not None) else np.nan,
                "Pokritost (%)": float(pokritost),
            }
            for k, v in izbrani_parametri.items():
                nova_vrstica[k] = v

            if not df.empty and osnovno_ime in df["Ime lističa"].values:
                idx = df.index[df["Ime lističa"] == osnovno_ime][0]
                # Počistimo morebitne stare parametre leta, ki pri novem shranjevanju niso izbrani
                for param in PRIVZETI_PARAMETRI_LETA:
                    if param in df.columns and param not in izbrani_parametri:
                        df.loc[idx, param] = np.nan
                for k, v in nova_vrstica.items():
                    if k not in df.columns:
                        df[k] = np.nan
                    df.loc[idx, k] = v
            else:
                df = pd.concat([df, pd.DataFrame([nova_vrstica])], ignore_index=True)

            st.session_state.rezultati_baza = df
            st.session_state.zadnja_tabela = df.copy()
            st.session_state.tabela_verzija += 1
            st.rerun()

# --- SPODNJA UREJEVALNA PREGLEDNICA IN IZVOZ V EXCEL ---
if not st.session_state.rezultati_baza.empty:
    st.divider()
    urejena_tabela = st.data_editor(
        st.session_state.rezultati_baza,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        key=f"urejevalnik_rezultatov_{st.session_state.tabela_verzija}",
    )
    st.session_state.zadnja_tabela = urejena_tabela

    if not urejena_tabela.empty:
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            urejena_tabela.to_excel(writer, index=False, sheet_name="Rezultati")

        st.download_button(
            label="📥 Izvozi v Excel (.xlsx)",
            data=excel_buffer.getvalue(),
            file_name="Analiza_listicev.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )