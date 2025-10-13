# streamlit_app.py
import io
import re
from datetime import date, timedelta

import streamlit as st
import pandas as pd
import pdfplumber
from docx import Document
from supabase import create_client

# =========================================================
# Page
# =========================================================
st.set_page_config(page_title="Review Pasien — Streamlit", page_icon="🦷", layout="wide")

# =========================================================
# DPJP Canon + Fuzzy Matching (tetap)
# =========================================================
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

STOP_TOKENS = {
    "DR", "DRG", "SP", "B", "M", "K",
    "BMM", "MARS", "MKES", "MKG", "PHD",
    "SUBSP", "C", "O", "TMTMJ", "TMJ", "ORTOGNAT"
}

def _norm_doctor(s: str) -> str:
    if not s: return ""
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
    if not ta or not tb: return 0.0, 0
    na, nb = ta - STOP_TOKENS, tb - STOP_TOKENS
    inter_n = na & nb
    sn = (len(inter_n) / len(na | nb)) if (na and nb) else 0.0
    sa = len(ta & tb) / len(ta | tb)
    return 0.85 * sn + 0.15 * sa, len(inter_n)

def map_doctor_to_canonical(raw: str, candidates=DPJP_CANON, threshold: float = 0.35) -> str:
    best, best_score = "", 0.0
    for c in candidates:
        sc, inter_name_cnt = _score_doctor(raw, c)
        if inter_name_cnt == 0: continue
        if sc > best_score:
            best, best_score = c, sc
    return best if best_score >= threshold else ""

def _fix_drg_lower(s: str) -> str:
    if not s: return s
    return re.sub(r'(?i)\bDRG\.', 'drg.', s)

# =========================================================
# Helpers (tetap)
# =========================================================
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
    if not (gigi and str(gigi).strip()): return text
    gigi_val = str(gigi).strip()
    # Hindari PatternError: gunakan lambda agar \1 tidak bentrok dengan angka gigi
    return re.sub(r"(?i)(\bgigi\s*)xx\b", lambda m: m.group(1) + gigi_val, text)

def is_impaksi_tooth(gigi: str | None) -> bool:
    if not gigi: return False
    s = re.sub(r"\D", "", str(gigi))
    return bool(re.fullmatch(r"\d{2}", s)) and s.endswith("8")

def _clean_slash_choices(txt: str, rm_impaksi_odonto: bool) -> str:
    if not txt: return txt
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
    if not base_date:
        return kontrol_tpl

    mk = re.search(r"\bPOD\s+([IVXLC]+)\b", kontrol_tpl, flags=re.IGNORECASE)
    md = re.search(r"\bPOD\s+([IVXLC]+)\b", diagnosa_text or "", flags=re.IGNORECASE)
    if not mk:
        return kontrol_tpl

    pod_k = roman_to_int(mk.group(1))
    pod_d = roman_to_int(md.group(1)) if md else 0

    offset = pod_k - pod_d
    if offset < 0:
        offset = 0
    target = base_date + timedelta(days=offset)

    # kalau target POD III jatuh Minggu → geser ke POD IV (senin)
    if pod_k == 3 and target.weekday() == 6:  # Sunday
        pod_k = 4
        target = target + timedelta(days=1)
        kontrol_tpl = re.sub(r"\bPOD\s+[IVXLC]+\b", "POD IV", kontrol_tpl, flags=re.IGNORECASE)

    date_str = target.strftime("%d/%m/%Y")
    if re.search(r"\([^)]*\)", kontrol_tpl):
        return re.sub(r"\([^)]*\)", f"({date_str})", kontrol_tpl)
    else:
        return f"{kontrol_tpl} ({date_str})"


# --- Auto-prefix "drg. " untuk operator (tanpa dobel kalau user sudah tulis dr./drg.) ---
def _operator_prefixed(op_name: str) -> str:
    s = (op_name or "").strip()
    if not s:
        return ""
    if re.match(r"(?i)\s*(dr\.?\s*)?drg\.", s) or re.match(r"(?i)^dr\.", s):
        return re.sub(r'(?i)\bDRG\.', 'drg.', s)
    return f"drg. {s}"

