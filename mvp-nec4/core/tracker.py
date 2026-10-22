"""Tracker helpers: packages, chainage parsing, privacy scrub, activity suggestions."""

import math
import re
from collections import Counter

import pandas as pd

# Main topics ("pacotes de assunto") defined by the project team. Editable in data/packages.csv.
# Keywords are case-insensitive substrings; a keyword starting with "re:" is a case-sensitive regex.
DEFAULT_PACKAGES = [
    {"package_id": "BT", "package": "BT / Openreach",
     "keywords": r"openreach;btor;bt openreach;fibre;copper cable;circet;joint box;re:\bBT\b"},
    {"package_id": "GUAY", "package": "Guay Farmhouse", "keywords": "guay farmhouse;guay cattle;guay"},
    {"package_id": "LEDP", "package": "Ledpettie Bridge", "keywords": "ledpettie;ledpetty;lb5574;lb 5574"},
    {"package_id": "TOPO", "package": "Topography",
     "keywords": "topograph;topo survey;topo data;existing levels;ground model;level discrepanc"},
    {"package_id": "BEAVER", "package": "Beaver", "keywords": "beaver"},
    {"package_id": "SW", "package": "Scottish Water",
     "keywords": r"scottish water;sw main;water main;re:\bSW\b"},
    {"package_id": "ITS", "package": "ITS",
     "keywords": r"cctv;camera;vms;radar;tse equip;weather station;loops;re:\bITS\b"},
    {"package_id": "SURVEY", "package": "Survey Control",
     "keywords": "survey control;control station;control point;setting out;setting-out;benchmark;gnss"},
    {"package_id": "SSEN", "package": "SSEN",
     "keywords": "ssen;overhead line;oh cable;ug cable;transformer;power supply;mains power;electricity"},
]

NAME_RE = re.compile(r"\b(Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-zA-Z\-]+(?:'s\b|(?:\s+[A-Z][a-zA-Z\-]+(?:'s\b)?)?)")
SIGN_RE = re.compile(r"[A-Z][a-z]+ [A-Z][a-zA-Z'\-]+?\s?on behalf of\s*(?:[A-Z][a-z]+ [A-Z][a-zA-Z'\-]+)?")
INITIAL_RE = re.compile(r"\b[A-Z]\.\s?[A-Z][a-z]{2,}(?:-[A-Z][a-z]+)?\b")
ACCESS_RE = re.compile(r"\b(Access|access|Login|login)\s*-\s*[A-Z][a-z]+ [A-Z][a-zA-Z'\-]+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<![\d/.-])(?:\+44\s?\(?0?\)?\s?|0)\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}(?![\d/.-])")
CH_KM_RE = re.compile(r"\bch\.?\s*:?\s*(\d{1,2})\s*\+\s*(\d{3})", re.I)
CH_RE = re.compile(r"\bch\.?\s*:?\s*(\d{3,5})(?:\s*(?:-|–|to)\s*(?:ch\.?\s*)?(\d{3,5}))?", re.I)
STOP = set("the a an of to and in for on at by with from is are be as this that it its or not no new "
           "further works work contractor client pm project manager - / & phase zone sb nb".split())


_EXTRA_NAMES: list[re.Pattern] = []


def set_private_names(names: list[str]):
    """Extra names to redact (kept in a local file outside the repo, see scripts/build_data.py --names)."""
    global _EXTRA_NAMES
    full = {n.strip() for n in names if n.strip()}
    surnames = {n.split()[-1] for n in full if len(n.split()) > 1 and len(n.split()[-1]) > 3}
    # full names: no word boundaries (exports often glue words: "2026.Gemma SmithProject Manager")
    pats = [re.compile(re.escape(n) + r"(?:'s)?", re.I) for n in sorted(full, key=len, reverse=True)]
    firsts = {n.split()[0] for n in full if len(n.split()) > 1 and len(n.split()[0]) > 3}
    firsts |= {"Rob" for n in full if n.startswith("Robert ")}
    pats += [re.compile(r"\b" + re.escape(n) + (r"(?:'s)?" if len(n) >= 6 else r"(?:'s)?(?![a-z])"))
             for n in sorted(surnames)]  # case-sensitive; long surnames also when glued ("Smithon behalf")
    pats += [re.compile(r"\b" + re.escape(n) + r"(?![a-z])") for n in sorted(firsts)]
    _EXTRA_NAMES = pats


