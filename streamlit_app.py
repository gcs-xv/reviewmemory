import io
import re
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import pdfplumber
from docx import Document
from supabase import create_client

st.set_page_config(page_title="Review Pasien — Streamlit", page_icon="🦷", layout="wide")

# ===== DPJP Canon =====
DPJP_CANON = [
    "Dr. drg. Andi Tajrin, M.Kes., Sp.B.M.M., Subsp. C.O.M.(K)",
    "drg. Mukhtar Nur Anam, Sp.B.M.M.",
    "drg. Husnul Basyar, Sp. B.M.M.",
    "drg. Abul Fauzi, Sp.B.M.M., Subsp.T.M.T.M.J.(K)",
    "drg. M. Irfan Rasul, Ph.D., Sp.B.M.M., Subsp.C.O.M.(K)",
    "drg. Mohammad Gazali, MARS., Sp.B.M.M., Subsp.T.M.T.M.J.(K)",
    "drg. Timurwati, Sp.B.M.M.",
    "drg. Husni Mubarak, Sp. B.M.M.",
    "drg. Nurwahida, M.K.G., Sp.B.M.M., Subsp.C.O.M(K)",
    "drg. Hadira, M.K.G., Sp.B.M.M., Subsp.C.O.M(K)",
    "drg. Carolina Stevanie, Sp.B.M.M.",
    "drg. Yossy Yoanita Ariestiana, M.KG., Sp.B.M.M., Subsp.Ortognat-D (K)",
]

# ===== Fuzzy DPJP =====
STOP_TOKENS = {
    "DR", "DRG", "SP", "B", "M", "K",
    "BMM", "MARS", "MKES", "MKG", "PHD",
    "SUBSP", "C", "O", "TMTMJ", "TMJ", "ORTOGNAT"
}

def _norm_doctor(s: str) -> str:
    if not s:
        return ""
    s = s.replace("drg..", "drg.")
    s = re.sub(r"Sp\.\s*BM\b", "Sp.BM", s, flags=re.IGNORECASE)
    s = re.sub(r"Sp\.BM\(?K\)?",  "Sp.BMM", s, flags=re.IGNORECASE)
    s = re.sub(r"Sp\.BMM\(?K\)?", "Sp.BMM", s, flags=re.IGNORECASE)
    s = s.upper()
    s = re.sub(r"[^A-Z]+", " ", s)
    s = re.sub(r"\bBMM\b", "B M M", s)
    s = re.sub(r"\bBM\b",  "B M", s)
    return " ".join(s.split())

def _tokens(s: str) -> set[str]:
    return set(_norm_doctor(s).split())

def _score_doctor(raw: str, canon: str):
    ta, tb = _tokens(raw), _tokens(canon)
    if not ta or not tb:
        return 0.0, 0
    na, nb = ta - STOP_TOKENS, tb - STOP_TOKENS
    inter_n = na & nb
    sn = (len(inter_n) / len(na | nb)) if (na and nb) else 0.0
    sa = len(ta & tb) / len(ta | tb)
    return 0.85 * sn + 0.15 * sa, len(inter_n)

def map_doctor_to_canonical(raw: str, candidates=DPJP_CANON, threshold: float = 0.35) -> str:
    best, best_score = "", 0.0
    for c in candidates:
        sc, inter_name_cnt = _score_doctor(raw, c)
        if inter_name_cnt == 0:
            continue
        if sc > best_score:
            best, best_score = c, sc
    return best if best_score >= threshold else ""

# --- NEW: paksa 'drg.' selalu huruf kecil tanpa mengubah 'Dr.' ---
def _fix_drg_lower(s: str) -> str:
    if not s:
        return s
    return re.sub(r'(?i)\bDRG\.', 'drg.', s)

# ===== Helpers =====
ID_MONTHS = {
    "JANUARI": 1, "FEBRUARI": 2, "MARET": 3, "APRIL": 4, "MEI": 5, "JUNI": 6,
    "JULI": 7, "AGUSTUS": 8, "SEPTEMBER": 9, "OKTOBER": 10, "NOVEMBER": 11, "DESEMBER": 12
}
ROMAN = {"I":1,"V":5,"X":10,"L":50,"C":100}
HARI_ID = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]

