"""Location-phrase extractor for the site's ad text, version 6 (the module keeps its first site's name).

It does NOT geocode or normalise: it picks the one phrase most likely to geocode to a point and
returns it exactly as written, so the geocoder does the parsing. Strongest hint first, over ALL
lines before the next kind: postal code > street address > hotel-anchored address > labelled
location > intersection > landmark (incl. aliases) > FSA > street > area. A line break is a phrase
boundary: no pattern spans two lines. `street` (one road, no cross street) and `area` (a
neighbourhood, "airport area") are never a point: derive treats their geocode as imprecise
whatever the geocoder says. The text is NFKC-normalised first, so `raw` is the phrase as written
after that normalisation. An alias hint also carries `query`, the geocodable phrase it stands for.
Every word list (street words, cities, areas, landmarks, aliases, highways, ad vocabulary) is the SITE's,
read at import from the site vocabulary (site.py: site config [vocab] hints, in DATA); the code holds only
the patterns that combine them.

@5 over @4 (measured misses): per-line matching; the suffix ends at a word boundary ("STAR" is not
"ST"); a digit run after "-" (a phone fragment) is never a house number; "e"/"y" and a direction
letter connect an intersection, a direction word may end a side; hotel-anchored lowercase
addresses; the area kind.

@6 over @5 (the held-out misses): NFKC before matching (decorative Unicode letters); a small alias
table (ALIASES); "corner" connects an intersection; a one-edit misspelling of a known street, only
beside a real street; a unit letter on a house number; "<city> -<hwy>" and a single street are kind
`street`; an outcall line keeps its area when the ad offers incall elsewhere.

@8 over @7 (prereg notes/gta-hints-v8-gold-v3-prereg.md §1: numbered-highway intersections, 7 of @6/@7's
10 tier-A misses on the spent gold v2): `hwy_intersection8`, a street x numbered-highway pass tried on a
line after INTERSECTION finds nothing there. Case-insensitive, one lexicon street word as the leg, a bare
4xx only when no digit, ".", "-", "$" or "," touches it. @6 and @7 do not run it (Lexicon default None).

@8.1 over @8 (gold v3's one lost hit, 2026-11-13): the highway pass runs ONLY when @7 finds nothing in the
WHOLE ad (`pick81`), so every @7 hint is kept byte-identical; @8 could replace one per line.

@8.2 over @8.1: LABEL_NOISE82, one ad-template phrase never makes a label a road (a label whose only road word
was a street word of @7's lexicon inside that phrase); nothing else changes.

@8.3 over @8.2: LABEL_NOISE83, that street word ALONE never makes a label a road; it counts only before a
street type or one of the site's listed follow-on words.

The postal-code grammar (POSTAL, FSA) and the @8.2/@8.3 words are the site's too (vocabulary `postal`, `fsa`,
`label_noise`): the code holds the patterns, never the region's grammar or a frozen rule's words.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from listing_lens import site

_V = site.hints_vocab()  # the site's word lists (DATA, never code): see site.py

SUF = (
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|way|cres|crescent|crt|court|pkwy|parkway|"
    r"ln|lane|line|gate|sq|square|pl|place|trl|trail)\.?"
)
SUFB = SUF + r"(?![A-Za-z])"  # not \b: after "St." the next char is a space, and \b fails there
DIRW = r"(?i:east|west|north|south|[ewns])\b"
HWY_NAMES = "|".join(_V["highway_names"])  # the region's named highways
HWY_NUM = _V["highway_number"]  # the region's numbered-highway pattern (a regex fragment)
HWY = rf"(?:hwy|highway)\s?\d{{1,3}}|\b{HWY_NUM}\b|{HWY_NAMES}"
# A unit number never extends a street name, and "Near" never starts one (after NFKC a non-breaking
# space before the street is an ordinary space).
WORD = r"(?!(?i:unit|suite|apt|near)\b)[A-Z][A-Za-z'.]+"
NAMED = rf"{WORD}(?:\s{WORD}){{0,2}}"
# A digit run right after "-" is a phone fragment, not a house number. One unit letter A-G may follow
# ("<n>A"); H is left out because "24H" means hours.
HOUSE = r"(?<![-\d])\b\d{1,5}(?-i:[A-G])?"
STREET_ADDR = re.compile(rf"{HOUSE}\s+{NAMED}\s+{SUFB}(?:\s+(?:w|e|n|s|west|east|north|south)\b)?", re.I)
HOTEL_ADDR = re.compile(
    rf"\b(?:inn|hotel|suites|motel)\b.{{0,25}}?({HOUSE}\s+[a-z]+(?:\s[a-z]+){{0,2}}\s+{SUFB})", re.I
)
POSTAL = re.compile(_V["postal"])  # the region's full postal code (a regex, site vocabulary)
LOC_LABEL = re.compile(
    r"\b(?:location|located (?:in|at|near|on|by)|incall (?:at|in|near|location)|address|(?<!airport )area)"
    r"\s*[:\-–~]*\s*([^|~\n]{4,80})",
    re.I,
)
SIDE = rf"(?:{HWY}|{NAMED}(?:\s{SUFB})?(?:\s{DIRW})?)"
# "&", "/", "@", "and", Spanish "e"/"y", or a direction letter standing between two streets.
CONNECTOR = r"(?:\s*(?:&|/|@)\s*|\s+(?i:and|e|y|corner(?: of)?)\s+|\s+[NSW]\s+)"
INTERSECTION = re.compile(rf"\b({SIDE}){CONNECTOR}({SIDE})\b")
FSA = re.compile(_V["fsa"])  # its leading part alone (a regex, site vocabulary)
LANDMARK = re.compile(r"\b(" + "|".join(_V["landmarks"]) + r")\b", re.I)
# Abbreviations that name ONE place, each resolved to a phrase a geocoder can place (the site vocabulary's
# `aliases`). Upper-case ones are matched case-sensitively, so ordinary words never trigger them.
ALIASES = [(re.compile(a["pattern"], re.I if a["ignorecase"] else 0), a["query"]) for a in _V["aliases"]]
CITIES = "|".join(_V["cities"])
# "<city> -<n>", "<city> / <named highway>": one highway near a city. Not followed by "-" or a digit (a phone).
CITY_HWY = re.compile(
    rf"\b({CITIES})\s*[-–/&@]\s*({HWY_NUM}|{HWY_NAMES}|(?:hwy|highway)\s?\d{{1,3}})(?![-\d])", re.I
)
# A single street: a known street word + a direction ("Morvan e"), or a Title-case name + a street
# suffix ("Varne Street"). Suffixes that are also ordinary nouns (way, line, lane, place, court…) are
# left out, and so are street words that are ordinary words in ads (GENERIC).
STREET_SUF = r"(?i:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|cres|crescent|pkwy|parkway|trl|trail)\.?(?![A-Za-z])"
SINGLE_STREET = re.compile(
    rf"\b(?:([A-Z][a-z']+)\s+(?i:e|w|n|s|east|west|north|south)\b|{WORD}(?:\s{WORD})?\s+{STREET_SUF})"
)
GENERIC = set(_V["generic"])
AREAS = "|".join(_V["areas"])
AREA_WORD = re.compile(rf"\b(?:{AREAS})\b", re.I)
AREA_OF = re.compile(r"\b([A-Za-z]+) area\b(?:\s+([A-Z][A-Za-z]+))?", re.I)  # "<X> area [<Road>]"
NOISE = re.compile("|".join(_V["noise"]), re.I)
ROADWORDS = set(_V["roadwords"])
STOP = set(_V["stop"])


def roadish(side: str, roadwords: frozenset | set = ROADWORDS) -> bool:
    words = re.findall(r"[a-z]+|\d+", side.lower())
    return bool(
        re.search(rf"\b{SUF}(?:\s+{DIRW})?\s*$", side, re.I)
        or re.search(HWY, side, re.I)
        or any(w in roadwords for w in words)
    )


def _street_name_ok(a: str) -> list[str] | None:
    a = re.sub(r"^\s*\d+[A-Za-z]?\s+", "", a)  # the house number (and its unit letter) is not the name
    name = [w for w in re.findall(r"[A-Za-z']+", a)[:-1] if not re.fullmatch(SUF, w, re.I)]
    if NOISE.search(a) or not name or any(w.lower() in STOP for w in name):
        return None
    return name


def _areas(line: str, roadwords: frozenset | set = ROADWORDS) -> list[str]:
    """Area phrases on one line: a known neighbourhood, or "<X> area" where X is a street, an area
    or the airport (a bare "<city> area" is a city, not an area)."""
    out = [m.group(0) for m in AREA_WORD.finditer(line)]
    for m in AREA_OF.finditer(line):
        x = m.group(1).lower()
        if x in roadwords or x == "airport" or AREA_WORD.fullmatch(x):
            road = m.group(2)
            out.append(
                m.group(0) if road and road.lower() in roadwords else m.group(0).split(" area")[0] + " area"
            )
    return out


OUTCALL = re.compile(r"\bout[\s-]?calls?\b", re.I)
INCALL = re.compile(r"\bin[\s-]?calls?\b", re.I)


def _area(line: str, incall_offered: bool = False, roadwords: frozenset | set = ROADWORDS) -> str | None:
    """None for an outcall service area or a coverage list (two or more areas on one line). An
    outcall line keeps its area only when the ad offers incall elsewhere and never says "only"."""
    outcall_only = OUTCALL.search(line) and not INCALL.search(line)
    if outcall_only and (not incall_offered or re.search(r"\bonly\b", line, re.I)):
        return None
    found = {f.lower(): f for f in _areas(line, roadwords)}
    phrases = [
        f for k, f in found.items() if not any(k != o and k in o for o in found)
    ]  # "Midmere" in "Midmere area"
    return phrases[0] if len(phrases) == 1 else None


def _osa(a: str, b: str) -> int:
    """Optimal-string-alignment edit distance (Damerau-Levenshtein, one transposition per pair)."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def misspelt_street(side: str, roadwords: frozenset | set = ROADWORDS) -> bool:
    """One Title-case word of 5+ letters within ONE edit of a known street word of 5+ letters."""
    w = side.strip()
    if not re.fullmatch(r"[A-Z][a-z]{4,}", w):
        return False
    w = w.lower()
    return w not in STOP and any(
        len(r) >= 5 and abs(len(r) - len(w)) <= 1 and _osa(w, r) == 1 for r in roadwords
    )


