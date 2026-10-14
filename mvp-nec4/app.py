"""NEC4 Delay & Compensation Event Tracker - MVP (Streamlit)."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from core import extractor
from core.fragnet import DEFAULT_STEPS, KEY_TYPES, build_fragnet, impact, to_p6_xlsx
from core.narrative import build_narrative
from core.nec4_rules import RULES, ce_deadline, status_for
from core.notice import build_notice, to_docx
from core.tracker import ActivityIndex, classify_package, fmt_location, parse_chainage, scrub

load_dotenv()
DATA = Path(__file__).parent / "data"
st.set_page_config(page_title="NEC4 CE Tracker", page_icon="🚧", layout="wide")


def _secret(key: str):
    try:
        return st.secrets.get(key)
    except Exception:  # no secrets file when running locally
        return None


# Password gate: active only when APP_PASSWORD is set in Streamlit secrets
_pw = _secret("APP_PASSWORD")
if _pw and not st.session_state.get("auth_ok"):
    st.title("🚧 NEC4 CE Tracker")
    st.caption("Confidential pilot - authorised users only.")
    entered = st.text_input("Password", type="password")
    if entered:
        if entered == _pw:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()
if _secret("ANTHROPIC_API_KEY"):
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", _secret("ANTHROPIC_API_KEY"))


# ---------- data ----------
@st.cache_data
def load_csv(name, dates):
    return pd.read_csv(DATA / name, parse_dates=dates)


if "docs" not in st.session_state:
    st.session_state.docs = load_csv("documents.csv", ["doc_date"])
    st.session_state.programme = load_csv("programme.csv", ["planned_start", "planned_finish"])
    st.session_state.notified = set()

if "tracker" not in st.session_state:
    st.session_state.tracker = load_csv("tracker.csv", ["date"]).fillna({"work_package": "", "impacted_activities": "",
                                                                         "location": ""})
    st.session_state.packages = load_csv("packages.csv", None)


@st.cache_resource
def programme_data():
    acts = pd.read_csv(DATA / "activities.csv")
    rels = pd.read_csv(DATA / "relationships.csv")
    return acts, rels, ActivityIndex(acts)


docs: pd.DataFrame = st.session_state.docs
prog: pd.DataFrame = st.session_state.programme
tracker: pd.DataFrame = st.session_state.tracker
packages: pd.DataFrame = st.session_state.packages
PKG = dict(zip(packages.package_id, packages.package)) | {"OTHER": "Other"}
ACTS, RELS, AIDX = programme_data()
ACT_LABEL = {r.task_code: f"{r.task_code} · {r.task_name[:80]}" for r in ACTS.itertuples()}

# ---------- sidebar ----------
st.sidebar.title("🚧 NEC4 CE Tracker")
st.sidebar.caption("MVP - delay & compensation event control")
PAGES = ["Tracker", "Fragnet & narrative", "Ingest documents", "CE dashboard", "Timeline", "EW scan",
         "Draft notification", "NEC4 rules"]
if st.session_state.get("goto") in PAGES:
    st.session_state["nav"] = st.session_state.pop("goto")
page = st.sidebar.radio("Go to", PAGES, key="nav")
today = st.sidebar.date_input("Reference date", date.today(), help="Change to simulate another day.")
contract = st.sidebar.text_input("Contract", "A9 Dualling - Tay Crossing to Ballinluig (NEC4 ECC)")
ai_on = bool(extractor.os.getenv("ANTHROPIC_API_KEY"))
st.sidebar.markdown(f"**AI extraction:** {'🟢 on (' + extractor.MODEL + ')' if ai_on else '⚪ off - keyword rules'}")
st.sidebar.caption("Data: A9 project exports (P6 programme rev 017, CEMAR correspondence, EW register of 01 Oct 2026). "
                   "Pseudonymised: roles only, no personal names.")

EVENT_TITLES = {
    "CE-01": "Physical condition of Ledpettie Bridge (LB 5574) and Client Scope not fit for purpose, preventing "
             "statutory consent, temporary works acceptance and sheet piling at CH 1600",
    "CE-02": "Project Manager did not reply to GC-1385 within the period required (Clause 60.1(6))",
}


def event_table() -> pd.DataFrame:
    rows = []
    for eid, g in docs.groupby("event_id"):
        trig = g[g["awareness_trigger"].astype(str).str.lower() == "yes"].sort_values("doc_date")
        if trig.empty:
            continue
        first, last = trig.iloc[0], trig.iloc[-1]
        d_first, d_last = ce_deadline(first.doc_date.date()), ce_deadline(last.doc_date.date())
        notified = eid in st.session_state.notified
        rows.append({
            "event_id": eid,
            "documents": len(g),
            "clauses": "; ".join(sorted({c.strip() for s in g["clauses"].dropna() for c in str(s).split(";") if c.strip()})),
            "first_aware": first.doc_date.date(), "first_ref": first.ref,
            "deadline_strict": d_first, "status_strict": status_for(d_first, today, notified),
            "latest_new_fact": last.doc_date.date(), "latest_ref": last.ref,
            "deadline_latest": d_last, "status_latest": status_for(d_last, today, notified),
            "blocked_activities": int((prog["blocked_by_event"] == eid).sum()),
        })
    return pd.DataFrame(rows)


COLOR = {"red": "🔴", "orange": "🟠", "green": "🟢"}

# ---------- pages ----------
def topic_table(tr: pd.DataFrame) -> pd.DataFrame:
    g = tr.groupby("topic_id")
    out = pd.DataFrame({
        "topic": g["topic_title"].first(), "package": g["package_id"].first().map(PKG),
        "location": g["location"].agg(lambda s: next((x for x in s if isinstance(x, str) and x), "")),
        "first_fact": g["date"].min().dt.date, "last_fact": g["date"].max().dt.date, "facts": g.size(),
        "impacted_activities": g["impacted_activities"].agg(lambda s: next((x for x in s if isinstance(x, str) and x), "")),
    }).reset_index()
    out["days_since_last_fact"] = out["last_fact"].map(lambda d: (today - d).days if pd.notna(d) else None)
    return out.sort_values("last_fact", ascending=False)


if page == "Tracker":
    st.title("Delay tracker")
    st.caption("Every fact recorded, classified by package, work package and location, grouped into topics. "
               "Select a topic to see its history or turn it into a fragnet.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Facts recorded", len(tracker))
    c2.metric("Topics", tracker["topic_id"].nunique())
    recent = tracker[tracker["date"].dt.date >= today - timedelta(days=30)]
    c3.metric("Facts in last 30 days", len(recent))
    c4.metric("Topics linked to programme", int((topic_table(tracker)["impacted_activities"] != "").sum()))

    with st.expander("Filters", expanded=True):
        f1, f2, f3 = st.columns([2, 2, 3])
        pk_sel = f1.multiselect("Package", list(PKG), format_func=lambda k: PKG[k])
        txt = f2.text_input("Search text / reference")
        chs = tracker["ch_from"].dropna()
        ch_rng = f3.slider("Location (chainage, m)", 0, int(chs.max() if len(chs) else 10000) + 100,
                           (0, int(chs.max() if len(chs) else 10000) + 100), step=50)
        only_loc = f3.checkbox("Only facts with a location", value=False)
    v = tracker.copy()
    if pk_sel:
        v = v[v["package_id"].isin(pk_sel)]
    if txt:
        v = v[(v["summary"].str.contains(txt, case=False, na=False)) | (v["ref"].str.contains(txt, case=False, na=False))
              | (v["topic_title"].str.contains(txt, case=False, na=False))]
    if only_loc or ch_rng != (0, int(chs.max() if len(chs) else 10000) + 100):
        v = v[v["ch_from"].between(*ch_rng)]

    by_pkg = v.drop_duplicates("topic_id")["package_id"].map(PKG).value_counts().reset_index()
    by_pkg.columns = ["package", "topics"]
    fig = px.bar(by_pkg, x="topics", y="package", orientation="h", height=260)
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_title=None)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Topics")
    tt = topic_table(v)
    ev = st.dataframe(tt, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      key="topic_pick", height=320)
    if ev.selection.rows:
        tid = tt.iloc[ev.selection.rows[0]]["topic_id"]
        st.session_state["topic_sel"] = tid
        sub = tracker[tracker["topic_id"] == tid].sort_values("date")
        st.markdown(f"**{tid} · {sub['topic_title'].iloc[0]}** — {PKG.get(sub['package_id'].iloc[0])}")
        st.dataframe(sub[["date", "ref", "entry_type", "summary", "location", "source"]].assign(
            date=sub["date"].dt.date), hide_index=True, width="stretch")
        with st.form("edit_topic"):
            e1, e2 = st.columns(2)
            new_pkg = e1.selectbox("Package", list(PKG), index=list(PKG).index(sub["package_id"].iloc[0])
                                   if sub["package_id"].iloc[0] in PKG else 0, format_func=lambda k: PKG[k])
            wp = e2.text_input("Work package impacted", sub["work_package"].iloc[0])
            cur = [c for c in str(sub["impacted_activities"].iloc[0]).split(";") if c in ACT_LABEL]
            sug = AIDX.suggest(" ".join(sub["topic_title"].astype(str).tolist() + sub["summary"].astype(str).tolist()[:5]),
                               sub["ch_from"].dropna().iloc[0] if sub["ch_from"].notna().any() else None)
            opts = list(dict.fromkeys(cur + sug["task_code"].tolist() + list(ACT_LABEL)))
            imp = st.multiselect("Programme activities impacted (suggestions first)", opts, default=cur,
                                 format_func=lambda c: ACT_LABEL.get(c, c))
            b1, b2 = st.columns(2)
            save = b1.form_submit_button("Save classification")
            to_fn = b2.form_submit_button("Save and open in Fragnet ➜")
            if save or to_fn:
                m = st.session_state.tracker["topic_id"] == tid
                st.session_state.tracker.loc[m, "package_id"] = new_pkg
                st.session_state.tracker.loc[m, "work_package"] = wp
                st.session_state.tracker.loc[m, "impacted_activities"] = ";".join(imp)
                if to_fn:
                    st.session_state["goto"] = "Fragnet & narrative"
                st.rerun()
        with st.expander("Suggested programme activities for this topic"):
            st.dataframe(sug, hide_index=True, width="stretch")

    st.subheader("Add a fact manually")
    with st.form("add_fact", clear_on_submit=True):
        a1, a2, a3 = st.columns(3)
        fd = a1.date_input("Date", today)
        fr = a2.text_input("Reference (e.g. GC-1500, e-mail, site diary)")
        fs = a3.selectbox("Source", ["E-mail", "CEMAR", "Site report", "Meeting", "Other"])
        fsum = st.text_area("What happened (fact)")
        b1, b2, b3 = st.columns(3)
        topics = topic_table(tracker)
        tsel = b1.selectbox("Topic", ["NEW"] + topics["topic_id"].tolist(),
                            format_func=lambda t: "➕ New topic" if t == "NEW" else
                            f"{t} · {topics.set_index('topic_id').loc[t, 'topic']}")
        tnew = b2.text_input("New topic title (if new)")
        fpk = b3.selectbox("Package (blank = auto)", [""] + list(PKG), format_func=lambda k: PKG.get(k, "Auto-classify"))
        if st.form_submit_button("Add to tracker"):
            a, b = parse_chainage(fsum)
            if tsel == "NEW":
                tsel = f"TOP-N{len(topics) + 1:03d}"
                ttitle = tnew or fsum[:60]
            else:
                ttitle = topics.set_index("topic_id").loc[tsel, "topic"]
            row = {"entry_id": f"T-{len(tracker) + 1:04d}", "topic_id": tsel, "topic_title": ttitle,
                   "package_id": fpk or classify_package(fsum + " " + ttitle, packages), "location": fmt_location(a, b),
                   "ch_from": a, "ch_to": b, "source": fs, "date": pd.Timestamp(fd), "ref": fr,
                   "summary": scrub(fsum), "entry_type": "Correspondence", "work_package": "", "impacted_activities": ""}
            st.session_state.tracker = pd.concat([tracker, pd.DataFrame([row])], ignore_index=True)
            st.success(f"Added to {tsel} ({PKG.get(row['package_id'])}).")
            st.rerun()

    st.download_button("Download tracker (CSV)", st.session_state.tracker.to_csv(index=False).encode(),
                       "tracker.csv")
    st.caption("Changes live in this session. Download the CSV to keep them (database persistence is a next step).")

elif page == "Fragnet & narrative":
    st.title("Fragnet & monthly narrative")
    topics = topic_table(tracker)
    default = st.session_state.get("topic_sel", "TOP-LEDPETTIE")
    ids = topics["topic_id"].tolist()
    tid = st.selectbox("Topic", ids, index=ids.index(default) if default in ids else 0,
                       format_func=lambda t: f"{t} · {topics.set_index('topic_id').loc[t, 'topic']}")
    sub = tracker[tracker["topic_id"] == tid].sort_values("date").reset_index(drop=True)
    pkg_id = sub["package_id"].iloc[0]
    st.caption(f"Package: {PKG.get(pkg_id)} · Location: {topics.set_index('topic_id').loc[tid, 'location'] or '-'} · "
               f"{len(sub)} facts")

    st.markdown("**1. Facts to include in the fragnet** (record-based activities)")
    facts = sub[["date", "ref", "entry_type", "summary"]].copy()
    facts.insert(0, "include", sub["entry_type"].isin(KEY_TYPES - {"Meeting note"}) | (len(sub) <= 8))
    facts["date"] = facts["date"].dt.date
    facts = st.data_editor(facts, hide_index=True, width="stretch", key=f"facts_{tid}",
                           column_config={"summary": st.column_config.TextColumn(width="large")},
                           disabled=["date", "ref", "entry_type"])
    chosen = facts[facts["include"]]

    st.markdown("**2. Programme activities the constraint drives** (tie-in)")
    cur = [c for c in str(sub["impacted_activities"].iloc[0]).split(";") if c in ACT_LABEL]
    sug = AIDX.suggest(" ".join([str(sub["topic_title"].iloc[0])] + sub["summary"].astype(str).tolist()[:5]),
                       sub["ch_from"].dropna().iloc[0] if sub["ch_from"].notna().any() else None)
    opts = list(dict.fromkeys(cur + sug["task_code"].tolist() + list(ACT_LABEL)))
    impacted = st.multiselect("Activities", opts, default=cur, format_func=lambda c: ACT_LABEL.get(c, c),
                              key=f"imp_{tid}")

    st.markdown("**3. Forecast steps until the constraint is removed** (planner assumptions)")
    steps = pd.DataFrame(DEFAULT_STEPS.get(pkg_id, DEFAULT_STEPS["OTHER"]), columns=["name", "duration_days", "party"])
    steps = st.data_editor(steps, num_rows="dynamic", hide_index=True, width="stretch", key=f"steps_{tid}")
    data_date = st.date_input("Data date for this assessment", today)

    if chosen.empty:
        st.warning("Select at least one fact.")
        st.stop()
    fn = build_fragnet(chosen.assign(date=pd.to_datetime(chosen["date"])), steps.dropna(subset=["name"]),
                       impacted, data_date)
    release = fn[fn["type"] != "Tie-in"]["finish"].max()
    imp = impact(ACTS, RELS, impacted, release) if impacted else pd.DataFrame()

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Fragnet activities", int((fn["type"] != "Tie-in").sum()))
    m2.metric("Constraint removed (forecast)", f"{release:%d %b %Y}")
    m3.metric("Programme activities pushed", len(imp))
    m4.metric("Max delay (days)", int(imp["delay_days"].max()) if len(imp) else 0)

    g = fn[fn["type"] != "Tie-in"].copy()
    g["bar_end"] = pd.to_datetime(g["finish"]) + pd.Timedelta(days=1)
    g["label"] = g["code"] + " " + g["name"].str.slice(0, 55)
    fig = px.timeline(g, x_start="start", x_end="bar_end", y="label", color="basis",
                      color_discrete_map={"Actual (record)": "#888780", "Forecast": "#EF9F27"})
    fig.update_yaxes(autorange="reversed", title=None)
    fig.add_vline(x=pd.Timestamp(data_date).timestamp() * 1000, line_dash="dash", line_color="gray")
    fig.update_layout(height=max(260, 26 * len(g)), margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")
    with st.expander("Fragnet table (codes, dates, logic)"):
        st.dataframe(fn, hide_index=True, width="stretch")

    if len(imp):
        st.markdown("**Effect on the programme** (indicative forward pass, calendar days)")
        ms = imp[imp["is_milestone"] == True]  # noqa: E712
        if len(ms):
            st.warning("Key milestones pushed: " + "; ".join(
                f"{r.task_code} {r.task_name[:60]} (+{r.delay_days} d)" for r in ms.head(5).itertuples()))
        st.dataframe(imp[["task_code", "task_name", "finish", "new_finish", "delay_days", "total_float_days", "wbs"]],
                     hide_index=True, width="stretch", height=300)
        st.caption("Calculated outside P6 (calendar days, no calendars or constraints). Run the fragnet in P6 to confirm.")

    st.markdown("**4. Narrative for the monthly programme submission**")
    wp = sub["work_package"].iloc[0]
    narr = build_narrative(str(sub["topic_title"].iloc[0]), PKG.get(pkg_id, ""), pkg_id,
                           topics.set_index("topic_id").loc[tid, "location"], wp,
                           chosen.assign(date=pd.to_datetime(chosen["date"])), fn,
                           imp if len(imp) else pd.DataFrame(columns=["task_code", "task_name", "finish", "new_finish",
                                                                       "delay_days", "is_milestone"]),
                           impacted, ACTS, data_date, "P6 programme rev 017 (data date 22 May 2026)", contract)
    key = f"narr_{tid}"
    if ai_on and st.button("✨ Refine narrative with AI"):
        with st.spinner("Refining..."):
            st.session_state[key] = extractor.refine_narrative(narr)
    text = st.text_area("Narrative (edit before submitting)", st.session_state.get(key, narr), height=480)
    d1, d2, d3 = st.columns(3)
    d1.download_button("Fragnet for P6 (.xlsx)", to_p6_xlsx(fn), f"{tid}_fragnet.xlsx")
    d2.download_button("Narrative (.docx)", to_docx(text), f"{tid}_narrative.docx")
    d3.download_button("Narrative (.txt)", text, f"{tid}_narrative.txt")

elif page == "CE dashboard":
    st.title("Delay & compensation event dashboard")
    st.caption(contract)
    ev = event_table()
    active = prog[~prog["owner"].astype(str).str.startswith("Inactive")]
    late = active[(active["planned_finish"].dt.date < today) & (active["status"] != "Complete")]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents logged", len(docs))
    c2.metric("Potential CEs", len(ev))
    c3.metric("Linked activities past planned finish", len(late))
    if not ev.empty:
        nearest = min((d - today).days for d in ev["deadline_latest"])
        c4.metric("Days to nearest 61.3 deadline", nearest)

    st.subheader("Alerts")
    for r in ev.itertuples():
        lab, col = r.status_latest
        strict_red = r.status_strict[1] == "red"
        box = st.error if col == "red" else st.warning if (col == "orange" or strict_red) else st.success
        box(
            f"**{r.event_id} - {EVENT_TITLES.get(r.event_id, '')}**\n\nNot yet notified as a compensation event.\n\n"
            f"- Contractor's view: latest new fact **{r.latest_ref}** on {r.latest_new_fact:%d %b %Y} "
            f"→ notify by **{r.deadline_latest:%d %b %Y}** ({lab}).\n"
            f"- Client may argue awareness from **{r.first_ref}** on {r.first_aware:%d %b %Y} "
            f"→ 8 weeks ended {r.deadline_strict:%d %b %Y} ({r.status_strict[0]}).\n"
            f"- {r.blocked_activities} programme activities blocked. Clauses engaged: {r.clauses}"
        )

    st.subheader("Programme: planned vs. today")
    p = active.copy()
    p["days_late"] = p["planned_finish"].apply(lambda d: max(0, (today - d.date()).days))
    p["bar_end"] = p["planned_finish"] + pd.Timedelta(days=1)
    fig = px.timeline(p, x_start="planned_start", x_end="bar_end", y="activity", color="status",
                      hover_data=["activity_id", "days_late", "total_float_days", "depends_on"],
                      category_orders={"activity": p["activity"].tolist()},
                      color_discrete_map={"In progress": "#378ADD", "Not started": "#EF9F27", "Complete": "#888780"})
    fig.update_yaxes(title=None)
    fig.add_vline(x=pd.Timestamp(today).timestamp() * 1000, line_dash="dash", line_color="gray")
    fig.add_annotation(x=pd.Timestamp(today), y=1.06, yref="paper", text="today", showarrow=False)
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0))
    st.plotly_chart(fig, width="stretch")
    p["planned_finish"] = p["planned_finish"].dt.date
    st.dataframe(p[["activity_id", "activity", "planned_finish", "status", "total_float_days", "depends_on", "days_late"]],
                 hide_index=True, width="stretch")
    st.caption("Planned dates from P6 programme rev 017. 'days_late' = reference date minus planned finish for activities "
               "that were not complete at the programme data date.")

    st.subheader("Programme logic check")
    fnd = pd.read_csv(DATA / "programme_findings.csv")
    for f in fnd.itertuples():
        (st.error if f.severity == "high" else st.warning if f.severity == "medium" else st.info)(
            f"**{f.activity_id}** - {f.finding}")

elif page == "Timeline":
    st.title("Evidence timeline & 8-week windows")
    d = docs.sort_values("doc_date")
    fig = go.Figure()
    for r in d[d["awareness_trigger"].astype(str).str.lower() == "yes"].itertuples():
        end = pd.Timestamp(ce_deadline(r.doc_date.date()))
        fig.add_trace(go.Scatter(x=[r.doc_date, end], y=[r.ref, r.ref], mode="lines",
                                 line=dict(width=10, color="rgba(226,75,74,0.35)"), showlegend=False,
                                 hovertemplate=f"{r.ref}: notify by {end:%d %b %Y}<extra></extra>"))
    fig.add_trace(go.Scatter(x=d["doc_date"], y=d["ref"], mode="markers", marker=dict(size=12),
                             text=d["summary"], hovertemplate="%{y}<br>%{x|%d %b %Y}<br>%{text}<extra></extra>",
                             name="documents"))
    fig.add_vline(x=pd.Timestamp(today).timestamp() * 1000, line_dash="dash", line_color="gray")
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0),
                      yaxis=dict(categoryorder="array", categoryarray=d["ref"].tolist()[::-1]))
    st.plotly_chart(fig, width="stretch")
    st.caption("Red bars = 8-week notification window (cl. 61.3) opened by each new fact. Dashed line = reference date.")
    st.dataframe(d.assign(doc_date=d["doc_date"].dt.date), hide_index=True, width="stretch")

elif page == "EW scan":
    st.title("Retrospective scan: early warnings without a compensation event")
    scan = pd.read_csv(DATA / "ew_scan.csv", parse_dates=["notified"])
    scan["8 weeks from EW"] = scan["notified"].dt.date.apply(ce_deadline)
    scan["days since EW"] = scan["notified"].dt.date.apply(lambda d: (today - d).days if pd.notna(d) else None)
    scan["priority"] = scan["topic"].eq("Utilities").map({True: "1 - Utilities (Others)", False: "2 - Other"})
    scan["matched_ce"] = scan.get("matched_ce", pd.Series(dtype=str)).fillna("")
    hide_matched = st.toggle("Hide EWs already matched to a notified CE (P6 CE record)", value=True)
    if hide_matched:
        scan = scan[scan["matched_ce"] == ""]
    c1, c2, c3 = st.columns(3)
    c1.metric("EWs on Client / Others with no CE found", len(scan))
    c2.metric("Still open", int((scan["status"] == "Open").sum()))
    c3.metric("Utility EWs (cl. 60.1(5) candidates)", int(scan["topic"].eq("Utilities").sum()))
    st.info("These early warnings involve the Client, the PM or Others (utilities, authorities) and the EW register "
            "shows no compensation event reference. Each is a candidate to check against the CE register: if no CE was "
            "notified and the event is a CE, the 8-week time bar (cl. 61.3) may already have passed - the value at risk.")
    only_open = st.toggle("Only open EWs", value=True)
    topic = st.multiselect("Topic", sorted(scan["topic"].dropna().unique()))
    v = scan[(scan["status"] == "Open") if only_open else slice(None)]
    if topic:
        v = v[v["topic"].isin(topic)]
    v = v.sort_values(["priority", "notified"], ascending=[True, False])
    v["notified"] = v["notified"].dt.date
    st.dataframe(v[["priority", "ew_id", "topic", "title", "notified", "status", "matched_ce", "8 weeks from EW", "days since EW"]],
                 hide_index=True, width="stretch")
    st.caption("Keyword screen of the EW register of 01 Oct 2026, matched by title against the CE record in P6 rev 017 "
               "(CE-001 to CE-037). CEs notified after May 2026 are not in that record - confirm against CEMAR.")

elif page == "Ingest documents":
    st.title("Ingest new documents")
    st.info("Upload letters, e-mails (.eml), PDFs or Word files. The system reads each one, proposes the package, "
            "topic, location and programme activities, and adds it to the tracker. E-mails and phone numbers are "
            "redacted before processing. In production this runs automatically from the project mailbox / CEMAR.")
    files = st.file_uploader("Documents", type=["pdf", "docx", "txt", "eml"], accept_multiple_files=True)
    topics = topic_table(tracker)
    pk_txt = "\n".join(f"{k}: {v}" for k, v in PKG.items())
    tp_txt = "\n".join(f"{r.topic_id}: {r.topic}" for r in topics.head(150).itertuples())
    events = sorted(docs["event_id"].dropna().unique().tolist())
    for f in files or []:
        ck = f"ing_{f.name}_{f.size}"
        if ck not in st.session_state:
            with st.spinner(f"Reading {f.name}..."):
                st.session_state[ck] = extractor.extract(f.name, f.getvalue(), "", pk_txt, tp_txt)
        res, text = st.session_state[ck]
        with st.expander(f"📄 {f.name}  ·  engine: {res.get('engine')}", expanded=True):
            if res.get("potential_ce"):
                st.warning(f"Potential compensation event - {res.get('reasoning', '')}")
            else:
                st.caption(res.get("reasoning", ""))
            a, b = parse_chainage(f"{res.get('location', '')} {text[:3000]}")
            pkg_guess = res.get("package_id") if res.get("package_id") in PKG else classify_package(text[:5000], packages)
            topic_guess = res.get("topic_id") if res.get("topic_id") in topics["topic_id"].tolist() else "NEW"
            sug = AIDX.suggest(f"{res.get('summary', '')} {res.get('affected_activity', '')} {res.get('work_package', '')}", a)
            with st.form(f"form_{ck}"):
                c1, c2, c3 = st.columns(3)
                dd = c1.date_input("Document date", pd.to_datetime(res.get("doc_date") or today).date())
                ref = c2.text_input("Reference", res.get("ref", ""))
                typ = c3.text_input("Type", res.get("doc_type", ""))
                summary = st.text_area("Fact (summary)", res.get("summary", ""))
                c4, c5, c6 = st.columns(3)
                pkg = c4.selectbox("Package", list(PKG), index=list(PKG).index(pkg_guess), format_func=lambda k: PKG[k])
                tids = ["NEW"] + topics["topic_id"].tolist()
                tsel = c5.selectbox("Topic", tids, index=tids.index(topic_guess),
                                    format_func=lambda t: "➕ New topic" if t == "NEW" else
                                    f"{t} · {topics.set_index('topic_id').loc[t, 'topic']}")
                loc = c6.text_input("Location", fmt_location(a, b) or res.get("location", ""))
                wp = c4.text_input("Work package", res.get("work_package", res.get("affected_activity", "")))
                tnew = c5.text_input("New topic title (if new)", res.get("summary", "")[:60])
                clauses = c6.text_input("NEC4 clauses", res.get("clauses", ""))
                imp_sel = st.multiselect("Programme activities impacted", sug["task_code"].tolist(),
                                         format_func=lambda c: ACT_LABEL.get(c, c))
                e1, e2 = st.columns(2)
                link_ce = e1.selectbox("Also log as CE evidence", ["No"] + events + ["NEW"])
                trig = e2.checkbox("New fact (starts 8-week clock)", value=bool(res.get("potential_ce")))
                if st.form_submit_button("Add to tracker"):
                    if tsel == "NEW":
                        tsel, ttitle = f"TOP-N{len(topics) + 1:03d}", tnew
                    else:
                        ttitle = topics.set_index("topic_id").loc[tsel, "topic"]
                    row = {"entry_id": f"T-{len(tracker) + 1:04d}", "topic_id": tsel, "topic_title": ttitle,
                           "package_id": pkg, "location": loc, "ch_from": a, "ch_to": b, "source": "Upload",
                           "date": pd.Timestamp(dd), "ref": ref, "summary": scrub(summary), "entry_type": typ or "Correspondence",
                           "work_package": wp, "impacted_activities": ";".join(imp_sel)}
                    st.session_state.tracker = pd.concat([tracker, pd.DataFrame([row])], ignore_index=True)
                    if link_ce != "No":
                        eid = link_ce if link_ce != "NEW" else f"CE-{len(events) + 1:02d}"
                        new = {"doc_date": pd.Timestamp(dd), "ref": ref, "doc_type": typ, "sender": res.get("sender", ""),
                               "summary": scrub(summary), "location": loc, "affected_activity": wp, "clauses": clauses,
                               "event_id": eid, "awareness_trigger": "yes" if trig else "no"}
                        st.session_state.docs = pd.concat([docs, pd.DataFrame([new])], ignore_index=True)
                    st.success(f"Added {ref} to {tsel} ({PKG[pkg]}).")
            with st.popover("View redacted text"):
                st.text(text[:5000])

elif page == "Draft notification":
    st.title("Draft compensation event notification (cl. 61.3)")
    ev_ids = sorted(docs["event_id"].dropna().unique())
    eid = st.selectbox("Event", ev_ids)
    g = docs[docs["event_id"] == eid]
    all_cl = sorted({c.strip() for s in g["clauses"].dropna() for c in str(s).split(";")
                     if c.strip().startswith(("60.1", "14.3"))})
    title = st.text_input("Subject", EVENT_TITLES.get(eid, ""))
    clauses = st.multiselect("Clauses relied on", all_cl, default=all_cl)
    draft = build_notice(eid, title, contract, clauses, g, prog, today)
    if ai_on and st.button("✨ Refine wording with AI"):
        with st.spinner("Refining..."):
            draft = extractor.refine_notice(draft)
    text = st.text_area("Draft (edit before issuing)", draft, height=520)
    c1, c2, c3 = st.columns(3)
    c1.download_button("Download .docx", to_docx(text), f"{eid}_CE_notification.docx")
    c2.download_button("Download .txt", text, f"{eid}_CE_notification.txt")
    if c3.button("Mark as notified"):
        st.session_state.notified.add(eid)
        st.success("Clock stopped. Next: PM must reply within 1 week (cl. 61.4) - "
                   f"by {today + timedelta(days=7):%d %b %Y}.")
    st.caption("Draft for review by the commercial team. Issue it through the contract's communication system "
               "(e.g. CEMAR), separate from other communications (cl. 13.7).")

else:
    st.title("NEC4 ECC rule base")
    st.caption("Paraphrased time limits and triggers. Check the signed contract, including Z clauses, which may change them.")
    st.dataframe(pd.DataFrame(RULES), hide_index=True, width="stretch")