def scrub(text: str) -> str:
    """Remove personal data: e-mails, phone numbers, titled names and listed names."""
    text = EMAIL_RE.sub("[email]", str(text))
    text = PHONE_RE.sub("[phone]", text)
    text = NAME_RE.sub("[private individual]", text)
    text = SIGN_RE.sub("[person] on behalf of [person]", text)
    text = INITIAL_RE.sub("[person]", text)
    text = ACCESS_RE.sub(r"\1 - [person]", text)
    for rx in _EXTRA_NAMES:
        text = rx.sub("[person]", text)
    return text


def parse_chainage(text: str):
    """Return (ch_from, ch_to) in metres, or (None, None)."""
    t = str(text)
    m = CH_KM_RE.search(t)
    if m:
        v = int(m.group(1)) * 1000 + int(m.group(2))
        return v, v
    m = CH_RE.search(t)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        return min(a, b), max(a, b)
    return None, None


def fmt_location(a, b) -> str:
    if a is None or (isinstance(a, float) and math.isnan(a)):
        return ""
    a, b = int(a), int(b)
    return f"CH {a}" if a == b else f"CH {a}-{b}"


PHASE_RE = re.compile(r"\b(?:phase|ph)\.?\s*(\d)\b", re.I)
ZONE_RE = re.compile(r"\bzones?\s*(\d{1,2}(?:\s*(?:,|and|&|-)\s*\d{1,2})*)", re.I)
DIR_RE = re.compile(r"\b(NB|SB|northbound|southbound|north\s*bound|south\s*bound)\b", re.I)


def parse_micro(text: str) -> dict:
    """Micro location (level 1 of the classification): phase, zone and direction, e.g. 'Ph2 Zone 8 SB'."""
    t = str(text)
    ph = PHASE_RE.search(t)
    zn = ZONE_RE.search(t)
    dr = DIR_RE.search(t)
    d = ""
    if dr:
        d = "NB" if dr.group(1).lower().startswith(("nb", "north")) else "SB"
    zone = re.sub(r"\s*(,|and|&)\s*", ",", zn.group(1)).replace(" ", "") if zn else ""
    return {"phase": ph.group(1) if ph else "", "zone": zone, "direction": d}


def fmt_micro(phase, zone, direction, location="") -> str:
    parts = []
    if str(phase) not in ("", "nan"):
        parts.append(f"Ph{str(phase).split('.')[0]}")
    if str(zone) not in ("", "nan"):
        parts.append(f"Zone {zone}")
    if str(direction) not in ("", "nan"):
        parts.append(str(direction))
    if str(location) not in ("", "nan"):
        parts.append(str(location))
    return " ".join(parts)


def _kw_score(raw: str, low: str, keywords: str) -> int:
    s = 0
    for k in str(keywords).split(";"):
        if not k:
            continue
        if k.startswith("re:"):
            s += len(re.findall(k[3:], raw))
        else:
            s += low.count(k.lower())
    return s


def classify_package(text: str, packages: pd.DataFrame) -> str:
    raw = f" {str(text)} "
    low = raw.lower()
    best, score = "OTHER", 0
    for p in packages.itertuples():
        s = _kw_score(raw, low, p.keywords)
        if s > score:
            best, score = p.package_id, s
    return best


def _tokens(text: str):
    return [w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in STOP and len(w) > 2]


class ActivityIndex:
    """Keyword + chainage matcher between tracker entries and programme activities."""

    def __init__(self, acts: pd.DataFrame):
        self.acts = acts[~acts["wbs"].astype(str).str.contains("Inactive|Early Warnings|Compensation Events",
                                                                 regex=True)].copy()
        self.acts["toks"] = (self.acts["task_name"] + " " + self.acts["wbs"].astype(str)).map(_tokens)
        df = Counter(t for toks in self.acts["toks"] for t in set(toks))
        n = len(self.acts)
        self.idf = {t: math.log(n / (1 + c)) for t, c in df.items()}
        ch = self.acts["task_name"].map(parse_chainage)
        self.acts["ch_from"] = [c[0] for c in ch]

    def suggest(self, text: str, ch_from=None, k: int = 8) -> pd.DataFrame:
        q = set(_tokens(text))
        scores = []
        for r in self.acts.itertuples():
            s = sum(self.idf.get(t, 0) for t in q.intersection(r.toks))
            if ch_from is not None and r.ch_from is not None and not pd.isna(ch_from):
                if abs(r.ch_from - ch_from) <= 150:
                    s += 4
            scores.append(s)
        out = self.acts.assign(score=scores)
        out = out[out.score > 0].sort_values("score", ascending=False).head(k)
        return out[["task_code", "task_name", "wbs", "start", "finish", "total_float_days", "status", "score"]]