# =========================================================
# Template dasar (dipakai fallback)
# =========================================================
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
    # Kunjungan 1: diagnosa kosong; kontrol H+7 (nanti dihitung di builder)
    "Kunjungan 1": dict(
        diagnosa="",
        tindakan=[
            "Konsultasi",
            "Periapikal X-ray gigi xx / OPG X-Ray",
        ],
        kontrol="Pro ekstraksi gigi xx dalam lokal anestesi / Pro odontektomi gigi xx dalam lokal anestesi (xx/04/2025)",
    ),
    "Kunjungan 2": dict(
        diagnosa="Impaksi gigi xx kelas xx posisi xx Mesioangular / Gangren pulpa gigi xx / Gangren radiks gigi xx",
        tindakan=[
            "Odontektomi gigi xx dalam lokal anestesi",
            "Ekstraksi gigi xx dalam lokal anestesi",
        ],
        kontrol="POD IV (xx/04/2025)",
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

def load_review_map(supabase, periode_date: str):
    """Ambil semua kolom yang ada (agar aman walau DB belum ditambah kolom baru)."""
    try:
        res = (
            supabase.table("reviews")
            .select("*")
            .eq("periode_date", periode_date)
            .execute()
        )
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else []) or []
        # kembalikan dict by RM
        return {str(row.get("rm")): row for row in data}
    except Exception:
        return {}

def upsert_reviews(supabase, periode_date: str, file_name: str, rows_to_upsert: list[dict]):
    """Simpan status & block_text per RM (upsert by (periode_date, rm)).

    Akan mencoba menyimpan kolom lengkap. Jika DB belum punya kolom tsb, fallback ke kolom minimal.
    """
    if not rows_to_upsert:
        return

    # payload lengkap
    full_payload = []
    for r in rows_to_upsert:
        full_payload.append({
            "periode_date": periode_date,
            "file_name": file_name,
            "rm": str(r["rm"]),
            "checked": bool(r.get("checked", True)),
            "reviewed_by": r.get("reviewed_by"),
            "block_text": r.get("block_text"),
            "visit": r.get("visit"),
            "gigi": r.get("gigi"),
            "telp": r.get("telp"),
            "operator": r.get("operator"),
        })

    try:
        supabase.table("reviews").upsert(full_payload, on_conflict="periode_date,rm").execute()
        return
    except Exception:
        # fallback ke kolom minimal yang pasti ada di skema user
        minimal_payload = []
        for r in rows_to_upsert:
            minimal_payload.append({
                "periode_date": periode_date,
                "file_name": file_name,
                "rm": str(r["rm"]),
                "checked": bool(r.get("checked", True)),
                "reviewed_by": r.get("reviewed_by"),
            })
        supabase.table("reviews").upsert(minimal_payload, on_conflict="periode_date,rm").execute()

def load_summary(supabase, periode_date: str) -> str:
    try:
        res = (
            supabase.table("daily_summaries")
            .select("summary_text")
            .eq("periode_date", periode_date)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else [])
        if not rows:
            return ""
        row = rows[0] if isinstance(rows, list) else rows
        return (row or {}).get("summary_text") or ""
    except Exception:
        return ""

def upsert_summary(supabase, periode_date: str, summary_text: str):
    try:
        supabase.table("daily_summaries").upsert(
            {"periode_date": periode_date, "summary_text": summary_text},
            on_conflict="periode_date"
        ).execute()
    except Exception:
        pass

