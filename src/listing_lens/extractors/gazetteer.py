"""gta-hints@7's lexicon, built from DATA (never from code): the OSM gazetteer snapshot and the operator's
`places` SKOS scheme. The extractor's patterns are @6's; only what it KNOWS changes (operator-approved):

- street words: @6's ROADWORDS + the single-word base names of ARTERIAL OSM streets (the geometry
  snapshot's highway class), minus ad vocabulary (GENERIC, STOP, NOISE) and OSM place names;
- landmarks: OSM malls, aerodromes, universities/colleges whose name passes the distinctive-name rule,
  and OSM stations ONLY next to station / subway / a transit brand (site vocabulary; a station is often named after its street:
  "Bay", "Eglinton");
- abbreviations: the `places` scheme's skos:altLabel -> its skos:prefLabel (the geocodable phrase), for
  concepts whose resolution is accepted (skos:note "resolved:…"); suggestions are never used.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import gta_hints as g

SNAPSHOT = "gazetteer/2026-11-08"
PLACES_VOCAB = "vocab/places.ttl"
STATION_BRANDS = "|".join(g._V["station_brands"])  # the site's transit brands (regex fragments)
STATION_CUE = rf"(?:station|stn|subway|{STATION_BRANDS})"
MIN_WORD = 4  # a single-word street base shorter than this is too likely an ordinary token


def _norm(s: str) -> str:
    from listing_lens.place_check import norm

    return norm(s)


def _base(s: str) -> str:
    from listing_lens.place_check import base

    return base(_norm(s))


def place_names(snap: Path) -> set[str]:
    """Place names from two sources: OSM places in the bbox, and GeoNames' populated places (feature class P)
    of the site's admin1 (site config [geonames]), which also covers towns just outside the bbox."""
    out = {
        _norm(e["tags"]["name"])
        for e in json.loads((snap / "osm-places.json").read_text())["elements"]
        if e.get("tags", {}).get("name")
    }
    from listing_lens import site

    cc, admin1 = site.get("geonames", "country"), site.get("geonames", "admin1")
    z = snap / f"{cc}.zip"
    if z.exists():
        import io
        import zipfile

        with zipfile.ZipFile(z) as zf, zf.open(f"{cc}.txt") as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                f = line.split("\t")
                if len(f) > 10 and f[6] == "P" and f[10] == admin1:
                    out.add(_norm(f[1]))
    return out


ARTERIAL = {"motorway", "trunk", "primary", "secondary", "tertiary"}
GEOM_SNAPSHOT = "gazetteer/2026-11-08-streets-geom"


def road_words(snap: Path, places: set[str], geom: Path | None = None) -> frozenset:
    """@6's words + single-word bases of ARTERIAL OSM streets. Arterials only: ads name major intersections,
    and residential bases are full of given names and ordinary words (measured on dev ads: "Judy & Candy",
    "Magic Touch & Gentle" became intersections with every OSM street)."""
    if geom and (geom / "osm-streets-geom.slim.json.gz").exists():
        import gzip

        with gzip.open(geom / "osm-streets-geom.slim.json.gz", "rt") as fh:
            bases = {_base(w["name"]) for w in json.load(fh)["ways"] if w["highway"] in ARTERIAL}
    else:
        raise SystemExit(f"gta-hints@7 needs DATA/{GEOM_SNAPSHOT} (arterial classes): refusing")
    extra = {
        b
        for b in bases
        if " " not in b
        and b.isalpha()
        and len(b) >= MIN_WORD
        and b not in g.GENERIC
        and b not in g.STOP
        and b not in places
        and not g.NOISE.fullmatch(b)
    }
    return frozenset(g.ROADWORDS | extra)


def distinctive(name: str, places: set[str], road: frozenset, counts: dict[str, int]) -> bool:
    """A landmark name worth matching in free text. WORKING READING (team-lead to confirm): not a place,
    street or city name; not ad vocabulary; multi-word, or ONE word that names exactly one gazetteer feature."""
    n = _norm(name)
    toks = n.split()
    if not toks or n in places or n in road or re.fullmatch(g.CITIES, n, re.I):
        return False
    if any(t in g.STOP for t in toks) and len(toks) == 1:
        return False
    if g.NOISE.search(n) or (len(toks) == 1 and (toks[0] in g.GENERIC or len(n) < 5)):
        return False
    return len(toks) > 1 or counts.get(n, 0) == 1


