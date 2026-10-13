"""Build the MVP data files from the project's raw exports.

Usage:
    python scripts/build_data.py --xer path/to/programme.xer --ewr path/to/EW_Register.xlsx

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
    out = df[df.client_or_others & ~df.ce_referenced][["ew_id", "topic", "title", "notified", "status"]].copy()
    out["title"] = out["title"].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
    out.to_csv(DATA / "ew_scan.csv", index=False)
    print(f"ew_scan.csv: {len(out)} of {len(df)} EWs involve Client/Others with no CE reference in the register")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xer")
    ap.add_argument("--ewr")
    args = ap.parse_args()
    if args.xer:
        build_programme(args.xer)
    if args.ewr:
        build_ew_scan(args.ewr)
