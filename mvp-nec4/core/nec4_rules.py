"""NEC4 ECC rule base (June 2017).

The rules below paraphrase the time limits and triggers of the NEC4
Engineering and Construction Contract. They are NOT the contract text
(which is copyright of NEC / Thomas Telford). Always check the actual
contract, including Z clauses, which may amend these periods.
"""

from datetime import date, timedelta

NOTIFY_CE_DAYS = 56          # cl. 61.3 - 8 weeks to notify a compensation event
PM_REPLY_CE_DAYS = 7         # cl. 61.4 - PM replies within 1 week
DEEMED_ACCEPT_DAYS = 14      # cl. 61.4 / 62.6 / 64.4 - 2 further weeks after a failure notice
QUOTE_DAYS = 21              # cl. 62.3 - quotation within 3 weeks
PM_REPLY_QUOTE_DAYS = 14     # cl. 62.3 - PM replies to quotation within 2 weeks

RULES = [
    {"clause": "15.1", "topic": "Early warning",
     "trigger": "Either party becomes aware of a matter that could increase the Prices, delay Completion or a Key Date, or impair performance.",
     "who": "Contractor / PM", "time_limit": "As soon as aware",
     "if_missed": "Cl. 63.7: the CE is assessed as if the warning had been given (value lost). Options C/D: possible Disallowed Cost."},
    {"clause": "14.3", "topic": "Change to the Scope",
     "trigger": "PM gives an instruction changing the Scope or a Key Date.",
     "who": "PM", "time_limit": "-",
     "if_missed": "Without the instruction the Contractor cannot lawfully do the extra work; refusal to instruct supports a CE under 60.1(18)."},
    {"clause": "60.1(1)", "topic": "CE - Scope change",
     "trigger": "PM instruction changing the Scope (incl. when the Client's Scope is not fit for purpose and must be changed).",
     "who": "PM notifies (61.1)", "time_limit": "At time of instruction", "if_missed": "-"},
    {"clause": "60.1(2)", "topic": "CE - Access late",
     "trigger": "Client does not allow access to the Site by the later of the access date and the Accepted Programme date.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "60.1(3)", "topic": "CE - Client fails to provide",
     "trigger": "Client does not provide something by the date shown on the Accepted Programme.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "60.1(5)", "topic": "CE - Client or Others late",
     "trigger": "Client or Others (e.g. utilities moving fibre optic cables) do not work within the times on the Accepted Programme.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "60.1(6)", "topic": "CE - PM/Supervisor silent",
     "trigger": "PM or Supervisor does not reply to a communication within the period for reply.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "60.1(12)", "topic": "CE - Physical conditions",
     "trigger": "Physical conditions within the Site that an experienced contractor would have judged to have a very small chance of occurring.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "60.1(18)", "topic": "CE - Client breach",
     "trigger": "Breach of contract by the Client not covered by another CE.",
     "who": "Contractor notifies", "time_limit": "8 weeks (61.3)", "if_missed": "Time-barred"},
    {"clause": "61.3", "topic": "Notify CE",
     "trigger": "Contractor believes an event is a CE and the PM has not notified it.",
     "who": "Contractor", "time_limit": "8 weeks after becoming aware",
     "if_missed": "Prices, Completion Date and Key Dates NOT changed (time bar)."},
    {"clause": "61.4", "topic": "PM reply to CE notice",
     "trigger": "Contractor notifies a CE.",
     "who": "PM", "time_limit": "1 week",
     "if_missed": "Contractor notifies the failure; after 2 more weeks the event is deemed a CE with instruction to quote."},
    {"clause": "62.3", "topic": "Quotation",
     "trigger": "PM instructs quotation.",
     "who": "Contractor, then PM", "time_limit": "Contractor 3 weeks; PM reply 2 weeks",
     "if_missed": "62.6: PM silent -> notice; after 2 more weeks quotation deemed accepted."},
    {"clause": "13.7", "topic": "Form of notification",
     "trigger": "Any notification the contract requires.",
     "who": "All", "time_limit": "-",
     "if_missed": "Notification must be separate from other communications, or it may be invalid."},
    {"clause": "31.2 / 32", "topic": "Programme",
     "trigger": "Programme must show work of Client and Others, and revised programmes show actual progress.",
     "who": "Contractor", "time_limit": "Interval in Contract Data",
     "if_missed": "Weak baseline for measuring delay (63.5 uses the Accepted Programme at the dividing date)."},
]


def ce_deadline(aware: date) -> date:
    """Last day to notify a compensation event under cl. 61.3."""
    return aware + timedelta(days=NOTIFY_CE_DAYS)


def status_for(deadline: date, today: date, notified: bool = False) -> tuple[str, str]:
    """Return (label, colour) for a notification deadline."""
    if notified:
        return "Notified", "green"
    left = (deadline - today).days
    if left < 0:
        return f"Time-barred risk ({-left} days late)", "red"
    if left <= 14:
        return f"URGENT - {left} days left", "red"
    if left <= 28:
        return f"Act now - {left} days left", "orange"
    return f"{left} days left", "green"