def _single_street(line: str, roadwords: frozenset | set = ROADWORDS) -> str | None:
    for m in SINGLE_STREET.finditer(line):
        a = m.group(0)
        if re.search(r"[\d-]\S*\s+$", line[: m.start()]):
            continue  # right after a number token ("24H", "5ft", a phone): not a stated street
        word = m.group(1)
        if word is not None:  # "<street word> <direction>"
            if word.lower() in roadwords and word.lower() not in GENERIC:
                return a
            continue
        name = [w for w in re.findall(r"[A-Za-z']+", a)[:-1]]
        if not NOISE.search(a) and not any(w.lower() in STOP or w.lower() in GENERIC for w in name):
            return a
    return None


def landmark6_list(line: str) -> tuple[str, ...] | None:
    """@6's hand-listed landmarks (no aliases)."""
    if m := LANDMARK.search(line):
        return "landmark", m.group(0)
    return None


def landmark6(line: str) -> tuple[str, ...] | None:
    """@6's landmarks: the hand list, then ALIASES."""
    if hit := landmark6_list(line):
        return hit
    for rx, query in ALIASES:
        if m := rx.search(line):
            return "landmark", m.group(0), query
    return None


@dataclass(frozen=True)
class Lexicon:
    """What a version knows: its street words and its landmark finder (one line -> (kind, raw[, query])).
    `hwy_intersection` (@8 only; None for @6 and @7, so their output is unchanged) is tried on a line
    after INTERSECTION finds nothing there: (line, roadwords) -> the phrase, or None."""

    roadwords: frozenset
    landmark: Callable[[str], tuple[str, ...] | None]
    hwy_intersection: Callable[[str, frozenset], str | None] | None = None
    # @8.2 only: phrases removed from a label's value before the "does it name a road?" test (None = @6..@8.1).
    label_noise: re.Pattern | None = None