def roman_to_int(s: str) -> int:
    s = re.sub(r"[^IVXLC]", "", s.upper())
    if not s: return 0
    total = 0
    prev = 0
    for ch in reversed(s):
        val = ROMAN.get(ch, 0)
        if val < prev: total -= val
        else:
            total += val; prev = val
    return total

def fmt_rm(rm: str) -> str:
    digits = re.sub(r"\D", "", rm or "")
    digits = digits.zfill(6)[:6]
    return f"{digits[0:2]}.{digits[2:4]}.{digits[4:6]}"

def extract_period_date_from_text(text: str):
    m = re.search(r"PERIODE\s+(\d{1,2})\s+([A-Z]+)\s+(\d{4})", text.upper())
    if not m: return None
    d = int(m.group(1)); mon_name = m.group(2).strip(); y = int(m.group(3))
    mon = ID_MONTHS.get(mon_name)
    if not mon: return None
    try:
        return date(y, mon, d)
    except Exception:
        return None

def replace_gigi(text: str, gigi: str | None) -> str:
    if not (gigi and str(gigi).strip()):
        return text
    gigi_val = str(gigi).strip()
    # penting: gunakan \b asli + lambda agar \1 tetap capture group
    return re.sub(r"(?i)(\bgigi\s*)xx\b", lambda m: m.group(1) + gigi_val, text)
    
# ======= Logika impaksi/odontektomi hanya untuk gigi berakhiran 8 =======

def is_impaksi_tooth(gigi: str | None) -> bool:
    if not gigi:
        return False
    s = re.sub(r"\D", "", str(gigi))
    return bool(re.fullmatch(r"\d{2}", s)) and s.endswith("8")

def _clean_slash_choices(txt: str, rm_impaksi_odonto: bool) -> str:
    if not txt:
        return txt
    parts = [p.strip() for p in re.split(r"\s*/\s*", txt)]
    if rm_impaksi_odonto:
        parts = [p for p in parts if not re.search(r"(?i)\bimpaksi\b|\bodontektomi\b", p)]
    parts = [re.sub(r"\s{2,}", " ", p).strip() for p in parts if p.strip()]
    return " / ".join(parts)

def filter_for_tooth(diagnosa: str, tindakan: list[str], kontrol: str, gigi: str | None):
    imp = is_impaksi_tooth(gigi)
    diagnosa = _clean_slash_choices(diagnosa, rm_impaksi_odonto=not imp)
    kontrol  = _clean_slash_choices(kontrol,  rm_impaksi_odonto=not imp)
    if not imp:
        tindakan = [
            t for t in tindakan
            if not re.search(r"(?i)\bodontektomi\b|\bopen\s+methode\b", t)
        ]
    return diagnosa, tindakan, kontrol


def compute_kontrol_text(kontrol_tpl: str, diagnosa_text: str, base_date):
    if not base_date: return kontrol_tpl
    mk = re.search(r"\bPOD\s+([IVXLC]+)\b", kontrol_tpl, flags=re.IGNORECASE)
    md = re.search(r"\bPOD\s+([IVXLC]+)\b", diagnosa_text or "", flags=re.IGNORECASE)
    if not mk: return kontrol_tpl

    pod_k = roman_to_int(mk.group(1))
    pod_d = roman_to_int(md.group(1)) if md else 0

    offset = pod_k - pod_d
    if offset < 0: offset = 0
    target = base_date + timedelta(days=offset)

    if pod_k == 3 and target.weekday() == 6:  # Sunday
        pod_k = 4
        target = target + timedelta(days=1)
        kontrol_tpl = re.sub(r"\bPOD\s+[IVXLC]+\b", "POD IV", kontrol_tpl, flags=re.IGNORECASE)

    date_str = target.strftime("%d/%m/%Y")
    if re.search(r"\([^)]*\)", kontrol_tpl):
        return re.sub(r"\([^)]*\)", f"({date_str})", kontrol_tpl)
    else:
        return f"{kontrol_tpl} ({date_str})"

