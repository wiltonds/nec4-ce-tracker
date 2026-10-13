"""Builds a draft Contractor's notification of a compensation event (cl. 61.3)."""

import io
from datetime import date

import pandas as pd


def build_notice(event_id: str, title: str, contract: str, clauses: list[str],
                 docs: pd.DataFrame, programme: pd.DataFrame, today: date) -> str:
    docs = docs.sort_values("doc_date")
    blocked = programme[programme["blocked_by_event"] == event_id]

    timeline = "\n".join(
        f"- {d.doc_date:%d %B %Y} - {d.ref} ({d.sender}): {d.summary}"
        for d in docs.itertuples()
    )
    acts = "\n".join(
        f"- {a.activity} (planned finish {a.planned_finish:%d %B %Y}) - status: {a.status}"
        for a in blocked.itertuples()
    )
    clause_txt = ", ".join(f"Clause {c}" for c in clauses)

    return f"""CONTRACTOR'S NOTIFICATION OF A COMPENSATION EVENT (CLAUSE 61.3)
[This notification is issued separately from other communications - Clause 13.7]

To: The Project Manager
Contract: {contract}
Date: {today:%d %B %Y}
Our ref: {event_id}
Subject: {title}
Clauses relied on: Clause 61.3, {clause_txt}

1. The event
In accordance with Clause 61.3, the Contractor notifies the Project Manager of the following
event, which the Contractor believes is a compensation event and which has not been notified
by the Project Manager. The relevant facts, in date order, are:

{timeline}

2. Effect on the Accepted Programme
The event prevents or delays the following operations shown on the Accepted Programme:

{acts}

These operations lie on or near the critical path and the event is expected to delay planned
Completion. The Contractor will show the effect in its quotation and revised programme (Clause 62.2).

3. Effect on Defined Cost
The event is increasing Defined Cost, including: standby and sub-optimal use of plant and labour,
additional specialist engineering and monitoring, changes to working methods, and prolonged
time-related site costs.

4. Action requested
Pursuant to Clause 61.4, the Contractor requests the Project Manager to notify within one week
that this event is a compensation event and to instruct the Contractor to submit quotations
(Clause 62). Where a change to the Scope is required, the Contractor requests an instruction
under Clause 14.3.

For and on behalf of the Contractor
[Name, position]
"""


def to_docx(text: str) -> bytes:
    import docx
    d = docx.Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
