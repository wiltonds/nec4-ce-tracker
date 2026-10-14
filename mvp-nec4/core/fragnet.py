"""Fragnet builder and approximate forward-pass impact on the programme.

Impact is computed in calendar days with a "shift" forward pass over the successors of the
impacted activities (FS/SS/FF/SF + lags from the P6 export). It ignores calendars and constraints,
so it is an indicative result: the official figure comes from running the fragnet in P6.
"""

import io
from collections import defaultdict, deque
from datetime import date, timedelta

import pandas as pd

DEFAULT_STEPS = {
    "STRUCT": [("PM instruction / change to the Scope (cl. 14.3)", 14, "Client"),
               ("Statutory consent application and determination", 56, "Others"),
               ("Temporary works redesign, check and PM acceptance", 42, "Contractor")],
    "UTIL": [("Others complete outstanding utility works", 28, "Others"),
             ("Confirmation and handover of work area to Contractor", 7, "Others")],
    "ENV": [("Licence / survey / mitigation completed", 28, "Others"),
            ("PM acceptance of revised plan", 14, "Client")],
    "LAND": [("Access / consent granted", 28, "Client")],
    "SCOPE": [("PM decision / instruction", 14, "Client"),
              ("Revised design and PM acceptance", 28, "Contractor")],
    "TM": [("Approval of traffic management / ITS arrangements", 21, "Client")],
    "EARTH": [("Investigation and revised design", 28, "Contractor"),
              ("PM acceptance", 14, "Client")],
    "OTHER": [("Resolution by Client / Others", 21, "Client")],
}
KEY_TYPES = {"Early warning raised", "PM Reply", "Correspondence", "Technical Report", "Missed reply",
             "Meeting Record", "PM Instruction", "CE Notification", "Meeting note"}


def short(text: str, n: int = 70) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def build_fragnet(entries: pd.DataFrame, steps: pd.DataFrame, impacted: list[str],
                  data_date: date, prefix: str = "FN") -> pd.DataFrame:
    """entries: selected facts (date, ref, summary); steps: forecast (name, duration_days, party)."""
    e = entries.sort_values("date").reset_index(drop=True)
    rows, n = [], 1000
    prev = None
    for i, r in e.iterrows():
        start = pd.to_datetime(r["date"]).date()
        finish = pd.to_datetime(e.loc[i + 1, "date"]).date() if i + 1 < len(e) else data_date
        finish = max(finish, start)
        code = f"{prefix}-{n}"
        rows.append({"code": code, "name": f"{r['ref']}: {short(r['summary'])}",
                     "type": "Milestone" if i == 0 else "Activity", "basis": "Actual (record)",
                     "party": r.get("party", ""), "start": start, "finish": start if i == 0 else finish,
                     "duration_days": 0 if i == 0 else (finish - start).days,
                     "predecessor": prev or "", "relationship": "FS" if prev else "", "source_ref": r["ref"]})
        prev, n = code, n + 10
        if i == 0 and len(e) == 1:
            pass
    cursor = max([data_date] + [r["finish"] for r in rows])
    for s in steps.itertuples():
        d = int(s.duration_days)
        code = f"{prefix}-{n}"
        rows.append({"code": code, "name": s.name, "type": "Activity", "basis": "Forecast",
                     "party": getattr(s, "party", ""), "start": cursor, "finish": cursor + timedelta(days=d),
                     "duration_days": d, "predecessor": prev or "", "relationship": "FS" if prev else "",
                     "source_ref": ""})
        prev, cursor, n = code, cursor + timedelta(days=d), n + 10
    end = f"{prefix}-{n}"
    rows.append({"code": end, "name": "Constraint removed - work can proceed", "type": "Milestone",
                 "basis": "Forecast", "party": "", "start": cursor, "finish": cursor, "duration_days": 0,
                 "predecessor": prev or "", "relationship": "FS" if prev else "", "source_ref": ""})
    for c in impacted:
        rows.append({"code": c, "name": "(tie-in to programme activity)", "type": "Tie-in", "basis": "Programme",
                     "party": "", "start": None, "finish": None, "duration_days": None,
                     "predecessor": end, "relationship": "FS", "source_ref": ""})
    return pd.DataFrame(rows)