# ===== Templates =====
B = "•⁠  ⁠"
LABELS = {
    "nama": "Nama            : ",
    "tgl":  f"{B}Tanggal lahir  : ",
    "rm":   f"{B}RM                   : ",
    "diag": f"{B}Diagnosa        : ",
    "tind": f"{B}Tindakan        : ",
    "kont": f"{B}Kontrol           : ",
    "dpjp": f"{B}DPJP               : ",
    "telp": f"{B}No. Telp.         : ",
    "opr":  f"{B}Operator         : ",
}
VISIT_TEMPLATES = {
    "(Pilih)": dict(diagnosa="", tindakan=[], kontrol=""),
    "Kunjungan 1": dict(
        diagnosa="Gangren pulpa gigi xx / Gangren radiks gigi xx",
        tindakan=[
            "Ekstraksi gigi xx dalam lokal anestesi",
            "Periapikal X-ray gigi xx / OPG X-Ray",
            "Odontektomi gigi xx dalam lokal anestesi",
        ],
        kontrol="POD III (xx/04/2025)",
    ),
    "Kunjungan 2": dict(
        diagnosa="Impaksi gigi xx kelas xx posisi xx Mesioangular / Gangren pulpa gigi xx / Gangren radiks gigi xx",
        tindakan=[
            "Odontektomi gigi xx dalam lokal anestesi",
            "ekstraksi gigi xx dengan penyulit dalam lokal anestesi",
            "ekstraksi gigi xx dengan open methode dalam lokal anestesi",
        ],
        kontrol="POD III (xx/04/2025)",
    ),
    "Kunjungan 3": dict(
        diagnosa="POD III Ekstraksi gigi xx dalam lokal anestesi / POD III Odontektomi gigi xx dalam lokal anestesi",
        tindakan=["Cuci luka intraoral dengan NaCl 0,9%"],
        kontrol="POD VII (xx/04/2025)",
    ),
    "Kunjungan 4": dict(
        diagnosa="POD VII Odontektomi gigi xx dalam lokal anestesi / POD VII Ekstraksi gigi xx dalam lokal anestesi",
        tindakan=["Cuci luka intra oral dengan NaCl 0,9%", "Aff hecting"],
        kontrol="POD XIV (xx/04/2025)",
    ),
    "Kunjungan 5": dict(
        diagnosa="POD XIV Ekstraksi gigi xx dalam lokal anestesi / POD XIV Odontektomi gigi xx dalam lokal anestesi",
        tindakan=["Kontrol luka post operasi", "Rujuk balik FKTP"],
        kontrol="-",
    ),
}

def normalize_visit(text: str) -> str:
    t = (text or "").strip()
    if not t: return "(Pilih)"
    if t.isdigit() and t in {"1","2","3","4","5"}:
        return f"Kunjungan {t}"
    low = t.lower()
    for k in VISIT_TEMPLATES.keys():
        if low == k.lower():
            return k
    return t

# ===== Shared storage (Supabase) =====
@st.cache_resource
def get_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_ANON_KEY"]
    return create_client(url, key)

def load_review_map(supabase, periode_date: str | None):
    if not periode_date:
        return {}
    data = (
        supabase
        .table("reviews")
        .select("rm, checked, reviewed_by, updated_at")
        .eq("periode_date", periode_date)
        .execute()
        .data or []
    )
    return {row["rm"]: row for row in data}

def upsert_reviews(supabase, periode_date: str, rows_to_upsert):
    payload = []
    for r in rows_to_upsert:
        payload.append({
            "periode_date": periode_date,
            "rm": str(r["rm"]),
            "checked": bool(r["checked"]),
            "reviewed_by": r.get("reviewed_by"),
        })
    if payload:
        supabase.table("reviews").upsert(payload, on_conflict="periode_date,rm").execute()

# ===== Block builder (with meta) =====

def build_block_with_meta(no, row, visit_key, base_date):
    tpl_key = normalize_visit(visit_key or row.get("visit") or "(Pilih)")
    tpl = VISIT_TEMPLATES.get(tpl_key, VISIT_TEMPLATES["(Pilih)"])
    diagnosa = tpl["diagnosa"]
    tindakan = list(tpl["tindakan"])
    kontrol  = tpl["kontrol"]

    # Kunjungan 1 override: diagnosa kosong, tindakan konsultasi + x-ray, kontrol = H+7 dari HARI INI,
