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
                          parse_chainage, scrub, set_private_names)
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
    if not f.exists():
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
                "ch_from": a, "ch_to": b, "source": "EW register"}
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
    tr.loc[led, ["topic_id", "topic_title", "package_id"]] = ["TOP-LEDPETTIE", "Ledpettie Bridge (LB 5574) condition", "STRUCT"]
    docs = pd.read_csv(DATA / "documents.csv", parse_dates=["doc_date"])
    docs = docs[docs.event_id == "CE-01"]
    docs = docs[~docs.ref.str.startswith("EW-")]  # EW rows already come from the register
    extra = pd.DataFrame({
        "topic_id": "TOP-LEDPETTIE", "topic_title": "Ledpettie Bridge (LB 5574) condition", "package_id": "STRUCT",
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xer")
    ap.add_argument("--ewr")
    ap.add_argument("--names", help="text file with one personal name per line to redact (keep outside the repo)")
    args = ap.parse_args()
    if args.names:
        set_private_names(Path(args.names).read_text(encoding="utf-8").splitlines())
    if args.xer:
        build_programme(args.xer)
        build_activities(args.xer)
    if args.ewr:
        build_ew_scan(args.ewr)
        build_tracker(args.ewr)
