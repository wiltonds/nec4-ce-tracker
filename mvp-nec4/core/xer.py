"""Minimal Primavera P6 .xer reader -> pandas DataFrames."""

import pandas as pd


def read_xer(path_or_bytes) -> dict[str, pd.DataFrame]:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        text = path_or_bytes.decode("cp1252", errors="ignore")
    else:
        with open(path_or_bytes, "rb") as f:
            text = f.read().decode("cp1252", errors="ignore")
    tables, name, cols, rows = {}, None, [], []
    for line in text.splitlines():
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            if name:
                tables[name] = pd.DataFrame(rows, columns=cols)
            name, cols, rows = parts[1].strip(), [], []
        elif tag == "%F":
            cols = parts[1:]
        elif tag == "%R":
            r = parts[1:]
            rows.append((r + [""] * len(cols))[: len(cols)])
    if name:
        tables[name] = pd.DataFrame(rows, columns=cols)
    return tables


DATE_COLS = ["target_start_date", "target_end_date", "act_start_date", "act_end_date",
             "early_start_date", "early_end_date", "restart_date", "reend_date"]


def activities(tables: dict) -> pd.DataFrame:
    """Activities with WBS path, planned (target), actual and forecast dates."""
    t = tables["TASK"].copy()
    for c in DATE_COLS:
        if c in t:
            t[c] = pd.to_datetime(t[c], errors="coerce")
    wbs = tables.get("PROJWBS")
    if wbs is not None:
        names = dict(zip(wbs["wbs_id"], wbs["wbs_name"]))
        t["wbs"] = t["wbs_id"].map(names)
    t["total_float_days"] = pd.to_numeric(t.get("total_float_hr_cnt"), errors="coerce") / 8
    t["forecast_start"] = t["act_start_date"].fillna(t.get("restart_date")).fillna(t.get("early_start_date"))
    t["forecast_finish"] = t["act_end_date"].fillna(t.get("reend_date")).fillna(t.get("early_end_date"))
    t["slip_days"] = (t["forecast_finish"] - t["target_end_date"]).dt.days
    return t
