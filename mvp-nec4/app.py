"""NEC4 Delay & Compensation Event Tracker - MVP (Streamlit)."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from core import extractor
from core.fragnet import DEFAULT_STEPS, KEY_TYPES, build_fragnet, impact, time_or_cost, to_p6_xlsx
from core.narrative import build_narrative
from core.nec4_rules import RULES, ce_deadline, status_for
from core.notice import build_notice, to_docx
from core.search import Index, around_document, build_corpus, parse_query, summary_ai, summary_offline
from core.tracker import (ActivityIndex, classify_package, fmt_location, fmt_micro, parse_chainage, parse_micro,
                          scrub)

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
    _t = load_csv("tracker.csv", ["date"]).copy()
    for _c in ["work_package", "impacted_activities", "location", "phase", "zone", "direction"]:
        if _c not in _t:
            _t[_c] = ""
        _t[_c] = _t[_c].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    st.session_state.tracker = _t
    st.session_state.packages = load_csv("packages.csv", None)


@st.cache_resource
def programme_data():
    acts = pd.read_csv(DATA / "activities.csv")
    if "service_package" not in acts:
        acts["service_package"] = ""
    acts["service_package"] = acts["service_package"].fillna("")
    rels = pd.read_csv(DATA / "relationships.csv")
    return acts, rels, ActivityIndex(acts)


@st.cache_data
def registers():
    """PMI register, full EW register and EW -> PMI -> CE chain (built by scripts/build_data.py)."""
    def _r(name, dates):
        f = DATA / name
        return pd.read_csv(f, parse_dates=dates, keep_default_na=False) if f.exists() else pd.DataFrame()
    pmi = _r("pmi_register.csv", ["date"])
    if len(pmi):
        pmi["date"] = pd.to_datetime(pmi["date"], errors="coerce")
        pmi["date_change"] = pmi["date_change"].astype(str).eq("True")
    ew = _r("ew_register.csv", [])
    if len(ew):
        ew["notified"] = pd.to_datetime(ew["notified"], errors="coerce")
    return pmi, ew, _r("chain.csv", []), _r("cemar_ce.csv", [])


@st.cache_data
def cemar_registers():
    """Official CEMAR exports: CE, TQ and GC registers (no author names)."""
    def _r(name, dates):
        f = DATA / name
        if not f.exists():
            return pd.DataFrame()
        d = pd.read_csv(f, keep_default_na=False)
        for c in dates:
            d[c] = pd.to_datetime(d[c], errors="coerce")
        for c in ["days_late", "price", "days"]:
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        return d
    return (_r("ce_register.csv", ["notified", "implemented", "quotation_due", "assessment_due"]),
            _r("tq_register.csv", ["notified", "reply_due", "reply_date"]),
            _r("gc_register.csv", ["date", "reply_due"]))


@st.cache_resource
def search_index():
    pmi, ew, _, cc = registers()
    ce, tq, gc = cemar_registers()
    corpus = build_corpus(load_csv("tracker.csv", ["date"]).fillna(""), pmi, ew, cc, ce, tq, gc)
    return corpus, Index(corpus)


@st.cache_data(max_entries=200)
def suggest_cached(text: str, ch):
    return AIDX.suggest(text, None if ch is None or pd.isna(ch) else float(ch))


docs: pd.DataFrame = st.session_state.docs
prog: pd.DataFrame = st.session_state.programme
tracker: pd.DataFrame = st.session_state.tracker
packages: pd.DataFrame = st.session_state.packages
PKG = dict(zip(packages.package_id, packages.package)) | {"OTHER": "Other (unclassified)"}
ACTS, RELS, AIDX = programme_data()
PMI, EWR, CHAIN, CEMAR_CE = registers()
CER, TQR, GCR = cemar_registers()
SERVICE = ACTS.set_index("task_code")["service_package"].to_dict()
ACT_LABEL = {r.task_code: f"{r.task_code} · {r.task_name[:80]}" for r in ACTS.itertuples()}

# ---------- sidebar ----------
st.sidebar.title("🚧 NEC4 CE Tracker")
st.sidebar.caption("MVP - delay & compensation event control")
PAGES = ["Tracker", "Ask the project", "Theme search", "Fragnet & narrative", "EW → PMI → CE chain", "PMI register",
         "CEMAR registers", "Ingest documents", "CE dashboard", "Timeline", "EW scan", "Draft notification", "NEC4 rules"]
if st.session_state.get("goto") in PAGES:
    st.session_state["nav"] = st.session_state.pop("goto")
page = st.sidebar.radio("Go to", PAGES, key="nav")

st.sidebar.markdown("**Filters (apply to all tabs)**")
_chs = tracker["ch_from"].dropna()
CH_MAX = int(_chs.max()) + 100 if len(_chs) else 12000
F_PKG = st.sidebar.multiselect("Main topic / package", list(PKG), format_func=lambda k: PKG[k], key="f_pkg")
F_TXT = st.sidebar.text_input("Search text / reference", key="f_txt")
F_CH = st.sidebar.slider("Location (chainage, m)", 0, CH_MAX, (0, CH_MAX), step=50, key="f_ch")
F_CH_ON = F_CH != (0, CH_MAX)


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    v = df
    if F_PKG:
        v = v[v["package_id"].isin(F_PKG)]
    if F_TXT:
        v = v[v["summary"].str.contains(F_TXT, case=False, na=False) | v["ref"].str.contains(F_TXT, case=False, na=False)
              | v["topic_title"].str.contains(F_TXT, case=False, na=False)]
    if F_CH_ON:
        v = v[v["ch_from"].between(*F_CH) | v["ch_to"].between(*F_CH)]
    return v


def filter_note():
    parts = []
    if F_PKG:
        parts.append("topic: " + ", ".join(PKG[k] for k in F_PKG))
    if F_TXT:
        parts.append(f"text: '{F_TXT}'")
    if F_CH_ON:
        parts.append(f"CH {F_CH[0]}-{F_CH[1]}")
    if parts:
        st.info("Filtered by " + " · ".join(parts) + " (change in the sidebar).")
today = st.sidebar.date_input("Reference date", date.today(), help="Change to simulate another day.")
contract = st.sidebar.text_input("Contract", "A9 Dualling - Tay Crossing to Ballinluig (NEC4 ECC)")
ai_on = bool(extractor.os.getenv("ANTHROPIC_API_KEY"))
st.sidebar.markdown(f"**AI extraction:** {'🟢 on (' + extractor.MODEL + ')' if ai_on else '⚪ off - keyword rules'}")
st.sidebar.caption("If the page shows 'Connecting…' or goes blank, reload it (F5). Edits made in the session are "
                   "kept only until reload - download the tracker CSV to keep them.")
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
EVENT_KEYS = {"CE-01": "ledpettie|lb5574|lb 5574", "CE-02": "GC-1385"}


def cemar_status(eid: str) -> str:
    """What the official CEMAR CE register says about one tracked event."""
    k = EVENT_KEYS.get(eid)
    if CER.empty or not k:
        return "Not found in the CE register."
    hit = CER[CER["title"].str.contains(k, case=False, regex=True)]
    if hit.empty:
        return "No compensation event found in the CEMAR CE register for this event."
    return "CEMAR CE register: " + "; ".join(
        f"**{h.ce}** '{h.title}' notified {h.notified:%d %b %Y} by the {h['from']} - status **{h.status}**"
        for _, h in hit.iterrows()) + (". A CE the PM did not accept can still be disputed, or a new CE notified "
                                       "if new facts arise (each new fact opens a new 8-week window)."
                                       if (hit.status == "NOT ACCEPTED").all() else ".")

# ---------- pages ----------
def _first(s):
    return next((x for x in s if isinstance(x, str) and x and x != "nan"), "")


def service_of(codes: str) -> str:
    """Level 2 of the classification: P6 service package(s) of the linked activities."""
    sp = [SERVICE.get(c, "") for c in str(codes).split(";") if c]
    return "; ".join(dict.fromkeys(x for x in sp if x))


def topic_table(tr: pd.DataFrame) -> pd.DataFrame:
    g = tr.groupby("topic_id")
    out = pd.DataFrame({
        "topic": g["topic_title"].first(), "package": g["package_id"].first().map(PKG),
        "micro": [fmt_micro(_first(x.phase), _first(x.zone), _first(x.direction)) for _, x in g],
        "location": g["location"].agg(lambda s: next((x for x in s if isinstance(x, str) and x), "")),
        "first_fact": g["date"].min().dt.date, "last_fact": g["date"].max().dt.date, "facts": g.size(),
        "impacted_activities": g["impacted_activities"].agg(lambda s: next((x for x in s if isinstance(x, str) and x), "")),
    }).reset_index()
    out["service_package"] = out["impacted_activities"].map(service_of)
    out["days_since_last_fact"] = out["last_fact"].map(lambda d: (today - d).days if pd.notna(d) else None)
    return out.sort_values("last_fact", ascending=False)


if page == "Tracker":
    st.title("Delay tracker")
    st.caption("Every fact recorded, classified by package, work package and location, grouped into topics. "
               "Select a topic to see its history or turn it into a fragnet.")
    fv = apply_filters(tracker)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Facts recorded", len(fv))
    c2.metric("Topics", fv["topic_id"].nunique())
    c3.metric("Facts in last 30 days", int((fv["date"].dt.date >= today - timedelta(days=30)).sum()))
    c4.metric("Topics linked to programme", int((topic_table(fv)["impacted_activities"] != "").sum()) if len(fv) else 0)

    filter_note()
    v = apply_filters(tracker)
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
        st.session_state["topic_sel"] = tt.iloc[ev.selection.rows[0]]["topic_id"]
    tid = st.session_state.get("topic_sel")
    if tid and tid in tracker["topic_id"].values:
        if st.button("✖ Clear selected topic"):
            st.session_state.pop("topic_sel", None)
            st.rerun()
        sub = tracker[tracker["topic_id"] == tid].sort_values("date")
        st.markdown(f"**{tid} · {sub['topic_title'].iloc[0]}**")
        st.caption("Classification — **Macro theme:** " + str(PKG.get(sub['package_id'].iloc[0])) +
                   " · **P6 service package:** " + (service_of(sub['impacted_activities'].iloc[0]) or "link activities below") +
                   " · **Micro:** " + (fmt_micro(_first(sub.phase), _first(sub.zone), _first(sub.direction),
                                                  _first(sub.location)) or "-"))
        st.dataframe(sub[["date", "ref", "entry_type", "summary", "location", "source"]].assign(
            date=sub["date"].dt.date), hide_index=True, width="stretch")
        with st.form("edit_topic"):
            e1, e2 = st.columns(2)
            new_pkg = e1.selectbox("Package", list(PKG), index=list(PKG).index(sub["package_id"].iloc[0])
                                   if sub["package_id"].iloc[0] in PKG else 0, format_func=lambda k: PKG[k])
            wp = e2.text_input("Work package impacted", sub["work_package"].iloc[0])
            cur = [c for c in str(sub["impacted_activities"].iloc[0]).split(";") if c in ACT_LABEL]
            sug = suggest_cached(" ".join(sub["topic_title"].astype(str).tolist() + sub["summary"].astype(str).tolist()[:5]),
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
            row = {"entry_id": f"T-{len(tracker) + 1:04d}", "topic_id": tsel, "topic_title": ttitle, **parse_micro(fsum),
                   "package_id": fpk or classify_package(fsum + " " + ttitle, packages), "location": fmt_location(a, b),
                   "ch_from": a, "ch_to": b, "source": fs, "date": pd.Timestamp(fd), "ref": fr,
                   "summary": scrub(fsum), "entry_type": "Correspondence", "work_package": "", "impacted_activities": ""}
            st.session_state.tracker = pd.concat([tracker, pd.DataFrame([row])], ignore_index=True)
            st.success(f"Added to {tsel} ({PKG.get(row['package_id'])}).")
            st.rerun()

    st.download_button("Download tracker (CSV)", st.session_state.tracker.to_csv(index=False).encode(),
                       "tracker.csv")
    st.caption("Changes live in this session. Download the CSV to keep them (database persistence is a next step).")

elif page == "Ask the project":
    st.title("Ask the project")
    st.caption("Ask in plain language (Portuguese or English) or pick one document. The search runs over every "
               "tracker fact, PMI, early warning and CE and is free; the AI is used only if you click "
               "'Summarise with AI', and it must cite the references.")
    corpus, idx = search_index()
    mode = st.radio("Search by", ["Question", "Document (before and after)"], horizontal=True, key="ask_mode")
    if mode == "Question":
        examples = ["issues at the bridge near CH 1600 since March and what the PM replied",
                    "beavers at CH 6500", "Openreach delays", "Guay Farmhouse access and demolition"]
        ex = st.selectbox("Examples", [""] + examples, key="ask_ex")
        q = st.text_input("Your question", ex, key=f"ask_q_{ex}",
                          placeholder="e.g. what happened with the bridge at CH 1600 since March?")
        if not q:
            st.stop()
        since, ch = parse_query(q)
        hits = idx.search(q, k=int(st.slider("Max results", 10, 60, 30, key="ask_k")), since=since, ch=ch)
        notes = [f"since {since:%b %Y}" if since is not None else "", f"near CH {ch}" if ch else ""]
        st.caption("Understood filters: " + (", ".join(n for n in notes if n) or "none") +
                   " · synonyms added: " + ", ".join(sorted(set(idx.expand(q)) - set(q.lower().split())))[:200])
        if hits.empty:
            st.warning("Nothing found. Try other words or a reference (EW-95, PMI-30, GC-1385).")
            st.stop()
        sel = hits.sort_values("date")
        key = "ask_" + q
    else:
        refs = sorted(corpus["ref"].unique(), key=lambda r: (r.split("-")[0], int(r.split("-")[1]) if r.split("-")[-1].isdigit() else 0))
        ref = st.selectbox("Document", refs, index=refs.index("GC-1385") if "GC-1385" in refs else 0, key="ask_ref")
        win = st.slider("Window (days before and after)", 30, 720, 365, step=30, key="ask_win")
        anchor, sel = around_document(corpus, ref, win, idx)
        if anchor is None:
            st.warning("Document not found.")
            st.stop()
        st.info(f"**{anchor.label}** · {anchor.date:%d %b %Y} · {anchor.kind}\n\n{' '.join(str(anchor.text).split())[:500]}")
        b, a = (sel["when"] == "before").sum(), (sel["when"] == "after").sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Before", int(b))
        c2.metric("After", int(a))
        c3.metric("References linked", len(set().union(*sel["refs"])) if len(sel) else 0)
        q = f"What happened before and after {anchor.label}?"
        key = "ask_doc_" + ref
    k1, k2, k3 = st.columns(3)
    k1.metric("Records found", len(sel))
    k2.metric("PMIs", int((sel["source"] == "PMI").sum()))
    k3.metric("CEs", int((sel["source"] == "CE (from PMIs)").sum()))
    tl = sel.copy()
    tl["y"] = tl["source"]
    fig = px.scatter(tl, x="date", y="y", color="source", hover_data={"label": True, "kind": True, "y": False},
                     height=230)
    fig.update_traces(marker=dict(size=11))
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_title=None, xaxis_title=None, showlegend=False)
    st.plotly_chart(fig, width="stretch")
    st.markdown("**Answer**")
    if ai_on and st.button("✨ Summarise with AI (uses credits)", key="ask_ai_btn"):
        with st.spinner("Writing the answer from the records found..."):
            st.session_state[key] = summary_ai(q, sel, extractor.MODEL)
    if st.session_state.get(key):
        st.success(st.session_state[key])
    else:
        st.caption("Chronology of the records found (no AI):")
        st.markdown(summary_offline(sel))
    show = sel.assign(include=True, date=sel["date"].dt.date)
    cols = ["include", "date", "label", "kind", "source", "topic", "text"] + [c for c in ["when", "link"] if c in show]
    ed = st.data_editor(show[cols], hide_index=True, width="stretch", height=360, key="ask_tbl_" + key,
                        disabled=[c for c in cols if c != "include"],
                        column_config={"text": st.column_config.TextColumn("content", width="large")})
    if st.button("Build fragnet from the ticked records ➜", type="primary"):
        pick = sel.loc[ed[ed["include"]].index]
        st.session_state["mixed"] = {
            "facts": pd.DataFrame({"date": pick["date"], "ref": pick["label"], "entry_type": pick["kind"],
                                   "summary": pick["text"].str.slice(0, 400)}),
            "title": q[:60], "package_id": pick["package_id"].mode().iat[0] if len(pick) else "OTHER"}
        st.session_state["topic_sel"] = "MIXED"
        st.session_state["goto"] = "Fragnet & narrative"
        st.rerun()

elif page == "Theme search":
    st.title("Theme search - build one fragnet from several topics")
    st.caption("Search every fact in the tracker, every PMI and every early warning (open and closed) for a word, a "
               "place or a reference - e.g. 'beaver', 'Kindallachan', 'CH 6500', 'Guay'. Tick the facts that belong "
               "to the same delay and send them to the Fragnet as one mixed selection.")
    q = st.text_input("Search for", st.session_state.get("theme_q", ""), placeholder="beaver, Kindallachan, Guay, GC-1385 ...")
    st.session_state["theme_q"] = q
    c1, c2, c3 = st.columns(3)
    use_tr = c1.checkbox("Tracker facts", True)
    use_pmi = c2.checkbox("PMIs", True)
    use_ew = c3.checkbox("EW register (incl. closed)", True)
    if not q:
        st.info("Type a word above. The sidebar filters (topic, chainage) also apply to tracker facts.")
        st.stop()
    rows = []
    if use_tr:
        t = apply_filters(tracker)
        m = (t["summary"].str.contains(q, case=False, na=False) | t["ref"].str.contains(q, case=False, na=False)
             | t["topic_title"].str.contains(q, case=False, na=False) | t["location"].str.contains(q, case=False, na=False))
        for r in t[m].itertuples():
            rows.append({"date": r.date, "source": "Tracker", "ref": r.ref, "entry_type": r.entry_type,
                         "summary": r.summary, "topic": f"{r.topic_id} · {r.topic_title}", "package_id": r.package_id,
                         "status": "", "absorbed_by": ""})
    if use_pmi and len(PMI):
        m = PMI["title"].str.contains(q, case=False) | PMI["summary"].str.contains(q, case=False) | PMI["pmi"].eq(q.upper())
        for r in PMI[m].itertuples():
            rows.append({"date": r.date, "source": "PMI", "ref": r.pmi, "entry_type": "PM Instruction",
                         "summary": f"{r.title} - {r.summary[:300]}", "topic": r.kind, "package_id": r.package_id,
                         "status": "date change" if r.date_change else "", "absorbed_by": r.ce_refs})
    if use_ew and len(EWR):
        m = EWR["title"].str.contains(q, case=False) | EWR["last_note"].str.contains(q, case=False) | EWR["ew_id"].eq(q.upper())
        for r in EWR[m].itertuples():
            rows.append({"date": r.notified, "source": "EW register", "ref": r.ew_id, "entry_type": "Early warning raised",
                         "summary": f"{r.title} - {r.last_note}", "topic": r.close_reason or "Open",
                         "package_id": r.package_id, "status": r.status, "absorbed_by": r.absorbed_by})
    res = pd.DataFrame(rows)
    if res.empty:
        st.warning("Nothing found.")
        st.stop()
    res["date"] = pd.to_datetime(res["date"], errors="coerce")
    res = res.dropna(subset=["date"]).sort_values("date").drop_duplicates(["ref", "date", "summary"])
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Results", len(res))
    k2.metric("Topics touched", res.loc[res.source == "Tracker", "topic"].nunique())
    k3.metric("PMIs", int((res.source == "PMI").sum()))
    k4.metric("Closed EWs", int(((res.source == "EW register") & res.status.str.contains("los", na=False)).sum()))
    res.insert(0, "include", False)
    res["date"] = res["date"].dt.date
    res["package"] = res["package_id"].map(PKG)
    ed = st.data_editor(res.drop(columns=["package_id"]), hide_index=True, width="stretch", height=420,
                        key=f"theme_{q}", disabled=[c for c in res.columns if c != "include"],
                        column_config={"summary": st.column_config.TextColumn(width="large"),
                                       "absorbed_by": st.column_config.TextColumn("absorbed by / CE")})
    sel = ed[ed["include"]]
    st.caption("'absorbed by / CE' shows, for a closed EW, the EW, PMI or CE that took it over; for a PMI, the CE it "
               "belongs to. Closed EWs stay visible so the full history is not lost.")
    b1, b2 = st.columns(2)
    if b1.button(f"Build fragnet from {len(sel)} selected ➜", disabled=sel.empty, type="primary"):
        pk = res.loc[sel.index, "package_id"].value_counts()
        st.session_state["mixed"] = {
            "facts": sel.assign(date=pd.to_datetime(sel["date"]))[["date", "ref", "entry_type", "summary"]],
            "title": f"Theme: {q}", "package_id": pk.index[0] if len(pk) else "OTHER"}
        st.session_state["topic_sel"] = "MIXED"
        st.session_state["goto"] = "Fragnet & narrative"
        st.rerun()
    b2.download_button("Download results (CSV)", res.to_csv(index=False).encode(), f"theme_{q}.csv")

elif page == "Fragnet & narrative":
    st.title("Fragnet & monthly narrative")
    filter_note()
    topics = topic_table(apply_filters(tracker))
    mixed = st.session_state.get("mixed")
    default = st.session_state.get("topic_sel", "TOP-LEDPETTIE")
    ids = (["MIXED"] if mixed else []) + topics["topic_id"].tolist()
    if not ids:
        st.warning("No topic matches the filters in the sidebar.")
        st.stop()
    tlab = topics.set_index("topic_id")["topic"].to_dict()
    tid = st.selectbox("Topic", ids, index=ids.index(default) if default in ids else 0,
                       format_func=lambda t: f"🧩 Mixed selection - {mixed['title']}" if t == "MIXED" else f"{t} · {tlab[t]}")
    st.session_state["topic_sel"] = tid
    if tid == "MIXED":
        sub = mixed["facts"].copy().reset_index(drop=True)
        for c, v in {"topic_title": mixed["title"], "package_id": mixed["package_id"], "impacted_activities": "",
                     "work_package": "", "ch_from": None, "location": ""}.items():
            sub[c] = v
        pkg_id = st.selectbox("Main theme for clauses and default steps", list(PKG), index=list(PKG).index(mixed["package_id"])
                              if mixed["package_id"] in PKG else 0, format_func=lambda k: PKG[k])
        loc = ""
        if st.button("✖ Discard mixed selection"):
            st.session_state.pop("mixed", None)
            st.session_state.pop("topic_sel", None)
            st.rerun()
    else:
        sub = tracker[tracker["topic_id"] == tid].sort_values("date").reset_index(drop=True)
        pkg_id = sub["package_id"].iloc[0]
        loc = topics.set_index("topic_id").loc[tid, "location"]
    st.caption(f"Package: {PKG.get(pkg_id)} · Location: {loc or '-'} · {len(sub)} facts")

    st.markdown("**1. Facts to include in the fragnet** (record-based activities)")
    facts = sub[["date", "ref", "entry_type", "summary"]].copy()
    facts.insert(0, "include", sub["entry_type"].isin(KEY_TYPES - {"Meeting note"}) | (len(sub) <= 8) | (tid == "MIXED"))
    if tid != "MIXED" and pkg_id != "OTHER" and len(PMI):
        rel = PMI[(PMI["package_id"] == pkg_id) & ~PMI["pmi"].isin(facts["ref"])]
        if len(rel):
            facts = pd.concat([facts, pd.DataFrame({
                "include": False, "date": rel["date"], "ref": rel["pmi"], "entry_type": "PM Instruction",
                "summary": rel["title"] + (" [date change: " + rel["dates_mentioned"] + "]").where(rel["date_change"], "")})],
                ignore_index=True).sort_values("date")
            st.caption(f"{len(rel)} PMI(s) on the same theme were added below, unticked. Tick them if they belong to "
                       "this delay (e.g. a PMI that moved the access date).")
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    facts = st.data_editor(facts, hide_index=True, width="stretch", key=f"facts_{tid}",
                           column_config={"summary": st.column_config.TextColumn(width="large")},
                           disabled=["date", "ref", "entry_type"])
    chosen = facts[facts["include"]]

    st.markdown("**2. Programme activities the constraint drives** (tie-in)")
    cur = [c for c in str(sub["impacted_activities"].iloc[0]).split(";") if c in ACT_LABEL]
    sug = suggest_cached(" ".join([str(sub["topic_title"].iloc[0])] + sub["summary"].astype(str).tolist()[:5]),
                         sub["ch_from"].dropna().iloc[0] if sub["ch_from"].notna().any() else None)
    opts = list(dict.fromkeys(cur + sug["task_code"].tolist() + list(ACT_LABEL)))
    impacted = st.multiselect("Activities", opts, default=cur, format_func=lambda c: ACT_LABEL.get(c, c),
                              key=f"imp_{tid}")

    st.markdown("**3. Forecast steps until the constraint is removed** (planner assumptions)")
    steps = pd.DataFrame(DEFAULT_STEPS.get(pkg_id, DEFAULT_STEPS["OTHER"]), columns=["name", "duration_days", "party"])
    steps = st.data_editor(steps, num_rows="dynamic", hide_index=True, width="stretch", key=f"steps_{tid}_{pkg_id}")
    data_date = st.date_input("Data date for this assessment", today)

    if chosen.empty:
        st.warning("Select at least one fact.")
        st.stop()
    fn = build_fragnet(chosen.assign(date=pd.to_datetime(chosen["date"])), steps.dropna(subset=["name"]),
                       impacted, data_date)
    release = fn[fn["type"] != "Tie-in"]["finish"].max()
    imp = impact(ACTS, RELS, impacted, release) if impacted else pd.DataFrame(
        columns=["task_code", "task_name", "finish", "new_finish", "delay_days", "is_milestone", "total_float_days", "wbs"])
    verdict, vdet = time_or_cost(ACTS, impacted, release, imp)

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Fragnet activities", int((fn["type"] != "Tie-in").sum()))
    m2.metric("Constraint removed (forecast)", f"{release:%d %b %Y}")
    m3.metric("Programme activities pushed", len(imp))
    m4.metric("Max delay (days)", int(imp["delay_days"].max()) if len(imp) else 0)
    box = st.error if verdict.startswith("Time") else st.success if verdict.startswith(("Cost", "No delay")) else st.info
    box(f"**Result: {verdict}.** " + (
        "The delay goes beyond the float of the linked activities (or moves Completion): claim time (EOT) and cost."
        if verdict.startswith("Time") else
        "The float of the linked activities absorbs the delay: Completion does not move, but the extra cost "
        "(disruption, standing time, resequencing) can still be claimed." if verdict.startswith("Cost") else
        "Link at least one programme activity in step 2 to assess time vs cost." if verdict.startswith("Not") else ""))
    if len(vdet):
        with st.expander("Float check per linked activity"):
            st.dataframe(vdet, hide_index=True, width="stretch")

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
    narr = build_narrative(str(sub["topic_title"].iloc[0]), PKG.get(pkg_id, ""), pkg_id, loc, wp,
                           chosen.assign(date=pd.to_datetime(chosen["date"])), fn, imp,
                           impacted, ACTS, data_date, "P6 programme rev 017 (data date 22 May 2026)", contract,
                           verdict=verdict)
    key = f"narr_{tid}"
    if ai_on and st.button("✨ Refine narrative with AI"):
        with st.spinner("Refining..."):
            st.session_state[key] = extractor.refine_narrative(narr)
    text = st.text_area("Narrative (edit before submitting)", st.session_state.get(key, narr), height=480)
    d1, d2, d3 = st.columns(3)
    d1.download_button("Fragnet for P6 (.xlsx)", to_p6_xlsx(fn), f"{tid}_fragnet.xlsx")
    d2.download_button("Narrative (.docx)", to_docx(text), f"{tid}_narrative.docx")
    d3.download_button("Narrative (.txt)", text, f"{tid}_narrative.txt")

elif page == "EW → PMI → CE chain":
    st.title("EW → PMI → CE chain")
    st.caption("For every early warning: was a PM instruction issued, and is there a compensation event? A gap (EW with "
               "no PMI and no CE) is a possible claim; an EW already covered by a CE or PMI is not suggested again.")
    if CHAIN.empty:
        st.warning("chain.csv not found - run scripts/build_data.py with --ewr and --pmis.")
        st.stop()
    ch = CHAIN.copy()
    ch["group"] = ch["coverage"].str.split(" \\(").str[0]
    if F_PKG:
        ch = ch[ch["package_id"].isin(F_PKG)]
    if F_TXT:
        ch = ch[ch["title"].str.contains(F_TXT, case=False) | ch["ew_id"].str.contains(F_TXT, case=False)]
    k = ch["group"].value_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Gap - no PMI/CE (open)", int(k.get("Gap - no PMI/CE", 0)))
    c2.metric("Covered by a CE (accepted or open)", int(k.get("Covered by CE", 0)))
    c3.metric("CE notified but NOT accepted", int(k.get("CE notified - NOT ACCEPTED", 0)))
    c4.metric("Closed / absorbed", int(k.get("Closed - no CE", 0) + k.get("Absorbed", 0)))
    groups = ["Gap - no PMI/CE", "CE notified - NOT ACCEPTED", "PMI issued - check CE", "Covered by CE", "Absorbed",
              "Closed - no CE"]
    pick = st.multiselect("Show", groups, default=["Gap - no PMI/CE", "CE notified - NOT ACCEPTED", "PMI issued - check CE"])
    v = ch[ch["group"].isin(pick)].copy()
    v["package"] = v["package_id"].map(PKG)
    v = v.merge(EWR[["ew_id", "close_reason", "last_note"]], on="ew_id", how="left") if len(EWR) else v
    ev = st.dataframe(v[["ew_id", "title", "package", "status", "notified", "pmis", "ces", "ce_status", "absorbed_by",
                         "coverage"]],
                      hide_index=True, width="stretch", height=380, on_select="rerun", selection_mode="single-row",
                      key="chain_pick")
    st.caption("Links: 'explicit' = the PMI or notes quote the EW; 'title match' = similar titles (check it). CE numbers "
               "come from the PMIs (CEMAR numbering) and the P6 CE record (shown as 'P6 CE-0xx').")
    if ev.selection.rows:
        r = v.iloc[ev.selection.rows[0]]
        st.subheader(f"{r.ew_id} · {r.title}")
        tl = [{"date": r.notified, "step": "Early warning", "ref": r.ew_id, "detail": r.title}]
        for p in filter(None, str(r.pmis).split(";")):
            x = PMI[PMI["pmi"] == p]
            if len(x):
                x = x.iloc[0]
                tl.append({"date": x.date.date() if pd.notna(x.date) else "", "step": "PM instruction", "ref": p,
                           "detail": f"{x.title} ({x.kind})"})
        for c in filter(None, str(r.ces).split(";")):
            x = CER[CER["ce"] == c] if len(CER) else pd.DataFrame()
            tl.append({"date": x.iloc[0]["notified"].date() if len(x) and pd.notna(x.iloc[0]["notified"]) else "",
                       "step": "Compensation event", "ref": c,
                       "detail": f"{x.iloc[0]['title']} - {x.iloc[0]['status']}" if len(x) else ""})
        if str(r.absorbed_by):
            tl.append({"date": "", "step": "Closed - absorbed by", "ref": r.absorbed_by, "detail": r.get("close_reason", "")})
        elif str(r.get("close_reason", "")):
            tl.append({"date": "", "step": "Closed", "ref": "", "detail": r.close_reason})
        st.dataframe(pd.DataFrame(tl), hide_index=True, width="stretch")
        st.caption("Last EW meeting note: " + str(r.get("last_note", "")))
        if r.group == "Gap - no PMI/CE":
            st.warning("No PMI and no CE found for this EW. If the event is a CE under cl. 60.1, check the 8-week "
                       "time bar (cl. 61.3) and consider notifying. Use Theme search to gather the facts.")
    st.download_button("Download chain (CSV)", v.to_csv(index=False).encode(), "ew_pmi_ce_chain.csv")

elif page == "PMI register":
    st.title("PMI register")
    st.caption("All Project Manager's Instructions read from the CEMAR PDFs: type, theme, references to EWs, CEs and "
               "GC letters, and PMIs that change a date (e.g. access or works by Others moved).")
    if PMI.empty:
        st.warning("pmi_register.csv not found - run scripts/build_data.py --pmis <folder>.")
        st.stop()
    p = PMI.copy()
    if F_PKG:
        p = p[p["package_id"].isin(F_PKG)]
    if F_TXT:
        p = p[p["title"].str.contains(F_TXT, case=False) | p["summary"].str.contains(F_TXT, case=False)
              | p["pmi"].str.contains(F_TXT, case=False)]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("PMIs", len(p))
    c2.metric("Changes to Scope", int(p["kind"].str.startswith("Change").sum()))
    c3.metric("With a date change", int(p["date_change"].sum()))
    c4.metric("Quotation requests", int(p["kind"].str.contains("uotation").sum()))
    kinds = st.multiselect("Type", sorted(p["kind"].unique()))
    only_dates = st.toggle("Only PMIs that change a date", value=False)
    if kinds:
        p = p[p["kind"].isin(kinds)]
    if only_dates:
        p = p[p["date_change"]]
    p["package"] = p["package_id"].map(PKG)
    p["date"] = p["date"].dt.date
    ev = st.dataframe(p[["pmi", "date", "title", "kind", "package", "ce_refs", "ew_refs", "gc_refs", "date_change",
                         "dates_mentioned"]], hide_index=True, width="stretch", height=420, on_select="rerun",
                      selection_mode="single-row", key="pmi_pick")
    if ev.selection.rows:
        r = p.iloc[ev.selection.rows[0]]
        st.subheader(f"{r.pmi} · {r.title}")
        st.caption(f"{r.date} · {r.kind} · clause {r.clause or '-'} · {r.package} · "
                   f"{fmt_micro(r.phase, r.zone, r.direction, r.location) or 'no location found'}")
        if r.no_effect_on_cost_or_completion in (True, "True"):
            st.info("The PM states this instruction has no effect on Defined Cost or Completion.")
        st.write(r.summary)
    st.download_button("Download PMI register (CSV)", p.to_csv(index=False).encode(), "pmi_register.csv")

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

    if len(CHAIN):
        cg = CHAIN["coverage"].str.split(" \\(").str[0].value_counts()
        st.subheader("Early warnings: coverage by PMI / CE")
        d1, d2, d3 = st.columns(3)
        d1.metric("EWs with no PMI and no CE (open)", int(cg.get("Gap - no PMI/CE", 0)))
        d2.metric("EWs covered by a CE", int(cg.get("Covered by CE", 0)))
        d3.metric("PMIs on record", len(PMI))
        if st.button("Open the EW → PMI → CE chain ➜"):
            st.session_state["goto"] = "EW → PMI → CE chain"
            st.rerun()

    st.subheader("Alerts")
    for r in ev.itertuples():
        lab, col = r.status_latest
        strict_red = r.status_strict[1] == "red"
        box = st.error if col == "red" else st.warning if (col == "orange" or strict_red) else st.success
        box(
            f"**{r.event_id} - {EVENT_TITLES.get(r.event_id, '')}**\n\n{cemar_status(r.event_id)}\n\n"
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
    if len(CHAIN):
        scan = scan.merge(CHAIN[["ew_id", "pmis", "ces", "coverage"]], on="ew_id", how="left").fillna(
            {"pmis": "", "ces": "", "coverage": ""})
    else:
        scan["pmis"], scan["ces"], scan["coverage"] = "", "", ""
    if len(EWR):
        scan = scan.merge(EWR[["ew_id", "absorbed_by", "close_reason"]], on="ew_id", how="left").fillna(
            {"absorbed_by": "", "close_reason": ""})
    hide_matched = st.toggle("Hide EWs already covered by an accepted/open CE or a PMI", value=True,
                             help="EWs whose CE was NOT accepted by the PM stay visible: they may be disputed.")
    if hide_matched:
        scan = scan[~scan["coverage"].isin(["Covered by CE", "PMI issued - check CE"]) & (scan["matched_ce"] == "")]
    c1, c2, c3 = st.columns(3)
    c1.metric("EWs on Client / Others with no CE found", len(scan))
    c2.metric("Still open", int((scan["status"] == "Open").sum()))
    c3.metric("Utility EWs (cl. 60.1(5) candidates)", int(scan["topic"].eq("Utilities").sum()))
    st.info("These early warnings involve the Client, the PM or Others (utilities, authorities) and the EW register "
            "shows no compensation event reference. Each is a candidate to check against the CE register: if no CE was "
            "notified and the event is a CE, the 8-week time bar (cl. 61.3) may already have passed - the value at risk.")
    only_open = st.toggle("Only open EWs", value=False,
                          help="Off = closed EWs are shown too, with what absorbed them, so the history is complete.")
    topic = st.multiselect("Topic", sorted(scan["topic"].dropna().unique()))
    v = scan[(scan["status"] == "Open") if only_open else slice(None)]
    if topic:
        v = v[v["topic"].isin(topic)]
    v = v.sort_values(["priority", "notified"], ascending=[True, False])
    v["notified"] = v["notified"].dt.date
    cols = ["priority", "ew_id", "topic", "title", "notified", "status", "absorbed_by", "close_reason", "matched_ce",
            "8 weeks from EW", "days since EW"]
    st.dataframe(v[[c for c in cols if c in v.columns]], hide_index=True, width="stretch")
    st.caption("Keyword screen of the EW register of 01 Oct 2026, matched by title against the CE record in P6 rev 017 "
               "(CE-001 to CE-037). CEs notified after May 2026 are not in that record - confirm against CEMAR.")

elif page == "CEMAR registers":
    st.title("CEMAR registers")
    st.caption("Official exports from CEMAR (21 Oct 2026): every compensation event, technical query and general "
               "communication. Author names are removed; only roles (Contractor / Project Manager) are kept.")
    if CER.empty and TQR.empty and GCR.empty:
        st.warning("No register exports found - run scripts/build_data.py --registers <folder>.")
        st.stop()
    t1, t2, t3 = st.tabs(["Compensation events", "Late PM replies (cl. 60.1(6))", "Letters (GC)"])
    with t1:
        ce = CER.copy()
        if F_PKG:
            ce = ce[ce["package_id"].isin(F_PKG)]
        if F_TXT:
            ce = ce[ce["title"].str.contains(F_TXT, case=False) | ce["ce"].str.contains(F_TXT, case=False)]
        k = ce["status"].value_counts()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("CEs notified", len(ce))
        c2.metric("Not accepted by the PM", int(k.get("NOT ACCEPTED", 0)))
        c3.metric("Implemented", int(k.get("IMPLEMENTED", 0)))
        c4.metric("Open (quotation / assessment / reply)", int(len(ce) - k.get("NOT ACCEPTED", 0) - k.get("IMPLEMENTED", 0)))
        by = ce.groupby(["clause", "status"]).size().reset_index(name="CEs")
        fig = px.bar(by, y="clause", x="CEs", color="status", orientation="h", height=380)
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis_title=None, legend_title=None)
        st.plotly_chart(fig, width="stretch")
        st.caption("CE-CNTFD-n = notified by the Contractor and not accepted by the PM as a compensation event. "
                   "These are the ones to review for dispute or re-notification when new facts arise.")
        stt = st.multiselect("Status", sorted(ce["status"].unique()), key="ce_status_f")
        v = ce[ce["status"].isin(stt)] if stt else ce
        v = v.assign(package=v["package_id"].map(PKG))
        for c in ["notified", "implemented", "quotation_due", "assessment_due"]:
            v[c] = v[c].dt.date
        st.dataframe(v[["ce", "notified", "from", "title", "clause", "status", "price", "decision", "quotation_due",
                        "package"]], hide_index=True, width="stretch", height=420)
    with t2:
        st.markdown("Under NEC4 the PM must reply within the period for reply. A late or missing reply can be a "
                    "compensation event (cl. 60.1(6)). These records show where that happened.")
        tq = TQR[TQR["days_late"].notna() | (TQR["status"] == "OVERDUE")].copy()
        tq["days_late"] = tq["days_late"].fillna((pd.Timestamp(today) - tq["reply_due"]).dt.days)
        gc = GCR[GCR["status"].isin(["OVERDUE", "AWAITING REPLY"]) & (GCR["from"] == "Contractor")].copy()
        gc["days_late"] = (pd.Timestamp(today) - gc["reply_due"]).dt.days
        c1, c2, c3 = st.columns(3)
        c1.metric("TQs answered late or overdue", len(tq))
        c2.metric("Contractor letters with PM reply overdue", int((gc["status"] == "OVERDUE").sum()))
        c3.metric("CEs already notified for late replies", int(CER["clause"].str.startswith("(6)").sum()))
        st.markdown("**Technical queries**")
        tqv = tq.assign(notified=tq["notified"].dt.date, reply_due=tq["reply_due"].dt.date, reply_date=tq["reply_date"].dt.date)
        st.dataframe(tqv[["tq", "notified", "title", "reply_due", "reply_date", "days_late", "status"]]
                     .sort_values("days_late", ascending=False), hide_index=True, width="stretch")
        st.markdown("**Contractor letters (GC) still waiting for the PM's reply**")
        gcv = gc.assign(date=gc["date"].dt.date, reply_due=gc["reply_due"].dt.date)
        st.dataframe(gcv[["gc", "date", "from", "title", "reply_due", "days_late", "status"]]
                     .sort_values("days_late", ascending=False), hide_index=True, width="stretch", height=360)
        st.caption("Check each one against the period for reply in the Contract Data and whether it affected the "
                   "work. Only the Contractor's communications that required a PM reply are relevant.")
    with t3:
        g = GCR.copy()
        if F_PKG:
            g = g[g["package_id"].isin(F_PKG)]
        if F_TXT:
            g = g[g["title"].str.contains(F_TXT, case=False) | g["description"].str.contains(F_TXT, case=False)
                  | g["gc"].str.contains(F_TXT, case=False)]
        c1, c2, c3 = st.columns(3)
        c1.metric("Letters", len(g))
        c2.metric("From the PM", int((g["from"] == "Project Manager").sum()))
        c3.metric("Conversation threads ('Further to')", int(g["further_to"].ne("").sum()))
        g = g.assign(date=g["date"].dt.date, package=g["package_id"].map(PKG))
        st.dataframe(g[["gc", "date", "from", "title", "kind", "further_to", "status", "package", "description"]],
                     hide_index=True, width="stretch", height=480,
                     column_config={"description": st.column_config.TextColumn(width="large")})
        st.caption("Use 'Ask the project' → Document to see what happened before and after any letter.")

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