LEX6 = Lexicon(frozenset(ROADWORDS), landmark6)


def pick(text: str, lex: Lexicon = LEX6) -> tuple[str, ...] | None:
    """(kind, raw) or, for an alias or a city-highway phrase, (kind, raw, query)."""
    rw = lex.roadwords
    text = unicodedata.normalize("NFKC", text)
    lines = text.split("\n")
    for line in lines:
        if m := POSTAL.search(line):
            return "postal", m.group(0).upper()
    for line in lines:
        for m in STREET_ADDR.finditer(line):
            a = m.group(0)
            name = _street_name_ok(a)
            if name and (any(w.lower() in rw for w in name) or all(w[0].isupper() for w in name)):
                return "address", a
    for line in lines:
        for m in HOTEL_ADDR.finditer(line):
            if _street_name_ok(m.group(1)):
                return "address", m.group(1)
    for line in lines:
        for m in LOC_LABEL.finditer(line):
            v = m.group(1).strip(" .!:-")
            vr = lex.label_noise.sub(" ", v) if lex.label_noise else v
            if (
                len(v) >= 4
                and not NOISE.search(v)
                and (roadish(vr, rw) or FSA.search(vr) or LANDMARK.search(vr))
            ):
                return "label", v
    for line in lines:
        for m in INTERSECTION.finditer(line):
            a, b = m.group(1), m.group(2)
            if NOISE.search(m.group(0)):
                continue
            if (
                (roadish(a, rw) and roadish(b, rw))
                or (roadish(a, rw) and misspelt_street(b, rw))
                or (misspelt_street(a, rw) and roadish(b, rw))
            ):
                return "intersection", m.group(0)
        if lex.hwy_intersection and (x := lex.hwy_intersection(line, rw)):
            return "intersection", x
    for line in lines:
        if hit := lex.landmark(line):
            return hit
    for line in lines:
        for m in LOC_LABEL.finditer(line):
            if f := FSA.search(m.group(1)):
                return "fsa", f.group(0).upper()
    for line in lines:
        if m := CITY_HWY.search(line):
            hwy = m.group(2)
            if hwy.isdigit():
                road = f"Highway {hwy}"
            elif re.match(r"(?i)hwy|highway", hwy):
                road = "Highway " + re.sub(r"\D", "", hwy)
            else:
                road = hwy.upper()  # a named highway (site vocabulary highway_names)
            return "street", m.group(0), f"{road}, {m.group(1).title()}"
    for line in lines:
        if a := _single_street(line, rw):
            return "street", a
    incall = bool(INCALL.search(text))
    for line in lines:
        if a := _area(line, incall, rw):
            return "area", a
    return None