# pakai 'ekstraksi' untuk non-impaksi, 'odontektomi' untuk impaksi
if tpl_key == "Kunjungan 1":
    imp = is_impaksi_tooth((row.get("gigi") or "").strip())
    tindakan = [
        "Konsultasi",
        f"Periapikal X-ray gigi { (row.get('gigi') or 'xx').strip() } / OPG X-Ray",
    ]
    diagnosa = ""  # sesuai spes kamu
    hplus = (date.today() + timedelta(days=7)).strftime("%d/%m/%Y")
    op_lower = "odontektomi" if imp else "ekstraksi"
    kontrol  = f"Pro {op_lower} gigi { (row.get('gigi') or 'xx').strip() } dalam lokal anestesi ({hplus})"

    gigi = (row.get("gigi") or "").strip()

    # 1) Isi 'xx' → angka gigi
    diagnosa = replace_gigi(diagnosa, gigi)
    tindakan = [replace_gigi(t, gigi) for t in tindakan]
    kontrol  = replace_gigi(kontrol,  gigi)

    # 2) Filter impaksi/odontektomi sesuai nomor gigi
    diagnosa, tindakan, kontrol = filter_for_tooth(diagnosa, tindakan, kontrol, gigi)

    # 3) Hitung tanggal kontrol dari POD
    kontrol = compute_kontrol_text(kontrol, diagnosa, base_date)

    dpjp_full = _fix_drg_lower((row.get("DPJP (auto)") or "").strip())
    telp = (row.get("telp") or "").strip()
    operator = _operator_prefixed((row.get("operator") or "").strip()) if (row.get("operator") or "").strip() else ""

    L = LABELS
    lines = []
    lines.append(f"{no}. {L['nama']}{row['Nama']}")
    lines.append(f"{L['tgl']}{row['Tgl Lahir']}")
    lines.append(f"{L['rm']}{fmt_rm(row['No. RM'])}")
    lines.append(f"{L['diag']}{diagnosa}")

    if tpl_key == "Kunjungan 3" and len(tindakan) == 1:
        lines.append(f"{L['tind']}{tindakan[0]}")
    else:
        lines.append(f"{L['tind']}")
        for t in tindakan:
            lines.append(f"    * {t}")

    lines.append(f"{L['kont']}{kontrol}")
    lines.append(f"{L['dpjp']}{dpjp_full}")
    lines.append(f"{L['telp']}{telp}")
    lines.append(f"{L['opr']}{operator}")

    konsul_flag = any(re.search(r"(?i)\bkonsultasi\b|\bkonsul\b", t) for t in tindakan)

        return "\n".join(lines), tindakan, konsul_flag

# ===== PDF Parser =====

def parse_pdf_to_rows_and_period_bytes(pdf_bytes: bytes):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = ""
        for p in pdf.pages:
            txt = p.extract_text() or ""
            full_text += txt + "\n"

    period_date = extract_period_date_from_text(full_text)

    pat = re.compile(
        r"(?P<rm>\d{5,6})\s+"
        r"(?P<nopen>\d{8,18})\s+"
        r"(?P<name>.+?)\s+"
        r"(?P<sex>[LP])(?=\s+[0-3]\d-\d{2}-\d{4})\s+"
        r"(?P<dob>[0-3]\d-\d{2}-\d{4})",
        re.DOTALL
    )
    matches = list(pat.finditer(full_text))

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        block = full_text[start:end]

        raw_name = m.group("name")
        name_clean = re.sub(r"[ \t\r\f\v]+", " ", raw_name).replace("\n", " ")
        name_clean = re.sub(r"\s{2,}", " ", name_clean).strip().title()

        doc_m = re.search(r"(drg\.?\.?\s*[^\n]+)", block, flags=re.IGNORECASE)
        dokter_raw = doc_m.group(1).strip() if doc_m else ""

        dokter_raw = re.split(
            r"\s\d{2}:\d{2}:\d{2}"
            r"|ANJUNGAN(?:\s+MANDIRI)?"
            r"|KLINIK"
            r"|BELUM"
            r"|,00|\.00|,0"
            r"|MELIZAH"
            r"|NAMIRA(?:\s+ANJANI)?"
            r"|NUR\s+AMRAENI(?:\s+LATIF)?"
            r"|DEWI(?:\s+SARTIKA)?"
            r"|NURHIDAYA",
            dokter_raw,
            1,
            flags=re.IGNORECASE
        )[0].strip()

        dokter_raw = dokter_raw.rstrip(",;")

        dpjp_auto = _fix_drg_lower(map_doctor_to_canonical(dokter_raw))

        rows.append({
            "No. RM": m.group("rm"),
            "Nama": name_clean,
            "Tgl Lahir": m.group("dob").replace("-", "/"),
            "DPJP (auto)": dpjp_auto,
            "checked": False,
            "visit": "(Pilih)",
            "gigi": "",
            "telp": "",
            "operator": "",
        })

    # Dedup
    uniq = {}
    for r in rows:
        key = (r["No. RM"], r["Nama"], r["Tgl Lahir"])
        if key not in uniq:
            uniq[key] = r
    final = list(uniq.values())

    out = []
    for i, r in enumerate(final, start=1):
        rr = dict(r)
        rr["No."] = i
        out.append(rr)

    return out, period_date