# =========================================================
# Block Builder — logika baru impaksi / non-impaksi
# =========================================================
def build_block_with_meta(no, row, visit_key, base_date):
    """
    Kunjungan 1:
      - Diagnosa: kosong
      - Tindakan: Konsultasi + Periapikal X-ray gigi G / OPG
      - Kontrol : Pro <ekstraksi|odontektomi> gigi G (H+7 dari hari ini)
    Kunjungan 2:
      - Impaksi(..8): Diagnosa Impaksi..., Tindakan Odontektomi...
      - Non-impaksi : Diagnosa Gangren..., Tindakan Ekstraksi...
      - Kontrol POD IV (base PERIODE)
    Kunjungan 3/4/5:
      - Diagnosa = POD {III|VII|XIV} {Ekstraksi|Odontektomi} gigi G
      - Tindakan sesuai template; kontrol sesuai template (base PERIODE)
    """
    tpl_key = normalize_visit(visit_key or row.get("visit") or "(Pilih)")
    g_raw = (row.get("gigi") or "").strip()
    g_clean = re.sub(r"\D", "", g_raw)
    tooth = g_clean if g_clean else "xx"
    imp = is_impaksi_tooth(g_clean)
    op_word = "Odontektomi" if imp else "Ekstraksi"

    dpjp_full = _fix_drg_lower((row.get("DPJP (auto)") or "").strip())
    telp = (row.get("telp") or "").strip()
    operator_in = str(row.get("operator") or "").strip()
    operator = _operator_prefixed(operator_in)

    L = LABELS
    lines = []

    # >>> HAPUS judul "Kunjungan X": tidak ditampilkan sesuai permintaan
    # (jangan tambahkan baris tpl_key apa pun)

    lines.append(f"{no}. {L['nama']}{row['Nama']}")
    lines.append(f"{L['tgl']}{row['Tgl Lahir']}")
    lines.append(f"{L['rm']}{fmt_rm(row['No. RM'])}")

    tindakan_list = []
    diagnosa_txt = ""
    kontrol_txt = ""

    if tpl_key == "Kunjungan 1":
        diagnosa_txt = ""
        tindakan_list = [
            "Konsultasi",
            f"Periapikal X-ray gigi {tooth} / OPG X-Ray",
        ]
        # H+7 dari hari ini
        hplus = (date.today() + timedelta(days=7)).strftime("%d/%m/%Y")
        op_lower = "odontektomi" if imp else "ekstraksi"
        kontrol_txt = f"Pro {op_lower} gigi {tooth} dalam lokal anestesi ({hplus})"

    elif tpl_key == "Kunjungan 2":
        if imp:
            diagnosa_txt = f"Impaksi gigi {tooth} kelas xx posisi xx Mesioangular"
            tindakan_list = [f"Odontektomi gigi {tooth} dalam lokal anestesi"]
        else:
            diagnosa_txt = f"Gangren pulpa gigi {tooth} / Gangren radiks gigi {tooth}"
            tindakan_list = [f"Ekstraksi gigi {tooth} dalam lokal anestesi"]
        kontrol_txt = compute_kontrol_text("POD IV (xx/04/2025)", diagnosa_txt, base_date)

    elif tpl_key == "Kunjungan 3":
        diagnosa_txt = f"POD III {op_word} gigi {tooth} dalam lokal anestesi"
        tindakan_list = ["Cuci luka intraoral dengan NaCl 0,9%"]
        kontrol_txt = compute_kontrol_text("POD VII (xx/04/2025)", diagnosa_txt, base_date)

    elif tpl_key == "Kunjungan 4":
        diagnosa_txt = f"POD VII {op_word} gigi {tooth} dalam lokal anestesi"
        tindakan_list = ["Cuci luka intra oral dengan NaCl 0,9%", "Aff hecting"]
        kontrol_txt = compute_kontrol_text("POD XIV (xx/04/2025)", diagnosa_txt, base_date)

    elif tpl_key == "Kunjungan 5":
        diagnosa_txt = f"POD XIV {op_word} gigi {tooth} dalam lokal anestesi"
        tindakan_list = ["Kontrol luka post operasi", "Rujuk balik FKTP"]
        kontrol_txt = "-"

    else:
        # fallback ke template + filter impaksi utk non-8
        tpl = VISIT_TEMPLATES.get(tpl_key, VISIT_TEMPLATES["(Pilih)"])
        diagnosa = replace_gigi(tpl["diagnosa"], tooth)
        tlist = [replace_gigi(t, tooth) for t in tpl["tindakan"]]
        kontrol = replace_gigi(tpl["kontrol"], tooth)
        diagnosa, tlist, kontrol = filter_for_tooth(diagnosa, tlist, kontrol, tooth)
        diagnosa_txt = diagnosa
        tindakan_list = tlist
        kontrol_txt = compute_kontrol_text(kontrol, diagnosa_txt, base_date) if kontrol else ""

    lines.append(f"{L['diag']}{diagnosa_txt}")

    # Kunjungan 3: tindakan satu baris tanpa bullet
    if tpl_key == "Kunjungan 3" and len(tindakan_list) == 1:
        lines.append(f"{L['tind']}{tindakan_list[0]}")
    else:
        lines.append(f"{L['tind']}")
        for t in tindakan_list:
            lines.append(f"    * {t}")

    lines.append(f"{L['kont']}{kontrol_txt}")
    lines.append(f"{L['dpjp']}{dpjp_full}")
    lines.append(f"{L['telp']}{telp}")
    lines.append(f"{L['opr']}{operator}")

    konsul_flag = any(re.search(r"(?i)\bkonsultasi\b|\bkonsul\b", t) for t in tindakan_list)
    return "\n".join(lines), tindakan_list, konsul_flag
    