def impact(acts: pd.DataFrame, rels: pd.DataFrame, impacted: list[str], release: date,
           max_nodes: int = 4000) -> pd.DataFrame:
    """Shift forward pass. Returns activities pushed later, with delay in calendar days."""
    a = acts.set_index("task_code")
    S = pd.to_datetime(a["start"]).dt.date.to_dict()
    F = pd.to_datetime(a["finish"]).dt.date.to_dict()
    done = (a["status"] == "Complete").to_dict()
    succ = defaultdict(list)
    preds = defaultdict(list)
    for r in rels.itertuples():
        succ[r.pred].append(r.succ)
        preds[r.succ].append((r.pred, r.type, int(r.lag_days)))
    # nodes reachable from impacted activities
    nodes, q = set(), deque([c for c in impacted if c in S])
    while q and len(nodes) < max_nodes:
        c = q.popleft()
        if c in nodes or done.get(c):
            continue
        nodes.add(c)
        q.extend(succ[c])
    indeg = {n: sum(1 for p, _, _ in preds[n] if p in nodes) for n in nodes}
    order, q = [], deque([n for n in nodes if indeg[n] == 0])
    while q:
        n = q.popleft()
        order.append(n)
        for s in succ[n]:
            if s in nodes:
                indeg[s] -= 1
                if indeg[s] == 0:
                    q.append(s)
    shift = {}
    for n in order:
        if S.get(n) is None or pd.isna(S.get(n)):
            continue
        best = 0
        if n in impacted:
            best = max(best, (release - S[n]).days + 1)
        for p, typ, lag in preds[n]:
            if p not in shift or shift[p] <= 0 or S.get(p) is None:
                continue
            if typ == "FS":
                slack = (S[n] - (F[p] + timedelta(days=lag))).days
            elif typ == "SS":
                slack = (S[n] - (S[p] + timedelta(days=lag))).days
            elif typ == "FF":
                slack = (F[n] - (F[p] + timedelta(days=lag))).days
            else:  # SF
                slack = (F[n] - (S[p] + timedelta(days=lag))).days
            best = max(best, shift[p] - max(slack, 0))
        shift[n] = max(best, 0)
    out = [(c, d) for c, d in shift.items() if d > 0]
    if not out:
        return pd.DataFrame(columns=["task_code", "task_name", "wbs", "finish", "new_finish", "delay_days",
                                     "total_float_days", "is_milestone"])
    res = a.loc[[c for c, _ in out], ["task_name", "wbs", "finish", "total_float_days", "is_milestone"]].copy()
    res["delay_days"] = [d for _, d in out]
    res["finish"] = pd.to_datetime(res["finish"]).dt.date
    res["new_finish"] = [f + timedelta(days=int(d)) for f, d in zip(res["finish"], res["delay_days"])]
    return res.reset_index().sort_values(["delay_days", "finish"], ascending=[False, True])


def to_p6_xlsx(fragnet: pd.DataFrame, wbs_hint: str = "") -> bytes:
    """Fragnet as Excel: a readable sheet plus TASK / TASKPRED sheets laid out for P6 spreadsheet import."""
    fn = fragnet[fragnet["type"] != "Tie-in"]
    task = pd.DataFrame({
        "task_code": fn["code"], "task_name": fn["name"].str.slice(0, 120),
        "status_code": fn["basis"].map({"Actual (record)": "Completed", "Forecast": "Not Started"}),
        "wbs_id": wbs_hint, "task_type": fn["type"].map({"Milestone": "Start Milestone", "Activity": "Task Dependent"}),
        "target_drtn_hr_cnt": fn["duration_days"].fillna(0).astype(int) * 8,
        "start_date": fn["start"], "end_date": fn["finish"],
    })
    pred = fragnet[fragnet["predecessor"] != ""]
    taskpred = pd.DataFrame({"task_code": pred["code"], "pred_task_code": pred["predecessor"],
                             "pred_type": "PR_" + pred["relationship"], "lag_hr_cnt": 0})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        fragnet.to_excel(xw, sheet_name="Fragnet", index=False)
        task.to_excel(xw, sheet_name="TASK", index=False)
        taskpred.to_excel(xw, sheet_name="TASKPRED", index=False)
        pd.DataFrame({"note": [
            "TASK and TASKPRED follow the column names of P6 spreadsheet import. Check them against your P6 "
            "import template before importing; durations are in hours (8 h/day).",
            "Actual (record) rows reflect documented dates; Forecast rows are planner assumptions to review."]}
        ).to_excel(xw, sheet_name="README", index=False)
    return buf.getvalue()