class GtaHints:
    name = "gta-hints@6"
    lex = LEX6

    def extract(self, text: str) -> dict | None:
        hit = pick(text, self.lex)
        if not hit:
            return None
        out = {"raw": hit[1], "kind": hit[0], "extractor": self.name}
        if len(hit) > 2:
            out["query"] = hit[2]  # the alias target / road phrase the geocoder is asked for
        return out


class GtaHints7(GtaHints):
    """@6's patterns with a lexicon built from DATA (extractors/gazetteer.py): OSM street words, OSM
    landmarks by the distinctive-name rule, stations only beside a station cue, and the `places` scheme's
    resolved abbreviations. `data` defaults to the store's DATA root."""

    name = "gta-hints@7"

    def __init__(self, data=None) -> None:
        from pathlib import Path

        from . import gazetteer

        if data is None:
            from scrape_harness import store

            data = store.data_dir()
        self.lex = gazetteer.lexicon(Path(data))


# ---- @8: numbered-highway intersections (prereg notes/gta-hints-v8-gold-v3-prereg.md §1) --------------
# @7 misses a street x numbered-highway intersection when the highway is upper-case ("HWY7", INTERSECTION's
# highway side is case-sensitive), the street is lower-case ("vexie/501", NAMED needs a capital), a word
# glued to the phrase defeats \b ("HWY7/WARDEN_..."), or a 3-word side swallows an ad word that NOISE then
# rejects ("... Incall Companion Brannock & 501"). This pass looks for exactly ONE known street (1-2 words,
# the last a lexicon street word, any case, an optional suffix/direction) beside ONE numbered highway.
# A bare 4xx is a highway only when no digit, ".", "-", "$" or "," touches it (a phone, a price).
HWY8 = rf"(?:hwy|highway)\s?\d{{1,3}}|(?<![\d.$,-]){HWY_NUM}(?![\d.,-]?\d)|{HWY_NAMES}"
STREET8 = rf"(?:[A-Za-z']+\s)?[A-Za-z']+(?:\s{SUFB})?(?:\s(?:east|west|north|south|[ewns])(?![A-Za-z]))?"
CONN8 = r"\s*(?:&|/|@|\+)\s*|\s+(?:and|at|e|y)\s+"
EDGE = r"(?<![A-Za-z0-9])"
END = r"(?![A-Za-z0-9])"
HWY_X = [
    re.compile(rf"{EDGE}(?P<st>{STREET8})(?:{CONN8})(?P<hw>{HWY8}){END}", re.I),
    re.compile(rf"{EDGE}(?P<hw>{HWY8})(?:{CONN8})(?P<st>{STREET8}){END}", re.I),
]


