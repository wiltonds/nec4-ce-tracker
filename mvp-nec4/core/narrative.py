"""Monthly programme narrative (Clause 32 revised programme) for one tracker topic."""

from datetime import date

import pandas as pd

CLAUSE_HINTS = {
    "UTIL": "Clause 60.1(5) (Others not working within the times shown on the Accepted Programme)",
    "STRUCT": "Clauses 60.1(1) (change to the Scope), 60.1(12) (physical conditions) and 14.3",
    "EARTH": "Clause 60.1(12) (physical conditions)",
    "ENV": "Clauses 60.1(3) / 60.1(5) (Client or Others not providing or not working to the Accepted Programme)",
    "LAND": "Clause 60.1(2) (access to the Site not given by the access date)",
    "SCOPE": "Clauses 60.1(1) (change to the Scope) and 60.1(6) (PM reply not within the period for reply)",
    "TM": "Clauses 60.1(1) / 60.1(5)",
    "OTHER": "the relevant Clause 60.1 event (to be confirmed)",
}


def build_narrative(topic: str, package: str, package_id: str, location: str, work_package: str,
                    facts: pd.DataFrame, fragnet: pd.DataFrame, imp: pd.DataFrame, impacted: list[str],
                    acts: pd.DataFrame, data_date: date, programme_ref: str, contract: str) -> str:
    facts = facts.sort_values("date")
    first, last = facts.iloc[0], facts.iloc[-1]
    chron = "\n".join(f"- {pd.to_datetime(r.date):%d %b %Y} - {r.ref}: {' '.join(str(r.summary).split())[:300]}"
                      for r in facts.itertuples())
    fn = fragnet[fragnet["type"] != "Tie-in"]
    forecast = fn[fn["basis"] == "Forecast"]
    release = fn["finish"].max()
    fc = "\n".join(f"- {r.code} {r.name}: {r.duration_days:.0f} days ({r.start:%d %b %Y} to {r.finish:%d %b %Y})"
                   for r in forecast.itertuples() if r.type == "Activity")
    a = acts.set_index("task_code")
    tie = "\n".join(f"- {c} {a.loc[c, 'task_name']} (planned start {pd.to_datetime(a.loc[c, 'start']):%d %b %Y}, "
                    f"total float {a.loc[c, 'total_float_days']:.0f} days)" for c in impacted if c in a.index)
    top = imp.head(10)
    imp_txt = "\n".join(f"- {r.task_code} {r.task_name}: {r.finish:%d %b %Y} -> {r.new_finish:%d %b %Y} "
                        f"(+{r.delay_days} days)" for r in top.itertuples()) or "- No activity pushed."
    ms = imp[imp["is_milestone"] == True]  # noqa: E712
    ms_txt = "\n".join(f"- {r.task_code} {r.task_name}: +{r.delay_days} days (to {r.new_finish:%d %b %Y})"
                       for r in ms.head(8).itertuples()) or "- No key milestone is delayed by this fragnet alone."
    kmc = imp[imp["task_code"].astype(str).str.startswith("KMC")]
    completion = (f"Planned Completion is forecast to move by {int(kmc['delay_days'].max())} days."
                  if not kmc.empty else
                  "On the current programme logic this fragnet does not move Planned Completion on its own; "
                  "the delay is absorbed by float on the path to Completion but affects the key dates and "
                  "milestones listed above.")

    return f"""PROGRAMME NARRATIVE - DELAY EVENT
Contract: {contract}
Programme basis: {programme_ref}   |   Data date of this assessment: {data_date:%d %B %Y}
Topic: {topic}
Package: {package}   |   Work package: {work_package or '-'}   |   Location: {location or '-'}

1. Description of the event
Since {pd.to_datetime(first.date):%d %B %Y} ({first.ref}) the matter below has prevented or restricted the
planned sequence of work. The latest recorded fact is {last.ref} on {pd.to_datetime(last.date):%d %B %Y}.
At the data date the constraint has not been removed.

2. Chronology of key facts
{chron}

3. Fragnet
A fragnet of {len(fn)} activities ({(fn['basis'] == 'Actual (record)').sum()} based on the record and
{(fn['basis'] == 'Forecast').sum()} forecast) has been inserted into the programme. Forecast durations are
the Contractor's assumptions at the data date:
{fc or '- None'}
The fragnet finishes on {release:%d %B %Y} and is linked finish-to-start to:
{tie or '- (no programme activity selected)'}

4. Effect on the programme
Activities most affected (indicative, calendar days, before mitigation):
{imp_txt}

Key dates and milestones affected:
{ms_txt}

{completion}

5. Contractual position
The event is considered to fall under {CLAUSE_HINTS.get(package_id, CLAUSE_HINTS['OTHER'])}. Early warnings
and notifications made to date are listed in the chronology. Where a compensation event has not yet been
notified, notification under Clause 61.3 is required within eight weeks of becoming aware.

6. Mitigation
[Planner to describe resequencing, additional resources or alternative methods considered.]

7. Request
The Contractor requests that the effect of this event is taken into account in the assessment of the
related compensation event(s) and in the acceptance of this revised programme (Clause 32), including any
change to the Completion Date and Key Dates (Clause 63.5).

Note: impact figures are calculated outside P6 in calendar days, ignoring calendars and constraints.
Run the fragnet in P6 to confirm before submission.
"""
