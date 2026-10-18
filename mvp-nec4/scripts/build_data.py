"""Build the MVP data files from the project's raw exports.

Usage:
    python scripts/build_data.py --xer path/to/programme.xer --ewr path/to/EW_Register.xlsx --names private_names.txt

Outputs (data/):
    programme.csv           activities linked to tracked events (from P6)
    programme_findings.csv  logic checks on the programme (e.g. consent not linked to works)
    ew_scan.csv             early warnings on Client/Others that show no CE reference

Raw files stay outside the repo: they are confidential (NEC4 cl. 29) and contain personal data.
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.tracker import (DEFAULT_PACKAGES, classify_package, fmt_location,  # noqa: E402
                          parse_chainage, parse_micro, scrub, set_private_names)
from core.xer import activities, read_xer  # noqa: E402

DATA = ROOT / "data"

# Activities to track per event (P6 task codes)
EVENT_ACTIVITIES = {
    "CE-01": ["DOC-4350", "DOC-4400", "TWD-8200", "TWD-8350", "CLV-5450", "CLV-5500",
              "CLV-5550", "CLV-5600", "TMP-4000"],
}
STATUS = {"TK_NotStart": "Not started", "TK_Active": "In progress", "TK_Complete": "Complete"}


def build_programme(xer_path: str):
    t = read_xer(xer_path)
    a = activities(t)
    pred = t["TASKPRED"]
    code = dict(zip(a.task_id, a.task_code))
    by = a.set_index("task_code")
    data_date = pd.to_datetime(t["PROJECT"]["last_recalc_date"].iloc[0])

    rows, findings = [], []
    for eid, codes in EVENT_ACTIVITIES.items():
        for c in codes:
            if c not in by.index:
                continue
            r = by.loc[c]
            preds = [code.get(p) for p in pred[pred.task_id == r.task_id].pred_task_id if code.get(p)]
            rows.append({
                "activity_id": c, "activity": r.task_name.strip(), "owner": r.get("wbs", ""),
                "planned_start": r.target_start_date.date(), "planned_finish": r.target_end_date.date(),
                "status": STATUS.get(r.status_code, r.status_code),
                "total_float_days": r.total_float_days,
                "depends_on": ";".join(p for p in preds if p in codes),
                "blocked_by_event": eid,
            })

        # Logic check 1: consent activities that do not drive the works they constrain
        works = [c for c in codes if c.startswith(("CLV", "TWD"))]
        for consent in [c for c in codes if c.startswith("DOC") and c in by.index]:
            r = by.loc[consent]
            succ = {code.get(s) for s in pred[pred.pred_task_id == r.task_id].task_id}
            if "not needed" in r.task_name.lower() or str(r.get("wbs", "")).startswith("Inactive"):
                findings.append({"event_id": eid, "activity_id": consent, "severity": "high",
                                 "finding": f"'{r.task_name.strip()}' was removed from the programme as 'not needed'."})
            elif not succ.intersection(works):
                findings.append({"event_id": eid, "activity_id": consent, "severity": "high",
                                 "finding": f"'{r.task_name.strip()}' is not a predecessor of the CH 1600 sheet piling "
                                            "or its temporary works design, so the programme does not show the consent "
                                            "as a constraint on those works (relevant to delay assessment, cl. 63.5)."})
        # Logic check 2: low float on constrained works
        for c in works:
            if c in by.index and pd.notna(by.loc[c].total_float_days) and by.loc[c].total_float_days <= 10:
                findings.append({"event_id": eid, "activity_id": c, "severity": "medium",
                                 "finding": f"'{by.loc[c].task_name.strip()}' has only {by.loc[c].total_float_days:.0f} days "
                                            "of total float: any delay quickly reaches the critical path."})
    findings.append({"event_id": "ALL", "activity_id": "-", "severity": "info",
                     "finding": f"Programme data date: {data_date:%d %b %Y}. Status and float are as at that date."})
    pd.DataFrame(rows).to_csv(DATA / "programme.csv", index=False)
    pd.DataFrame(findings).to_csv(DATA / "programme_findings.csv", index=False)
    print(f"programme.csv: {len(rows)} activities; findings: {len(findings)}")


def build_activities(xer_path: str):
    """Full activity list + relationships (for suggestions and forward-pass impact) + CE record."""
    t = read_xer(xer_path)
    a = activities(t)
    a["status"] = a["status_code"].map(STATUS).fillna(a["status_code"])
    a["is_milestone"] = a["task_type"].isin(["TT_Mile", "TT_FinMile"])
    out = pd.DataFrame({
        "task_code": a.task_code, "task_name": a.task_name.str.strip().map(scrub), "wbs": a.wbs,
        "start": a.forecast_start.dt.date, "finish": a.forecast_finish.dt.date,
        "planned_start": a.target_start_date.dt.date, "planned_finish": a.target_end_date.dt.date,
        "total_float_days": a.total_float_days.round(1), "status": a.status, "is_milestone": a.is_milestone,
        "service_package": a.get("service_package", ""),
    })
    out.to_csv(DATA / "activities.csv", index=False)
    code = dict(zip(a.task_id, a.task_code))
    r = t["TASKPRED"]
    rel = pd.DataFrame({
        "pred": r.pred_task_id.map(code), "succ": r.task_id.map(code),
        "type": r.pred_type.str.replace("PR_", "", regex=False),
        "lag_days": (pd.to_numeric(r.lag_hr_cnt, errors="coerce").fillna(0) / 8 * 7 / 5).round(),
    }).dropna(subset=["pred", "succ"])
    rel.to_csv(DATA / "relationships.csv", index=False)
    ce = a[a.wbs.astype(str).str.startswith("Compensation Events")]
    ce_out = pd.DataFrame({"ce_ref": ce.task_name.str.extract(r"(CE-\d+)")[0],
                           "title": ce.task_name.str.replace(r"^CE-\d+\s*", "", regex=True).map(scrub),
                           "date": ce.target_start_date.dt.date})
    ce_out.to_csv(DATA / "ce_record.csv", index=False)
    print(f"activities.csv: {len(out)}; relationships.csv: {len(rel)}; ce_record.csv: {len(ce_out)}")


def _tok(s):
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2}


def _match_ce(title, ce):
    tt = _tok(title)
    best, bs = "", 0.0
    for c in ce.itertuples():
        ct = _tok(c.title)
        if not tt or not ct:
            continue
        j = len(tt & ct) / len(tt | ct)
        if j > bs:
            best, bs = c.ce_ref, j
    return best if bs >= 0.5 else ""


def _packages():
    f = DATA / "packages.csv"
    if not f.exists() or "LEDP" not in f.read_text():
        pd.DataFrame(DEFAULT_PACKAGES).to_csv(f, index=False)
    return pd.read_csv(f)


NOTE_SPLIT = re.compile(r"(?m)^\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\s*:?\s*")


def build_tracker(ewr_path: str):
    """Tracker = one row per dated fact. Sources: EW register (EW + dated meeting notes) and CEMAR docs."""
    pk = _packages()
    df = pd.read_excel(ewr_path, sheet_name="EW Register", header=2).iloc[:, :8]
    df.columns = ["ew_id", "topic", "title", "description", "notified", "status", "notes", "actions"]
    df = df[df.ew_id.astype(str).str.startswith("EW-")].copy()
    df["ew_id"] = df.ew_id.str.replace(" ", "")
    df["notified"] = pd.to_datetime(df["notified"], errors="coerce", dayfirst=True)
    # topic = root EW (merge "Further to EWxx")
    root = {}
    for r in df.itertuples():
        m = re.search(r"further to\s*ew[- ]?0*(\d+)", str(r.description), re.I)
        root[r.ew_id] = f"EW-{m.group(1)}" if m else r.ew_id
    for k in list(root):
        while root[k] != k and root.get(root[k], root[k]) != root[k]:
            root[k] = root[root[k]]
    rows = []
    for r in df.itertuples():
        text = f"{r.title} {r.description}"
        a, b = parse_chainage(text)
        pkg = classify_package(text, pk)
        topic = root[r.ew_id]
        base = {"topic_id": "TOP-" + topic, "package_id": pkg, "location": fmt_location(a, b),
                "ch_from": a, "ch_to": b, "source": "EW register", **parse_micro(text)}
        desc = scrub(re.sub(r"\s+", " ", str(r.description)))[:400]
        rows.append({**base, "date": r.notified.date() if pd.notna(r.notified) else None, "ref": r.ew_id,
                     "summary": f"{scrub(str(r.title).strip())} - {desc}", "entry_type": "Early warning raised"})
        for blob in [r.notes, r.actions]:
            parts = NOTE_SPLIT.split(str(blob)) if pd.notna(blob) else []
            for i in range(1, len(parts) - 1, 2):
                d = pd.to_datetime(parts[i].replace("/", "."), format="%d.%m.%y", errors="coerce")
                if pd.isna(d):
                    d = pd.to_datetime(parts[i], dayfirst=True, errors="coerce")
                txt = scrub(re.sub(r"\s+", " ", parts[i + 1]).strip())[:400]
                if pd.notna(d) and txt and txt.lower() != "nan":
                    rows.append({**base, "date": d.date(), "ref": f"{r.ew_id} (EW meeting)",
                                 "summary": txt, "entry_type": "Action" if blob is r.actions else "Meeting note"})
    tr = pd.DataFrame(rows)
    titles = df.set_index("ew_id")["title"].astype(str).str.strip().map(scrub).to_dict()
    tr["topic_title"] = tr["topic_id"].str.replace("TOP-", "", regex=False).map(titles)
    # Ledpettie: merge EW-95/EW-146 thread with the CEMAR correspondence
    led = tr["topic_id"].isin(["TOP-EW-95", "TOP-EW-146"])
    tr.loc[led, ["topic_id", "topic_title", "package_id"]] = ["TOP-LEDPETTIE", "Ledpettie Bridge (LB 5574) condition", "LEDP"]
    docs = pd.read_csv(DATA / "documents.csv", parse_dates=["doc_date"])
    docs = docs[docs.event_id == "CE-01"]
    docs = docs[~docs.ref.str.startswith("EW-")]  # EW rows already come from the register
    extra = pd.DataFrame({
        "topic_id": "TOP-LEDPETTIE", "topic_title": "Ledpettie Bridge (LB 5574) condition", "package_id": "LEDP",
        "location": "CH 1600", "ch_from": 1600, "ch_to": 1600, "source": "CEMAR", "date": docs.doc_date.dt.date,
        "ref": docs.ref, "summary": docs.summary, "entry_type": docs.doc_type})
    tr = pd.concat([tr, extra], ignore_index=True)
    tr["work_package"] = ""
    tr["impacted_activities"] = ""
    tr.loc[tr.topic_id == "TOP-LEDPETTIE", ["work_package", "impacted_activities"]] = [
        "Culvert 23/A9 - SB CH 1600 (sheet piling)", "TWD-8350;CLV-5450;CLV-5500"]
    tr = tr.sort_values(["topic_id", "date"]).reset_index(drop=True)
    tr.insert(0, "entry_id", [f"T-{i + 1:04d}" for i in range(len(tr))])
    tr.to_csv(DATA / "tracker.csv", index=False)
    print(f"tracker.csv: {len(tr)} entries, {tr.topic_id.nunique()} topics; by package:",
          tr.drop_duplicates("topic_id").package_id.value_counts().to_dict())


def build_ew_scan(ewr_path: str):
    df = pd.read_excel(ewr_path, sheet_name="EW Register", header=2)
    df = df.iloc[:, :8]
    df.columns = ["ew_id", "topic", "title", "description", "notified", "status", "notes", "actions"]
    df = df[df.ew_id.astype(str).str.startswith("EW-")].copy()
    df["notified"] = pd.to_datetime(df["notified"], errors="coerce", dayfirst=True)
    text = df["title"].astype(str) + " " + df["description"].astype(str)
    others = r"openreach|\bbt\b|btor|fibre|ssen|scottish water|utilit|client|transport scotland|\bpm\b|scope|consent|council|pkc|network rail"
    df["client_or_others"] = (df["topic"] == "Utilities") | text.str.contains(others, case=False, regex=True)
    df["ce_referenced"] = (df["notes"].astype(str) + df["actions"].astype(str)).str.contains(
        r"\bCE[- ]?\d+|compensation event", case=False, regex=True)
    ce_file = DATA / "ce_record.csv"
    ce = pd.read_csv(ce_file) if ce_file.exists() else pd.DataFrame(columns=["ce_ref", "title"])
    df["matched_ce"] = df["title"].map(lambda t: _match_ce(t, ce))
    out = df[df.client_or_others & ~df.ce_referenced][["ew_id", "topic", "title", "notified", "status", "matched_ce"]].copy()
    out["title"] = out["title"].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
    out["title"] = out["title"].map(scrub)
    out.to_csv(DATA / "ew_scan.csv", index=False)
    print(f"ew_scan.csv: {len(out)} of {len(df)} EWs on Client/Others; {int((out.matched_ce != '').sum())} matched to a CE title")


# ---------------------------------------------------------------- PMI register (CEMAR PDFs)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_IN_TEXT = re.compile(rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?(?:{MONTHS}|(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\s+20\d\d\b"
                          r"|(?<![\d.])(?:0[1-9]|[12]\d|3[01])[./](?:0[1-9]|1[0-2])[./](?:20)?2\d(?![\d.])"
                          r"|\b[A-Z][a-z]{2}, \d{2}-[A-Z][a-z]{2}-20\d\d\b")
FOOTER = re.compile(r"^\s*[A-Z][a-z]{2}, \d{2}-[A-Z][a-z]{2}-\d{4} \d{2}:\d{2}\s+\d+ of \d+\s*$", re.M)


def _refs(prefix, text, exclude=""):
    out = sorted({f"{prefix}-{int(n)}" for n in re.findall(rf"\b{prefix}[- ]?0*(\d+)\b", text)} - {exclude},
                 key=lambda s: int(s.split("-")[1]))
    return ";".join(out)


def _pmi_kind(title, body):
    t = title.lower()
    if "revised quotation" in t:
        return "Revised quotation request (cl. 62.3)"
    if "submit quotation" in t:
        return "Quotation request (cl. 61.2)"
    if "proposed instruction" in t:
        return "Proposed instruction (cl. 65.1)"
    if "change to scope" in t or "scope is changed" in body.lower() or re.search(r"clause 14\.3", body):
        return "Change to Scope (cl. 14.3)"
    return "Other instruction"


def parse_pmi(text: str, pk: pd.DataFrame) -> dict:
    lines = text.splitlines()
    n = re.search(r"PMI-(\d+)", text)
    date = re.search(r"Date\s+([A-Z][a-z]{2}, \d{2}-[A-Z][a-z]{2}-\d{4})", text)
    title, start = "", 0
    for i, ln in enumerate(lines):
        if re.search(r"Project Manager.s Instruction PMI-\d+\s*$", ln):
            title, start = lines[i + 1].strip(), i + 2
            break
        m = re.match(r"\s*PMI-\d+:\s*(.+)", ln)
        if m:  # proposed instruction layout: "PMI-76: Instruction to submit ... PI-1" + "PI-1: <title>"
            nxt = lines[i + 1].strip()
            title, start = f"{m.group(1).strip()} - {nxt.split(':', 1)[-1].strip()}", i + 2
            break
    body = "\n".join(lines[start:])
    body = body.split("Yours faithfully")[0]
    body = FOOTER.sub("", body)
    flat = re.sub(r"\s+", " ", body).strip()
    full = f"{title} {flat}"
    pkg = classify_package(title, pk)
    if pkg == "OTHER":
        pkg = classify_package(full, pk)
    a, b = parse_chainage(full)
    micro = parse_micro(full)
    kind = _pmi_kind(title, flat)
    clause = re.search(r"[Cc]l(?:ause|\.)\s*(\d+(?:\.\d+)*)", full)
    ce_title = re.search(r"entitled:\s*(.+?)\.\s*I confirm", flat)
    due = re.search(r"due by:?\s*([A-Z][a-z]{2}, \d{2}-[A-Z][a-z]{2}-\d{4})", flat)
    dates = DATE_IN_TEXT.findall(flat)
    date_change = kind.startswith(("Change to Scope", "Other")) and bool(dates) and bool(
        re.search(r"programmed|completion of|access|period|from .* to |text to be added|revised date|no longer", flat, re.I))
    no_effect = bool(re.search(r"no effect (?:up)?on Defined Cost or Completion", flat, re.I))
    return {
        "pmi": f"PMI-{n.group(1)}" if n else "", "date": pd.to_datetime(date.group(1), format="%a, %d-%b-%Y").date() if date else None,
        "title": scrub(title), "kind": kind, "clause": clause.group(1) if clause else "",
        "package_id": pkg, "location": fmt_location(a, b), "ch_from": a, "ch_to": b, **micro,
        "ce_refs": _refs("CE", full), "ew_refs": _refs("EW", full), "gc_refs": _refs("GC", full),
        "pmi_refs": _refs("PMI", full, exclude=f"PMI-{n.group(1)}" if n else ""), "pi_refs": _refs("PI", full),
        "ce_title": scrub(ce_title.group(1)) if ce_title else "",
        "quotation_due": pd.to_datetime(due.group(1), format="%a, %d-%b-%Y").date() if due else None,
        "date_change": date_change, "dates_mentioned": "; ".join(dict.fromkeys(dates))[:200],
        "no_effect_on_cost_or_completion": no_effect, "summary": scrub(flat)[:900],
    }


def build_pmis(pmi_dir: str):
    """PMI register from the CEMAR PMI PDFs (needs `pdftotext`, from poppler-utils)."""
    import subprocess
    pk = _packages()
    rows = []
    for f in sorted(Path(pmi_dir).glob("PMI-*.pdf"), key=lambda p: int(re.findall(r"\d+", p.name)[0])):
        txt = subprocess.run(["pdftotext", "-layout", str(f), "-"], capture_output=True, text=True).stdout
        rows.append(parse_pmi(txt, pk))
    pm = pd.DataFrame(rows)
    pm.to_csv(DATA / "pmi_register.csv", index=False)
    # CE register as far as the PMIs reveal it (quotation instructions carry the CE title)
    ce = []
    for r in pm.itertuples():
        for c in filter(None, r.ce_refs.split(";")):
            ce.append({"ce": c, "title": r.ce_title if r.kind.startswith(("Quotation", "Revised")) and r.ce_title else "",
                       "pmi": r.pmi, "date": r.date, "package_id": r.package_id,
                       "quotation_due": r.quotation_due})
    ce = pd.DataFrame(ce)
    if not ce.empty:
        ce["quotation_due"] = pd.to_datetime(ce["quotation_due"], errors="coerce")
        ce["date"] = pd.to_datetime(ce["date"], errors="coerce")
        ce = (ce.sort_values("date").groupby("ce", as_index=False)
              .agg(title=("title", lambda s: next((x for x in s if x), "")), first_seen=("date", "min"),
                   pmis=("pmi", lambda s: ";".join(dict.fromkeys(s))), package_id=("package_id", "first"),
                   last_quotation_due=("quotation_due", "max")))
        ce["n"] = ce.ce.str.extract(r"(\d+)").astype(int)
        ce = ce.sort_values("n").drop(columns="n")
    ce.to_csv(DATA / "cemar_ce.csv", index=False)
    print(f"pmi_register.csv: {len(pm)} PMIs ({pm.kind.value_counts().to_dict()}); "
          f"{int(pm.date_change.sum())} with date changes; cemar_ce.csv: {len(ce)} CEs")


# ---------------------------------------------------------------- full EW register + EW -> PMI -> CE chain
def _ew_frame(ewr_path):
    df = pd.read_excel(ewr_path, sheet_name="EW Register", header=2).iloc[:, :8]
    df.columns = ["ew_id", "topic", "title", "description", "notified", "status", "notes", "actions"]
    df = df[df.ew_id.astype(str).str.startswith("EW-")].copy()
    df["ew_id"] = df.ew_id.str.replace(" ", "")
    df["notified"] = pd.to_datetime(df["notified"], errors="coerce", dayfirst=True)
    for c in ["title", "description", "notes", "actions"]:
        df[c] = df[c].fillna("").astype(str)
    return df


def build_ew_register(ewr_path: str):
    """Every EW (open and closed) with 'absorbed by' for closed ones."""
    pk = _packages()
    df = _ew_frame(ewr_path)
    ids = set(df.ew_id)
    further = {}
    for r in df.itertuples():
        m = re.search(r"further to\s*ew[- ]?0*(\d+)", r.description, re.I)
        if m:
            further.setdefault(f"EW-{m.group(1)}", r.ew_id)
    closes = {}  # "agreed to close EW-111" written in another EW's notes
    for r in df.itertuples():
        for m in re.finditer(r"close\s+(?:the\s+)?EW[- ]?0*(\d+)", r.notes + " " + r.actions, re.I):
            tgt = f"EW-{m.group(1)}"
            if tgt != r.ew_id:
                closes[tgt] = r.ew_id
    rows = []
    for r in df.itertuples():
        text = f"{r.title} {r.description}"
        a, b = parse_chainage(text)
        notes = re.sub(r"\s+", " ", f"{r.notes} {r.actions}").strip()
        closed = bool(re.search(r"clos", str(r.status), re.I))
        absorbed = ""
        if closed:
            m = re.search(r"(?:(?:discussed|discussions?)\s+(?:under|on|in)|see|covered\s+(?:by|under)|captured\s+(?:in|under)|"
                          r"transferred\s+to|superseded\s+by)\s+EW[- ]?0*(\d+)", notes, re.I)
            if r.ew_id in closes:
                absorbed = closes[r.ew_id]
            elif m and f"EW-{m.group(1)}" != r.ew_id and f"EW-{m.group(1)}" in ids:
                absorbed = f"EW-{m.group(1)}"
            elif r.ew_id in further:
                absorbed = further[r.ew_id]
            ce = _refs("CE", notes)
            pmi = _refs("PMI", notes)
            absorbed = ";".join(x for x in [absorbed, pmi, ce] if x)
        last = NOTE_SPLIT.split(r.notes)
        last_note = scrub(re.sub(r"\s+", " ", last[2] if len(last) > 2 else r.notes)).strip()[:300]
        reason = ""
        if closed:
            low = notes.lower()
            reason = ("Absorbed by " + absorbed) if absorbed else (
                "Event passed / no further impact" if re.search(r"no further|event has passed|passed|no longer|resolved", low)
                else "Closed (reason not recorded)")
        rows.append({"ew_id": r.ew_id, "title": scrub(r.title.strip()), "register_topic": r.topic,
                     "notified": r.notified.date() if pd.notna(r.notified) else None, "status": r.status,
                     "package_id": classify_package(text, pk), "location": fmt_location(a, b), "ch_from": a, "ch_to": b,
                     **parse_micro(text), "further_to": next((k for k, v in further.items() if v == r.ew_id), ""),
                     "absorbed_by": absorbed, "close_reason": reason,
                     "ce_refs": _refs("CE", notes + " " + r.description), "pmi_refs": _refs("PMI", notes + " " + r.description),
                     "last_note": last_note})
    out = pd.DataFrame(rows)
    out.to_csv(DATA / "ew_register.csv", index=False)
    print(f"ew_register.csv: {len(out)} EWs; closed {int(out.close_reason.ne('').sum())}, "
          f"absorbed {int(out.absorbed_by.ne('').sum())}")


STOPW = set("the and for with from change scope works work delay delays ltd s100 s320 s350 s2805 contract drawings "
            "instruction submit quotation quotations revised reply potential impact early warning".split())


def _ttok(s):
    return {w for w in re.findall(r"[a-z0-9]+", str(s).lower()) if len(w) > 2 and w not in STOPW}


def _sim(a, b):
    a, b = _ttok(a), _ttok(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def build_chain():
    """EW -> PMI -> CE chain for every EW, with the coverage verdict used by the app."""
    ew = pd.read_csv(DATA / "ew_register.csv", keep_default_na=False)
    pm = pd.read_csv(DATA / "pmi_register.csv", keep_default_na=False)
    cc = pd.read_csv(DATA / "cemar_ce.csv", keep_default_na=False) if (DATA / "cemar_ce.csv").exists() else pd.DataFrame(columns=["ce", "title"])
    p6 = pd.read_csv(DATA / "ce_record.csv", keep_default_na=False)
    scan = pd.read_csv(DATA / "ew_scan.csv", keep_default_na=False).set_index("ew_id")["matched_ce"].to_dict()
    rows = []
    for e in ew.itertuples():
        pmis, how = {}, {}
        for p in pm.itertuples():
            explicit = e.ew_id in p.ew_refs.split(";") or p.pmi in e.pmi_refs.split(";")
            s = _sim(e.title, p.title)
            if explicit or s >= 0.34:
                pmis[p.pmi] = p
                how[p.pmi] = "explicit" if explicit else f"title match {s:.0%}"
        ces = {c: "EW notes" for c in filter(None, e.ce_refs.split(";"))}
        for p in pmis.values():
            for c in filter(None, p.ce_refs.split(";")):
                ces.setdefault(c, f"via {p.pmi}")
        for c in cc.itertuples():
            if c.title and _sim(e.title, c.title) >= 0.4:
                ces.setdefault(c.ce, "CEMAR CE title match")
        p6ce = scan.get(e.ew_id, "")
        if not p6ce:
            best = max(((x.ce_ref, _sim(e.title, x.title)) for x in p6.itertuples()), key=lambda t: t[1], default=("", 0))
            p6ce = best[0] if best[1] >= 0.5 else ""
        if p6ce:
            ces.setdefault(f"P6 {p6ce}", "P6 CE record")
        closed = e.close_reason != ""
        if ces:
            cov = "Covered by CE"
        elif pmis:
            cov = "PMI issued - check CE"
        elif closed and e.absorbed_by:
            cov = "Absorbed (" + e.absorbed_by + ")"
        elif closed:
            cov = "Closed - no CE"
        else:
            cov = "Gap - no PMI/CE (opportunity)"
        rows.append({"ew_id": e.ew_id, "title": e.title, "status": e.status, "notified": e.notified,
                     "package_id": e.package_id, "absorbed_by": e.absorbed_by,
                     "pmis": ";".join(pmis), "pmi_link": ";".join(f"{k}:{v}" for k, v in how.items()),
                     "ces": ";".join(ces), "ce_link": ";".join(f"{k}:{v}" for k, v in ces.items()), "coverage": cov})
    out = pd.DataFrame(rows)
    out.to_csv(DATA / "chain.csv", index=False)
    print("chain.csv:", out.coverage.str.split(" \\(").str[0].value_counts().to_dict())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xer")
    ap.add_argument("--ewr")
    ap.add_argument("--pmis", help="folder with the CEMAR PMI PDFs (PMI-<n>_Instruction.pdf)")
    ap.add_argument("--names",help="text file with one personal name per line to redact (keep outside the repo)")
    args = ap.parse_args()
    if args.names:
        set_private_names(Path(args.names).read_text(encoding="utf-8").splitlines())
    if args.xer:
        build_programme(args.xer)
        build_activities(args.xer)
    if args.ewr:
        build_ew_scan(args.ewr)
        build_tracker(args.ewr)
        build_ew_register(args.ewr)
    if args.pmis:
        build_pmis(args.pmis)
    if args.ewr or args.pmis:
        build_chain()