def _leg(toks: list[tuple[str, int, int]], roadwords, from_left: bool) -> tuple[int, int] | None:
    """The street leg inside a matched side: the street word nearest the highway (a lexicon street word,
    not "highway", not a stop word), plus its neighbour away from the highway only when that is a street word
    too, plus a suffix/direction right after it. (start, end) offsets in the line, or None."""
    names = [t for t in toks if not re.fullmatch(SUF, t[0], re.I) and not re.fullmatch(DIRW, t[0])]

    def ok(w):
        return w.lower() in roadwords and w.lower() not in ("highway", "hwy") and w.lower() not in STOP

    if not names:
        return None
    near = names[0] if from_left else names[-1]
    if not ok(near[0]):
        return None
    pick_ = [near]
    k = names.index(near)
    nb = k + 1 if from_left else k - 1
    if 0 <= nb < len(names) and ok(names[nb][0]):
        pick_.append(names[nb])
    start, end = min(t[1] for t in pick_), max(t[2] for t in pick_)
    after = [t for t in toks if t[1] >= end]
    if after and (re.fullmatch(SUF, after[0][0], re.I) or re.fullmatch(DIRW, after[0][0])):
        end = after[0][2]
    return start, end


def hwy_intersection8(line: str, roadwords: frozenset | set = ROADWORDS) -> str | None:
    """The leftmost street x numbered-highway phrase on one line, as written; None if there is none."""
    best: tuple[int, str] | None = None
    for street_first, rx in zip((True, False), HWY_X, strict=True):
        for m in rx.finditer(line):
            st0 = m.start("st")
            toks = [
                (t.group(0), st0 + t.start(), st0 + t.end())
                for t in re.finditer(r"[A-Za-z']+\.?", m.group("st"))
            ]
            leg = _leg(toks, roadwords, from_left=not street_first)
            if not leg:
                continue
            start, end = (leg[0], m.end()) if street_first else (m.start(), leg[1])
            if best is None or start < best[0]:
                best = (start, line[start:end].strip())
            break
    return best[1] if best else None


