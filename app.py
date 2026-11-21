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
if "zadnji_kamera_id" not in st.session_state:
    st.session_state.zadnji_kamera_id = None
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
        key="nalagalnik_galerija",
    )
with zav2:
    st.markdown('<span id="kamera-uploader-oznaka"></span>', unsafe_allow_html=True)
    slika_kamere = st.file_uploader(
        "Slikaj listič s kamero telefona",
        type=["jpg", "jpeg", "png", "bmp", "webp"],
        accept_multiple_files=False,
        key="nalagalnik_kamera",
    )
    # Skript doda atribut capture="environment" samo nalagalniku v zavihku Kamera,
    # da telefon neposredno odpre privzeto kamero in sliko zanesljivo prenese prek HTTP.
    components.html(
        """
        <script>
        (function() {
          try {
            const doc = window.parent.document;
            function nastaviKamero() {
              const oznaka = doc.getElementById("kamera-uploader-oznaka");
              if (!oznaka) return;
              const panel = oznaka.closest('[role="tabpanel"]') || oznaka.closest('[data-testid="stVerticalBlock"]');
              if (!panel) return;
              const vhod = panel.querySelector('input[type="file"]');
              if (vhod) {
                if (vhod.getAttribute("accept") !== "image/*") {
                  vhod.setAttribute("accept", "image/*");
                }
                if (vhod.getAttribute("capture") !== "environment") {
                  vhod.setAttribute("capture", "environment");
                }
                if (vhod.hasAttribute("multiple")) {
                  vhod.removeAttribute("multiple");
                }
              }
            }
            nastaviKamero();
            if (window.parent.__kameraObserver) {
              window.parent.__kameraObserver.disconnect();
            }
            const obs = new MutationObserver(nastaviKamero);
            obs.observe(doc.body, { childList: true, subtree: true });
            window.parent.__kameraObserver = obs;
          } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )

    if slika_kamere is not None:
        trenutni_id = getattr(
            slika_kamere, "file_id", f"{slika_kamere.name}_{slika_kamere.size}"
        )
        if trenutni_id != st.session_state.zadnji_kamera_id:
            st.session_state.zadnji_kamera_id = trenutni_id
            st.session_state.stevec_kamere += 1
            ime_slike = f"Kamera_listic_{st.session_state.stevec_kamere}.jpg"
            st.session_state.kamera_slike[ime_slike] = slika_kamere.getvalue()
            st.session_state.izbrani_listic_select = ime_slike

    if st.session_state.kamera_slike:
        st.caption(f"Zajetih fotografij iz kamere: **{len(st.session_state.kamera_slike)}**")
        if st.button("🗑️ Počisti slike iz kamere"):
            st.session_state.kamera_slike = {}
            st.session_state.zadnji_kamera_id = None
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