def _elements(snap: Path, f: str) -> list[dict]:
    p = snap / f
    return json.loads(p.read_text())["elements"] if p.exists() else []


def landmark_names(snap: Path, places: set[str], road: frozenset) -> tuple[list[str], list[str]]:
    """(free-standing landmark names, station names needing a cue), as written in OSM."""
    feats = [e for f in ("osm-malls.json", "osm-other.json") for e in _elements(snap, f)]
    names = [
        n.strip()
        for e in feats
        for k in ("name", "short_name", "alt_name")
        for n in e.get("tags", {}).get(k, "").split(";")  # OSM's multi-value separator
        if n.strip()
    ]
    counts: dict[str, int] = {}
    for n in names:
        counts[_norm(n)] = counts.get(_norm(n), 0) + 1
    free = sorted({n for n in names if distinctive(n, places, road, counts)}, key=len, reverse=True)
    stations = sorted(
        {e["tags"]["name"] for e in _elements(snap, "osm-stations.json") if e.get("tags", {}).get("name")},
        key=len,
        reverse=True,
    )
    return free, stations


def _alt(names: list[str]) -> str:
    """A regex alternation of names, spaces and punctuation loose ("St. George" ~ "St George")."""
    parts = []
    for n in names:
        toks = re.findall(r"[A-Za-z0-9]+", n)
        if toks:
            parts.append(r"[\s.'’-]*".join(re.escape(t) for t in toks))
    return "|".join(parts) or r"(?!x)x"


def places_aliases(vocab: Path) -> list[tuple[re.Pattern, str]]:
    """(altLabel matcher, prefLabel) for each RESOLVED concept of the places scheme. Upper-case labels match
    case-sensitively (ordinary words never trigger them), like @6's ALIASES."""
    if not vocab.exists():
        return []
    import rdflib
    from rdflib.namespace import SKOS

    gr = rdflib.Graph().parse(str(vocab), format="turtle")
    out = []
    for c in set(gr.subjects(SKOS.prefLabel, None)):
        notes = [str(n) for n in gr.objects(c, SKOS.note)]
        if not any(n.startswith("resolved:") for n in notes):
            continue
        pref = str(next(gr.objects(c, SKOS.prefLabel)))
        for alt in gr.objects(c, SKOS.altLabel):
            a = str(alt)
            flags = 0 if a.isupper() else re.I
            out.append((re.compile(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", flags), pref))
    return sorted(out, key=lambda x: -len(x[0].pattern))


def lexicon(data: Path) -> g.Lexicon:
    snap = data / SNAPSHOT
    if not (snap / "osm-places.json").exists():
        raise SystemExit(f"gta-hints@7 needs the gazetteer snapshot DATA/{SNAPSHOT} (absent): refusing")
    places = place_names(snap)
    road = road_words(snap, places, data / GEOM_SNAPSHOT)
    free, stations = landmark_names(snap, places, road)
    free_rx = re.compile(rf"(?<![A-Za-z0-9])(?:{_alt(free)})(?![A-Za-z0-9])", re.I)
    st = _alt(stations)
    station_rx = re.compile(
        rf"(?<![A-Za-z0-9])(?:(?:{st})\s+{STATION_CUE}|(?:{STATION_BRANDS})\s+(?:{st}))(?![A-Za-z0-9])", re.I
    )
    # @6's compiled ALIASES first (their regexes hold each alias's spelling variants), so the
    # scheme only ADDS concepts and @7 never loses an alias @6 found (it is scored paired against @6).
    aliases = list(g.ALIASES) + places_aliases(data / PLACES_VOCAB)

    def landmark(line: str) -> tuple[str, ...] | None:
        if hit := g.landmark6_list(line):
            return hit
        if m := free_rx.search(line):
            return "landmark", m.group(0)
        if m := station_rx.search(line):
            return "landmark", m.group(0)
        for rx, query in aliases:
            if m := rx.search(line):
                return "landmark", m.group(0), query
        return None

    return g.Lexicon(road, landmark)