# =========================================================
# Parser PDF (tetap)
# =========================================================
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

# =========================================================
# UI — Per Pasien Block + Editor + Highlight Hijau
# =========================================================
st.title("🦷 Review Pasien — Per Pasien Block")
st.caption("Parsing PDF → blok per pasien (editable) → gabungan (format beku). Sinkronisasi lintas user: next step (Supabase).")

with st.expander("Catatan & aturan format", expanded=False):
    st.markdown(
        "- **Nama multi-baris utuh**, NOPEN 8–18 digit, RM → `XX.XX.XX`.\n"
        "- **PERIODE** dipakai sebagai base tanggal kontrol POD (kecuali Kunjungan 1 = H+7 dari hari ini).\n"
        "- Kunjungan 3: **Tindakan** satu baris (tanpa bullet).\n"
        "- Format spacing **beku** (dipertahankan saat copy/download)."
    )

uploaded = st.file_uploader("Upload PDF laporan", type=["pdf"])

# Persist uploaded PDF bytes across reruns (for auto-refresh)
if uploaded is not None:
    _file_bytes = uploaded.read()
    st.session_state["_uploaded_pdf"] = _file_bytes
    st.session_state["_uploaded_name"] = uploaded.name
uploaded_bytes = st.session_state.get("_uploaded_pdf")
uploaded_name = st.session_state.get("_uploaded_name", uploaded.name if uploaded is not None else "")


@st.cache_data(show_spinner=False)
def _parse_cached(pdf_bytes: bytes):
    return parse_pdf_to_rows_and_period_bytes(pdf_bytes)

# --- Autoflush flags & sidebar action flags ---
# (Used for always-visible sidebar buttons / auto-sync without external packages)
if "do_save_blocks" not in st.session_state:
    st.session_state.do_save_blocks = False
if "do_save_rekap" not in st.session_state:
    st.session_state.do_save_rekap = False

# Helper: Komputasi baris yang akan di-save dari st.session_state
def _compute_rows_to_save(all_rows, reviewer_name):
    rows_to_save = []
    for rr in pd.DataFrame(all_rows).sort_values("No.").to_dict("records"):
        rm_key = str(rr["No. RM"])
        st_state = st.session_state.per_patient.get(rm_key)
        if not st_state:
            continue
        reviewed_ok = (
            str(st_state["visit"]).lower().startswith("kunjungan")
            and str(st_state["gigi"]).strip() != ""
            and (str(st_state["telp"]).strip() != "" or str(st_state["operator"]).strip() != "")
        )
        if reviewed_ok and (st_state.get("block") or "").strip():
            rows_to_save.append({
                "rm": rm_key,
                "checked": True,
                "reviewed_by": (reviewer_name or None),
                "block_text": st_state["block"],
                "visit": st_state.get("visit"),
                "gigi": st_state.get("gigi"),
                "telp": st_state.get("telp"),
                "operator": st_state.get("operator"),
            })
    return rows_to_save

# state: per pasien (RM) simpan reviewed + teks editor
if "per_patient" not in st.session_state:
    st.session_state.per_patient = {}  # rm -> dict(state)

