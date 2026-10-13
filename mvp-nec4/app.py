"""NEC4 Delay & Compensation Event Tracker - MVP (Streamlit)."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from core import extractor
from core.nec4_rules import RULES, ce_deadline, status_for
from core.notice import build_notice, to_docx

load_dotenv()
DATA = Path(__file__).parent / "data"
st.set_page_config(page_title="NEC4 CE Tracker", page_icon="🚧", layout="wide")


# ---------- data ----------
@st.cache_data
def load_csv(name, dates):
    return pd.read_csv(DATA / name, parse_dates=dates)


if "docs" not in st.session_state:
    st.session_state.docs = load_csv("documents.csv", ["doc_date"])
    st.session_state.programme = load_csv("programme.csv", ["planned_start", "planned_finish"])
    st.session_state.notified = set()

docs: pd.DataFrame = st.session_state.docs
prog: pd.DataFrame = st.session_state.programme

# ---------- sidebar ----------
st.sidebar.title("🚧 NEC4 CE Tracker")
st.sidebar.caption("MVP - delay & compensation event control")
page = st.sidebar.radio("Go to", ["Dashboard", "Timeline", "EW scan", "Ingest documents", "Draft notification", "NEC4 rules"])
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
if page == "Dashboard":
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
    c1, c2, c3 = st.columns(3)
    c1.metric("EWs on Client / Others with no CE reference", len(scan))
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
    st.dataframe(v[["priority", "ew_id", "topic", "title", "notified", "status", "8 weeks from EW", "days since EW"]],
                 hide_index=True, width="stretch")
    st.caption("Keyword screen of the EW register of 01 Oct 2026. The CE register is needed to confirm which were notified.")

elif page == "Ingest documents":
    st.title("Ingest new documents")
    st.info("Upload letters, e-mails (.eml), PDFs or Word files. E-mails and phone numbers are redacted before "
            "processing; AI is instructed to use roles, never personal names (UK GDPR data minimisation). "
            "In production this step runs automatically from the project mailbox / SharePoint / CEMAR.")
    files = st.file_uploader("Documents", type=["pdf", "docx", "txt", "eml"], accept_multiple_files=True)
    acts = "\n".join(f"{a.activity_id}: {a.activity}" for a in prog.itertuples())
    events = sorted(docs["event_id"].dropna().unique().tolist()) + ["NEW"]
    for f in files or []:
        with st.spinner(f"Reading {f.name}..."):
            res, text = extractor.extract(f.name, f.getvalue(), acts)
        with st.expander(f"📄 {f.name}  ·  engine: {res.get('engine')}", expanded=True):
            if res.get("potential_ce"):
                st.warning(f"Potential compensation event - {res.get('reasoning', '')}")
            else:
                st.caption(res.get("reasoning", ""))
            with st.form(f"form_{f.name}"):
                c1, c2, c3 = st.columns(3)
                dd = c1.date_input("Document date", pd.to_datetime(res.get("doc_date") or today).date())
                ref = c2.text_input("Reference", res.get("ref", ""))
                typ = c3.text_input("Type", res.get("doc_type", ""))
                sender = c1.text_input("Sender (role)", res.get("sender", ""))
                loc = c2.text_input("Location", res.get("location", ""))
                act = c3.text_input("Affected activity", res.get("affected_activity", ""))
                summary = st.text_area("Summary", res.get("summary", ""))
                c4, c5, c6 = st.columns(3)
                clauses = c4.text_input("NEC4 clauses", res.get("clauses", ""))
                eid = c5.selectbox("Link to event", events)
                trig = c6.checkbox("New fact (starts 8-week clock)", value=bool(res.get("potential_ce")))
                if st.form_submit_button("Add to log"):
                    if eid == "NEW":
                        eid = f"CE-{len(events):02d}"
                    new = {"doc_date": pd.Timestamp(dd), "ref": ref, "doc_type": typ, "sender": sender,
                           "summary": summary, "location": loc, "affected_activity": act, "clauses": clauses,
                           "event_id": eid, "awareness_trigger": "yes" if trig else "no"}
                    st.session_state.docs = pd.concat([docs, pd.DataFrame([new])], ignore_index=True)
                    st.success(f"Added {ref} to {eid}. Dashboard updated.")
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
