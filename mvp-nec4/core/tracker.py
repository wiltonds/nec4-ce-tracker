"""Tracker helpers: packages, chainage parsing, privacy scrub, activity suggestions."""

import math
import re
from collections import Counter

import pandas as pd

# Default "big packages". Editable in data/packages.csv (the Project Manager's own list).
DEFAULT_PACKAGES = [
    {"package_id": "UTIL", "package": "Utilities & works by Others",
     "keywords": "openreach;bt ;btor;fibre;ssen;scottish water;sw main;utilit;qts;network rail;power supply;"
                 "uncharted services;statutory undertaker;cable;apparatus;diversion;undertrack"},
    {"package_id": "STRUCT", "package": "Structures & heritage",
     "keywords": "bridge;culvert;retaining wall; rw ;listed;lb5574;lb 5574;heritage;parapet;sheet pil;piling;"
                 "structural;abutment;soil nail;headwall"},
    {"package_id": "EARTH", "package": "Earthworks, drainage & ground",
     "keywords": "earthwork;drainage;ground condition;topsoil;excavat;geotech;rock;dewater;run-off;run off;"
                 "ditch;physical condition;topograph;filter drain;soil"},
    {"package_id": "ENV", "package": "Environment & ecology",
     "keywords": "ecolog;inns;balsam;bat ;bats;fish;otter;badger;water quality;wqm;tree fell;felling;nesting;"
                 "sepa;pws;private water;invasive;species;naturescot;noise;vibration;windthrow;car licen"},
    {"package_id": "LAND", "package": "Land, access & consents",
     "keywords": "landowner;access;cpo;land ;farm;consent;licen;pkc;council;permit;section 61;wayleave;"
                 "community;boundar;occupier"},
    {"package_id": "SCOPE", "package": "Scope, design & Client decisions",
     "keywords": "scope;design;tq;client's team;client team;vehicles;contact information;specification;"
                 "instruction;change to scope;departure;sds;check certificate;assumption;weather"},
    {"package_id": "TM", "package": "Traffic management, ITS & roads",
     "keywords": "traffic;ttm; tm ;switch; its ;camera;cctv;vms;streetlight;street light;signage;road closure;"
                 "lane;pavement;surfacing;rsa;radar;kerb"},
]

NAME_RE = re.compile(r"\b(Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+)?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(\+?\d[\d\s()-]{8,}\d)")
CH_KM_RE = re.compile(r"\bch\.?\s*:?\s*(\d{1,2})\s*\+\s*(\d{3})", re.I)
CH_RE = re.compile(r"\bch\.?\s*:?\s*(\d{3,5})(?:\s*(?:-|–|to)\s*(?:ch\.?\s*)?(\d{3,5}))?", re.I)
STOP = set("the a an of to and in for on at by with from is are be as this that it its or not no new "
           "further works work contractor client pm project manager - / & phase zone sb nb".split())


_EXTRA_NAMES: list[re.Pattern] = []


def set_private_names(names: list[str]):
    """Extra names to redact (kept in a local file outside the repo, see scripts/build_data.py --names)."""
    global _EXTRA_NAMES
    _EXTRA_NAMES = [re.compile(r"\b" + re.escape(n.strip()) + r"(?:'s|s)?\b", re.I) for n in names if n.strip()]


def scrub(text: str) -> str:
    """Remove personal data: e-mails, phone numbers, titled names and listed names."""
    text = EMAIL_RE.sub("[email]", str(text))
    text = PHONE_RE.sub("[phone]", text)
    text = NAME_RE.sub("[private individual]", text)
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


def classify_package(text: str, packages: pd.DataFrame) -> str:
    low = f" {str(text).lower()} "
    best, score = "OTHER", 0
    for p in packages.itertuples():
        s = sum(low.count(k) for k in str(p.keywords).split(";") if k)
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