if uploaded_bytes is not None:
    data = uploaded_bytes
    uploaded_name = uploaded_name or (uploaded.name if uploaded is not None else "uploaded.pdf")
    try:
        rows, period_date = _parse_cached(data)
    except Exception as e:
        st.error(f"Gagal membaca PDF: {e}")
        st.stop()

    if not rows:
        st.error("PDF tidak terbaca / pola tidak cocok.")
        st.stop()

    per_date = period_date if period_date else date.today()
    hari_str = HARI_ID[per_date.weekday()]
    per_str_show = per_date.strftime("%d/%m/%Y")
    per_str_db = per_date.strftime("%Y-%m-%d")
    supabase = get_supabase()
    review_map = load_review_map(supabase, per_str_db)

    # ===== Sidebar actions (no auto-refresh) =====
    with st.sidebar:
        st.markdown("## ⚙️ Aksi")
        st.caption("Ingat: setelah mengisi, **klik Simpan**. Untuk menarik perubahan dari teman, klik **Update**.")

        if st.button("🔄 Update (ambil dari Supabase)", key="sb_pull", use_container_width=True):
            try:
                # ambil ulang dari DB dan merge ke state saat ini (tanpa kehilangan edit manual)
                latest_map = load_review_map(supabase, per_str_db)
                pulled = 0
                for rm_k, saved in latest_map.items():
                    st_state = st.session_state.per_patient.get(str(rm_k))
                    if not st_state:
                        continue
                    # hanya overwrite field kosong / belum disentuh, atau kalau ada block_text dari DB
                    if saved.get("visit") and not st_state.get("manually_touched", False):
                        st_state["visit"] = normalize_visit(saved.get("visit"))
                    if saved.get("gigi") and not st_state.get("manually_touched", False):
                        st_state["gigi"] = saved.get("gigi")
                    if saved.get("telp") and not st_state.get("manually_touched", False):
                        st_state["telp"] = saved.get("telp")
                    if saved.get("operator") and not st_state.get("manually_touched", False):
                        st_state["operator"] = saved.get("operator")
                    if saved.get("block_text"):
                        st_state["block"] = saved["block_text"]
                    st_state["db_updated_at"] = str(saved.get("updated_at") or "")
                    pulled += 1
                st.success(f"Update selesai. {pulled} pasien disinkron dari Supabase.")
            except Exception as e:
                st.error(f"Gagal update dari Supabase: {e}")

        if st.button("💾 Simpan (blok & rekap harian)", key="sb_save_all", use_container_width=True, type="primary"):
            try:
                # 1) simpan blok-blok reviewed (shared)
                payload = _compute_rows_to_save(rows, st.session_state.get("reviewer"))
                upsert_reviews(supabase, per_str_db, uploaded_name, payload)

                # 2) susun rekap harian dari blok yang ada di state saat ini
                _combined = []
                _konsul = 0
                for _rm, _st in st.session_state.per_patient.items():
                    if _st.get("block") and str(_st.get("visit","")).lower().startswith("kunjungan") and str(_st.get("gigi","")).strip() and (str(_st.get("telp","")).strip() or str(_st.get("operator","")).strip()):
                        _combined.append(_st["block"])
                        if re.search(r"(?i)\bkonsultasi\b|\bkonsul\b", _st["block"]):
                            _konsul += 1
                _total = len(_combined)
                _tind = max(_total - _konsul, 0)
                header_lines = [
                    "Review jumlah pasien Poli Bedah Mulut dan Maksilofasial RSGMP UNHAS, ",
                    f"{hari_str}, {per_str_show}",
                    "",
                    f"Jumlah pasien     : {_total} Pasien",
                    f"Tindakan              : {_tind} Pasien",
                    f"Konsultasi\t      : {_konsul} Pasien",
                    f"Terjaring GA\t      : xx Pasien",
                    f"Baksos                 : xx Pasien",
                    f"VIP                        : -",
                    "",
                    "-----------------------------------------------------",
                    "",
                    "POLI INTEGRASI",
                    "",
                ]
                _body = "\n\n".join(_combined) if _combined else ""
                _footer = [
                    "",
                    "------------------------------------------------------",
                    "",
                    f"{hari_str}, {per_str_show}",
                    "",
                    "Chief jaga poli :",
                    "drg. xx",
                ]
                _final_text = "\n".join(header_lines) + _body + ("\n" + "\n".join(_footer))
                upsert_summary(supabase, per_str_db, _final_text)

                st.success(f"Sukses simpan: {len(payload)} blok + rekap harian.")
            except Exception as e:
                st.error(f"Gagal simpan: {e}")

    st.success(f"Ditemukan {len(rows)} pasien — PERIODE: **{per_str_show}** — file: **{uploaded_name}**")
    st.info("✅ Setelah isi **Kunjungan/Gigi/Telp/Operator**, klik **Simpan (blok & rekap)** di sidebar. Untuk melihat perubahan dari rekan, klik **Update** di sidebar.")

    reviewer = st.text_input("Nama reviewer (opsional)") or ""
    st.session_state["reviewer"] = reviewer

    # ===== render blok per pasien
    st.markdown("---")
    st.markdown("### Blok per pasien (editable)")

    combined_blocks = []
    konsultasi_count = 0

    # urutkan by No.
    df = pd.DataFrame(rows).sort_values("No.")
    for _, r in df.iterrows():
        rm = str(r["No. RM"])
        # init state default (sekali)
        st.session_state.per_patient.setdefault(rm, {
            "visit": r["visit"],
            "gigi": r["gigi"],
            "telp": r["telp"],
            "operator": r["operator"],
            "block": None,          # text block terakhir
            "manually_edited": False,
            "name": r["Nama"],
            "dob": r["Tgl Lahir"],
            "dpjp_auto": r["DPJP (auto)"],
            "no": int(r["No."]),
        })
        state = st.session_state.per_patient[rm]
        patient_key = f"{rm}_{state['no']}"
        state.setdefault("last_sig", None)
        state.setdefault("manually_touched", False)

        # Prefill / overwrite dari Supabase jika ada pembaruan, kecuali user sudah mengedit manual
        saved = review_map.get(rm)
        state.setdefault("db_updated_at", None)
        if saved:
            db_ts = str(saved.get("updated_at") or "")
            # Perbarui state bila:
            #  - belum pernah punya timestamp DB, atau
            #  - timestamp DB berubah dari terakhir kita tahu,
            # dan user belum mengubah textarea di sesi ini.
            if (state["db_updated_at"] != db_ts) and (not state.get("manually_touched", False)):
                # update form fields dari DB bila tersedia
                if saved.get("visit"):
                    state["visit"] = normalize_visit(saved.get("visit"))
                if saved.get("gigi"):
                    state["gigi"] = saved.get("gigi")
                if saved.get("telp"):
                    state["telp"] = saved.get("telp")
                if saved.get("operator"):
                    state["operator"] = saved.get("operator")
                # update block text jika DB punya
                if saved.get("block_text"):
                    state["block"] = saved["block_text"]
                state["db_updated_at"] = db_ts

        # input mini
        v1, v2, v3, v4 = st.columns(4)
        with v1:
            state["visit"] = normalize_visit(st.text_input("Kunjungan", value=state["visit"], key=f"visit_{patient_key}"))
        with v2:
            state["gigi"] = st.text_input("Gigi", value=state["gigi"], key=f"gigi_{patient_key}")
        with v3:
            state["telp"] = st.text_input("Telp", value=state["telp"], key=f"telp_{patient_key}")
        with v4:
            state["operator"] = st.text_input("Operator", value=state["operator"], key=f"opr_{patient_key}")

        # Recompute reviewed status AFTER inputs, then open wrapper and render header
        auto_ok = (
            str(state["visit"]).lower().startswith("kunjungan")
            and str(state["gigi"]).strip() != ""
            and (str(state["telp"]).strip() != "" or str(state["operator"]).strip() != "")
        )
        wrap_style = "background-color:#e8f5e9;border:1px solid #2e7d32;border-radius:10px;padding:16px" if auto_ok else "background-color:#ffffff;border:1px solid #ddd;border-radius:10px;padding:16px"
        st.markdown(f'<div style="{wrap_style}">', unsafe_allow_html=True)
        st.markdown(f"**RM {fmt_rm(rm)} — {r['Nama']}**")
        st.caption(f"Tgl lahir: {r['Tgl Lahir']} | DPJP (auto): {r['DPJP (auto)']}")

        # jika belum lengkap: tutup wrapper & lanjut pasien berikutnya (tidak render textarea)
        if not auto_ok:
            st.markdown("</div>", unsafe_allow_html=True)
            st.markdown("")  # spacer
            continue

        # build default block berdasarkan input TERKINI
        rdict = {
            "Nama": state["name"],
            "Tgl Lahir": state["dob"],
            "No. RM": rm,
            "DPJP (auto)": state["dpjp_auto"],
            "visit": state["visit"],
            "gigi": state["gigi"],
            "telp": state["telp"],
            "operator": state["operator"],
        }
        default_block, tind_list, konsul_flag = build_block_with_meta(
            state["no"], rdict, state["visit"], per_date
        )

        # signature input saat ini
        current_sig = (
            normalize_visit(state["visit"]),
            str(state["gigi"]).strip(),
            str(state["telp"]).strip(),
            str(state["operator"]).strip(),
        )

        # set awal atau auto-overwrite bila input berubah dan user belum edit textarea
        if state["block"] is None:
            state["block"] = default_block
        elif (state["last_sig"] != current_sig) and (not state["manually_touched"]):
            state["block"] = default_block

        # render textarea dan deteksi apakah user benar2 mengedit manual
        old_text = state["block"]
        edited_text = st.text_area(
            "Blok preview (boleh revisi manual)",
            value=old_text,
            height=220,
            key=f"block_{patient_key}"
        )
        st.markdown("</div>", unsafe_allow_html=True)

        state["manually_touched"] = (edited_text != old_text)
        state["block"] = edited_text
        state["last_sig"] = current_sig

        # akumulasi gabungan & hitung konsultasi
        combined_blocks.append(state["block"])
        if konsul_flag or re.search(r"(?i)\bkonsultasi\b|\bkonsul\b", state["block"]):
            konsultasi_count += 1

        st.markdown("")  # spacer

    # ===== gabungan + rekap (render sekali di paling bawah) =====
    total_reviewed = len(combined_blocks)
    tindakan_count = max(total_reviewed - konsultasi_count, 0)

    st.markdown("---")
    st.markdown("### Rekap & Gabungan (format beku)")

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
    body_text = "\n\n".join(combined_blocks) if combined_blocks else ""
    footer_lines = [
        "",
        "------------------------------------------------------",
        "",
        f"{hari_str}, {per_str_show}",
        "",
        "Chief jaga poli :",
        "drg. xx",
    ]
    final_text = "\n".join(header_lines) + body_text + ("\n" + "\n".join(footer_lines))

    # Prefill rekap dari Supabase (kalau ada)
    saved_summary = load_summary(supabase, per_str_db)
    if saved_summary:
        final_text = saved_summary

    st.text_area("Teks gabungan", final_text, height=420)


    colD1, colD2 = st.columns(2)
    with colD1:
        st.download_button(
            "⬇️ Download TXT",
            data=final_text.encode("utf-8"),
            file_name="laporan_pasien.txt",
            mime="text/plain",
            use_container_width=True
        )
    with colD2:
        # DOCX monospace agar spasi tetap
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