# ===== UI =====

st.title("🦷 Review Pasien (Ported) — Streamlit")
st.caption("Porting dari app Tkinter: parsing PDF → tabel → template WA + shared memory (Supabase, keyed by PERIODE)")

with st.expander("Catatan & aturan format", expanded=False):
    st.markdown(
        "- **Nama multi-baris utuh**, NOPEN 8–18 digit, RM → `XX.XX.XX`.\n"
        "- **PERIODE** dipakai sebagai base tanggal kontrol & rekap harian, **dan kunci shared memory**.\n"
        "- Kunjungan 3: **Tindakan** satu baris (tanpa bullet).\n"
        "- Kontrol otomatis dari **POD** (dengan aturan Minggu → POD IV / +1 hari).\n"
        "- Status review disimpan bersama via Supabase berdasarkan **tanggal PERIODE + RM** (bukan nama file).\n"
    )

uploaded = st.file_uploader("Upload PDF laporan", type=["pdf"])

@st.cache_data(show_spinner=False)
def _parse_cached(pdf_bytes: bytes):
    return parse_pdf_to_rows_and_period_bytes(pdf_bytes)

if uploaded is not None:
    data = uploaded.read()
    try:
        rows, period_date = _parse_cached(data)
    except Exception as e:
        st.error(f"Gagal membaca PDF: {e}")
        st.stop()

    if not rows:
        st.error("PDF tidak terbaca / pola tidak cocok.")
        st.stop()

    # Tanggal PERIODE → kunci bersama
    per_date = period_date if period_date else date.today()
    hari_str = HARI_ID[per_date.weekday()]
    per_str_show = per_date.strftime("%d/%m/%Y")
    per_str_db = per_date.strftime("%Y-%m-%d")

    st.success(f"Ditemukan {len(rows)} pasien — PERIODE: **{per_str_show}** — file: **{uploaded.name}**")

    # ===== Shared review status load (keyed by PERIODE only) =====
    supabase = get_supabase()
    reviewer = st.text_input("Nama reviewer (opsional)")

    df = pd.DataFrame(rows, columns=["No.","Nama","Tgl Lahir","No. RM","DPJP (auto)","visit","gigi","telp","operator","checked"])

    review_map = load_review_map(supabase, per_str_db)
    if review_map:
        df["checked"] = df["No. RM"].astype(str).map(lambda rm: bool(review_map.get(rm, {}).get("checked", False)))

    st.markdown("### Tabel pasien (editable)")

    # Centang semua
    select_all = st.checkbox("Centang semua baris", value=False)
    if select_all:
        df["checked"] = True

    edited = st.data_editor(
        df,
        column_config={
            "checked": st.column_config.CheckboxColumn("✓"),
            "visit": st.column_config.TextColumn("Kunjungan"),
            "gigi": st.column_config.TextColumn("Gigi"),
            "telp": st.column_config.TextColumn("Telp"),
            "operator": st.column_config.TextColumn("Operator"),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        height=380,
    )

    # Simpan perubahan status review (shared by date)
    def current_checked_in_db(rm):
        return bool(review_map.get(str(rm), {}).get("checked", False))

    changed = []
    for _, r in edited.iterrows():
        rm = str(r["No. RM"])
        new_checked = bool(r["checked"])
        old_checked = current_checked_in_db(rm)
        if new_checked != old_checked:
            changed.append({
                "rm": rm,
                "checked": new_checked,
                "reviewed_by": (reviewer or "").strip() or None
            })

    if st.button("💾 Simpan status review (sync)", use_container_width=True, type="primary"):
        try:
            upsert_reviews(supabase, per_str_db, changed)
            st.success(f"Status {len(changed)} pasien tersimpan untuk tanggal {per_str_show} & terbagi ke semua user.")
            st.experimental_rerun()
        except Exception as e:
            st.error(f"Gagal menyimpan: {e}")

    # Build preview blocks + hitung rekap
    sel = edited[edited["checked"] == True].copy().sort_values("No.")
    preview_blocks = []
    konsultasi_count = 0

    if not sel.empty:
        st.markdown("### Blok per pasien (hijau = sudah direview)")
        GREEN = "background-color:#e8f5e9;border:1px solid #2e7d32;border-radius:10px;padding:12px"
        GRAY  = "background-color:#f5f5f5;border:1px solid #ddd;border-radius:10px;padding:12px"

        for _, r in sel.iterrows():
            rdict = {
                "Nama": r["Nama"],
                "Tgl Lahir": r["Tgl Lahir"],
                "No. RM": r["No. RM"],
                "DPJP (auto)": r["DPJP (auto)"],
                "visit": r["visit"],
                "gigi": r["gigi"],
                "telp": r["telp"],
                "operator": r["operator"],
            }
            block, tind_list, konsul_flag = build_block_with_meta(int(r["No."]), rdict, r["visit"], per_date)

            # aturan reviewed: ada kunjungan + gigi + (telp atau operator)
            reviewed = (
                str(r["visit"]).lower().startswith("kunjungan")
                and str(r["gigi"]).strip() != ""
                and (str(r["telp"]).strip() != "" or str(r["operator"]).strip() != "")
            )
            wrap = GREEN if reviewed else GRAY

            st.markdown(f'<div style="{wrap}"><pre style="white-space:pre-wrap">{block}</pre></div>', unsafe_allow_html=True)
            st.markdown("")

            preview_blocks.append(block)
            if konsul_flag or re.search(r"(?i)\bkonsultasi\b|\bkonsul\b", block):
                konsultasi_count += 1
                
    total_reviewed = len(preview_blocks)
    tindakan_count = max(total_reviewed - konsultasi_count, 0)

    # ===== Compose final text with frozen spacing (plain text) =====
    header_lines = [
        "Review jumlah pasien Poli Bedah Mulut dan Maksilofasial RSGMP UNHAS, ",
        f"{hari_str}, {per_str_show}",
        "",
        f"Jumlah pasien     : {total_reviewed} Pasien",
        f"Tindakan              : {tindakan_count} Pasien",
        f"Konsultasi\t      : {konsultasi_count} Pasien",
        f"Terjaring GA\t      : xx Pasien",
        f"Baksos                 : xx Pasien",
        f"VIP                        : -",
        "",
        "-----------------------------------------------------",
        "",
        "POLI INTEGRASI",
        "",
    ]

    body_text = "\n\n".join(preview_blocks) if preview_blocks else ""

    footer_lines = [
        "",
        "------------------------------------------------------",
        "",
        f"{hari_str}, {per_str_show}",
        "",
        "Chief jaga poli :",
        "drg. xx",
    ]

    final_text = "\n".join(header_lines) + body_text + ("\n" + "\n".join(footer_lines) if body_text else "\n" + "\n".join(footer_lines))

    col1, col2 = st.columns([3,2], gap="large")
    with col1:
        st.markdown("### Preview teks final (format beku)")
        st.text_area("Teks hasil", final_text, height=420)

        if final_text.strip():
            # Download TXT (UTF-8)
            st.download_button(
                "⬇️ Download TXT",
                data=final_text.encode("utf-8"),
                file_name="laporan_pasien.txt",
                mime="text/plain",
                use_container_width=True
            )

            # Download DOCX (pakai monospace supaya spasi tidak berubah)
            buf = io.BytesIO()
            doc = Document()
            style = doc.styles["Normal"]
            style.font.name = "Courier New"
            for part in final_text.split("\n"):
                doc.add_paragraph(part)
            doc.save(buf)
            st.download_button(
                "⬇️ Download DOCX",
                data=buf.getvalue(),
                file_name="laporan_pasien.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True
            )

    with col2:
        st.markdown("### Opsi")
        st.write("- Ubah nilai **Kunjungan/Gigi/Telp/Operator** langsung di tabel.")
        st.write("- **Centang** pasien yang sudah direview, lalu klik **Simpan status review (sync)** agar tersimpan bersama per tanggal.")
        st.write("- DPJP terisi otomatis via *fuzzy mapping* dari PDF.")

st.divider()
st.caption("Made for Streamlit Cloud — pdfplumber + python-docx + Supabase shared state (keyed by PERIODE)")