class GtaHints8(GtaHints7):
    """@7 plus `hwy_intersection8`: a street x numbered-highway intersection that @7's INTERSECTION misses
    (case, a glued word, a swallowed ad word). Everything else is @7, unchanged."""

    name = "gta-hints@8"

    def __init__(self, data=None) -> None:
        from dataclasses import replace

        super().__init__(data)
        self.lex = replace(self.lex, hwy_intersection=hwy_intersection8)


def pick81(text: str, lex: Lexicon) -> tuple[str, ...] | None:
    """@8.1: the base version's hint UNCHANGED whenever it has one; only when it finds nothing in the WHOLE
    ad, the first street x numbered-highway phrase (hwy_intersection8), line by line. So every base hint is
    kept byte-identical and a new hint appears only where the base had none (a clean superset)."""
    if hit := pick(text, lex):
        return hit
    for line in unicodedata.normalize("NFKC", text).split("\n"):
        if x := hwy_intersection8(line, lex.roadwords):
            return "intersection", x
    return None


class GtaHints81(GtaHints7):
    """@7, then the @8 highway pass ONLY when @7 returns nothing for the whole ad. @8 ran the pass per line
    and could replace a non-null @7 hint with another occurrence of the same intersection (a lost hit on
    gold v3, 2026-11-13); @8.1 never replaces a @7 hint."""

    name = "gta-hints@8.1"

    def extract(self, text: str) -> dict | None:
        hit = pick81(text, self.lex)
        if not hit:
            return None
        out = {"raw": hit[1], "kind": hit[0], "extractor": self.name}
        if len(hit) > 2:
            out["query"] = hit[2]
        return out


# The frozen @8.2/@8.3 rules' words are the site's (vocabulary `label_noise`, by version key); the rationale is
# kept with them in DATA.
_LN = _V["label_noise"]

# @8.2: one ad-template phrase is never a road. @7's lexicon holds a street word that also opens an ordinary
# phrase, so a label carrying that phrase counted as naming a road (a false-positive template on DATA). Only
# this phrase is removed, and only for the label's road test: the value itself, and "by appointment at
# <street>" lines, are untouched.
LABEL_NOISE82 = re.compile(r"\b" + r"\s+".join(map(re.escape, _LN["@8.2"]["phrase"])) + r"\b", re.I)


class GtaHints82(GtaHints81):
    """@8.1 plus LABEL_NOISE82: a label whose only road word sits inside the @8.2 phrase is not a hint."""

    name = "gta-hints@8.2"

    def __init__(self, data=None) -> None:
        from dataclasses import replace

        super().__init__(data)
        self.lex = replace(self.lex, label_noise=LABEL_NOISE82)


# @8.3: the street word ALONE never makes a label a road (8.2 caught only one phrase; other ordinary phrases
# with the same word still cost a paid geocode). It still counts when a street type or one of the listed
# follow-on words comes next. Only the label's road test changes; the label value itself is untouched.
_W83 = _LN["@8.3"]
_AFTER83 = "|".join(map(re.escape, _W83["unless_before"]))
LABEL_NOISE83 = re.compile(rf"\b{re.escape(_W83['word'])}\b(?!\s+(?:{SUF}|{_AFTER83})(?![A-Za-z]))", re.I)


class GtaHints83(GtaHints81):
    """@8.1 plus LABEL_NOISE83 (supersedes @8.2's LABEL_NOISE82): a label whose only road word is the bare
    @8.3 word is not a hint."""

    name = "gta-hints@8.3"

    def __init__(self, data=None) -> None:
        from dataclasses import replace

        super().__init__(data)
        self.lex = replace(self.lex, label_noise=LABEL_NOISE83)