elif uploaded_bytes is None:
    with st.expander("📅 Lihat data tersimpan berdasarkan tanggal (tanpa upload)", expanded=True):
        pick_date = st.date_input("Pilih tanggal PERIODE", value=date.today())
        if st.button("🔄 Update dari Supabase", use_container_width=True):
            # trigger a no-op rerun; the user will click 'Muat data tersimpan' di bawah untuk menampilkan
            st.experimental_rerun()
        if st.button("Muat data tersimpan", use_container_width=True):
            per_date = pick_date
            hari_str = HARI_ID[per_date.weekday()]
            per_str_show = per_date.strftime("%d/%m/%Y")
            per_str_db = per_date.strftime("%Y-%m-%d")
            supabase = get_supabase()
            review_map = load_review_map(supabase, per_str_db)
            blocks = [r.get("block_text","") for r in review_map.values() if (r.get("block_text") or "").strip()]
            if not blocks:
                st.warning("Belum ada blok yang tersimpan untuk tanggal ini.")
            else:
                st.success(f"Ditemukan {len(blocks)} blok tersimpan untuk {per_str_show}.")
                body_text = "\n\n".join(blocks)
                header_lines = [
                    "Review jumlah pasien Poli Bedah Mulut dan Maksilofasial RSGMP UNHAS, ",
                    f"{hari_str}, {per_str_show}",
                    "",
                    f"Jumlah pasien     : {len(blocks)} Pasien",
                    "Tindakan              : -",
                    "Konsultasi\t      : -",
                    "Terjaring GA\t      : xx Pasien",
                    "Baksos                 : xx Pasien",
                    "VIP                        : -",
                    "",
                    "-----------------------------------------------------",
                    "",
                    "POLI INTEGRASI",
                    "",
                ]
                footer_lines = [
                    "",
                    "------------------------------------------------------",
                    "",
                    f"{hari_str}, {per_str_show}",
                    "",
                    "Chief jaga poli :",
                    "drg. xx",
                ]
                final_text = "\n".join(header_lines) + body_text + ("\n" + "\n".join(footer_lines))
                # Prefill rekap harian kalau ada
                saved_summary = load_summary(supabase, per_str_db)
                if saved_summary:
                    final_text = saved_summary
                st.text_area("Teks gabungan (tersimpan)", final_text, height=420)
