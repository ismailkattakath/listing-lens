"""`llens places-resolve`: the `places` SKOS scheme (DATA/vocab/places.ttl) of place ABBREVIATIONS that
gta-hints@7 expands, resolved from several sources, never from one guess (operator decision):

- candidates: the site's operator-approved seed aliases (site config [places] seeds) + all-caps tokens that a gazetteer source
  knows AND that stand in a location line ("near", "location", "area", "incall", "@" …) of a DEVELOPMENT ad,
  minus words (spaCy's English stop words, the services vocabulary's labels).
  Ads of the frozen address-gold-v2 operators are never read (plan-v2: @7 development excludes them);
- sources (the 2026-11-08 snapshot): OSM name / short_name / alt_name, Wikidata en aliases, GeoNames
  alternate names (the site's country + admin1) flagged abbr / short / colloquial, and the initials of a multi-word OSM name. Initials
  are WEAK: they never count as a source of their own toward resolution;
- vote: RESOLVED when 2+ strong sources name the same place (same normalised name); CONFLICTING when strong
  sources name different places; else UNRESOLVED. Only resolved concepts (skos:note "resolved:…") are used by
  the extractor;
- unresolved/conflicting tokens -> DATA vocab/places.pending.jsonl for the human loop. With --gemini (operator-
  approved: GEMINI_MODEL + Google Maps grounding, key GEMINI_API_KEY from `secret exec`), each such
  token is asked as the BARE token + the site's region phrase (never ad text); the answer is stored as a
  suggestion (source=gemini) that needs a second source or the operator's review, never as a resolution.
"""

from __future__ import annotations

import collections
import io
import json
import re
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from listing_lens import site

SNAPSHOT = "gazetteer/2026-11-08"
ABBR = re.compile(r"^[A-Z][A-Z0-9]{1,4}$")
TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,4})(?![A-Za-z0-9])")
CUE = re.compile(
    r"\b(?:near|location|located|area|incall|close to|steps from|minutes? from|mins from|across from|beside)\b|@",
    re.I,
)
# The site's: the region box + 25 km (as the street-geometry snapshot), the region phrase a bare token is
# asked with, and the seed abbreviations (@6's operator-approved ALIASES) -- site config [places].
BOX = tuple(site.get("places", "box"))
STRONG = ("osm", "wikidata", "geonames")
SEEDS = dict(site.get("places", "seeds"))
# 2.5-flash-lite answers HTTP 404 (unavailable to new users, measured 2026-11-08).
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_CONTEXT = site.get("places", "context")
# GeoNames: the country file (<CC>.zip, alternatenames_<CC>.zip) and the region's admin1 code -- site config [geonames].
GEONAMES_COUNTRY = site.get("geonames", "country")
GEONAMES_ADMIN1 = site.get("geonames", "admin1")


def _norm(s: str) -> str:
    from .place_check import norm

    return norm(s)


def initials(name: str) -> str | None:
    w = [x for x in re.findall(r"[A-Za-z]+", name) if x.lower() not in {"of", "the", "and", "de"}]
    return "".join(x[0] for x in w).upper() if len(w) >= 2 else None


def source_index(snap: Path) -> dict[str, dict[str, set[str]]]:
    """token -> source -> {place names}."""
    src: dict = collections.defaultdict(lambda: collections.defaultdict(set))
    for f in ("osm-malls.json", "osm-stations.json", "osm-other.json", "osm-places.json"):
        p = snap / f
        if not p.exists():
            continue
        for e in json.loads(p.read_text())["elements"]:
            t = e.get("tags", {})
            nm = t.get("name")
            if not nm:
                continue
            for k in ("short_name", "alt_name", "name"):
                for v in t.get(k, "").split(";"):
                    if ABBR.match(v.strip()) and v.strip() != nm or (k != "name" and ABBR.match(v.strip())):
                        src[v.strip()]["osm"].add(nm)
            if (i := initials(nm)) and ABBR.match(i):
                src[i]["initials"].add(nm)
    w = snap / "wikidata-aliases.json"
    if w.exists():
        for _q, (label, aliases) in json.loads(w.read_text()).items():
            for a in aliases:
                if ABBR.match(a):
                    src[a]["wikidata"].add(label)
    on = {}
    cc, admin1 = GEONAMES_COUNTRY, GEONAMES_ADMIN1
    if (snap / f"{cc}.zip").exists():
        with zipfile.ZipFile(snap / f"{cc}.zip") as z, z.open(f"{cc}.txt") as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                f = line.split("\t")
                if f[10] == admin1 and BOX[0] <= float(f[4]) <= BOX[2] and BOX[1] <= float(f[5]) <= BOX[3]:
                    on[f[0]] = f[1]
    if (snap / f"alternatenames_{cc}.zip").exists():
        with zipfile.ZipFile(snap / f"alternatenames_{cc}.zip") as z, z.open(f"{cc}.txt") as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                f = line.rstrip("\n").split("\t")
                flagged = f[2] == "abbr" or (len(f) > 6 and "1" in (f[5], f[6]))
                if f[1] in on and ABBR.match(f[3]) and flagged:
                    src[f[3]]["geonames"].add(on[f[1]])
    return src


def vote(sources: dict[str, set[str]]) -> tuple[str, str | None, list[str]]:
    """(status, place, strong sources). Same place = same normalised name across sources."""
    by_place: dict[str, set[str]] = collections.defaultdict(set)
    for s in STRONG:
        for name in sources.get(s, ()):
            by_place[_norm(name)].add(s)
    if not by_place:
        return "unresolved", None, []
    best = max(by_place.items(), key=lambda kv: len(kv[1]))
    if len(best[1]) >= 2 and sum(1 for v in by_place.values() if len(v) >= 2) == 1:
        return "resolved", best[0], sorted(best[1])
    if len(by_place) > 1:
        return "conflicting", None, sorted({s for v in by_place.values() for s in v})
    return "unresolved", best[0], sorted(best[1])


def not_place_words(data: Path) -> set[str]:
    """Tokens that are words, not places: spaCy's English stop words (a maintained list, in the toolchain via
    services-match), every label of the operator's services vocabulary (e.g. service abbreviations) and every
    token the operator reviewed as not_a_place (DATA decisions/place-review.*.jsonl): never a candidate, so
    never pending and never sent to Gemini again."""
    from spacy.lang.en.stop_words import STOP_WORDS

    from .place_apply import load_reviews, not_place_tokens

    out = {w.lower() for w in STOP_WORDS} | {"ur", "u", "r", "pls", "lol", "hmu"}
    out |= {t.lower() for t in not_place_tokens(load_reviews(data))}  # the operator's not_a_place reviews
    v = data / "vocab" / "services.ttl"
    if v.exists():
        from . import services_match as sm

        out |= {label.lower() for _, label in sm._labels(sm.load_vocab(v))}
    return out


def candidates(
    rows, src: dict, excluded_ops: set[str], op: dict[str, str], words: set[str] = frozenset()
) -> dict[str, set[str]]:
    """Source-known tokens in a location line of a development ad -> the operators that wrote them. A token
    that is a word (`words`) is never a candidate."""
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        if op.get(r["ad_id"]) in excluded_ops:
            continue
        t = unicodedata.normalize("NFKC", f"{r['content']['title']}\n{r['content']['description']}")
        for line in t.split("\n"):
            if not CUE.search(line):
                continue
            for m in TOKEN.finditer(line):
                if m.group(1) in src and m.group(1).lower() not in words:
                    seen[m.group(1)].add(op.get(r["ad_id"], r["ad_id"]))
    return seen


def turtle(resolved: list[dict], version: str) -> str:
    """The places scheme as Turtle, written with rdflib (the services scheme's toolchain)."""
    import rdflib
    from rdflib.namespace import DCTERMS, OWL, RDF, SKOS

    ns = rdflib.Namespace("https://listing-insights.invalid/vocab/places#")
    g = rdflib.Graph()
    g.bind("skos", SKOS)
    g.bind("places", ns)
    scheme = ns["scheme"]
    g.add((scheme, RDF.type, SKOS.ConceptScheme))
    g.add((scheme, OWL.versionInfo, rdflib.Literal(version)))
    g.add((scheme, DCTERMS.title, rdflib.Literal("GTA place abbreviations (li places-resolve)")))
    for c in resolved:
        u = ns[re.sub(r"[^A-Za-z0-9]", "_", c["token"])]
        g.add((u, RDF.type, SKOS.Concept))
        g.add((u, SKOS.inScheme, scheme))
        g.add((u, SKOS.prefLabel, rdflib.Literal(c["query"])))
        g.add((u, SKOS.altLabel, rdflib.Literal(c["token"])))
        g.add((u, SKOS.note, rdflib.Literal(f"resolved:{'+'.join(c['sources'])}")))
    return g.serialize(format="turtle")


def gemini_suggest(token: str) -> dict:
    """ONE grounded question about a BARE token. Never ad text. Returns a suggestion, never a resolution."""
    import os
    import urllib.request

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit(
            "GEMINI_API_KEY not set. Run under: secret exec GEMINI_API_KEY=gemini:aistudio:apikey -- …"
        )
    prompt = (
        f'In {GEMINI_CONTEXT}, what single place does the abbreviation "{token}" most commonly refer to? '
        'Answer as JSON {"place": <full name or null>, "confidence": "high"|"low"}; null if it is not a place.'
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "tools": [{"googleMaps": {}}]}).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 - the key travels in a header; record the type (and HTTP code) only
        code = getattr(e, "code", None)
        return {
            "token": token,
            "source": "gemini",
            "model": GEMINI_MODEL,
            "status": f"ERROR:{type(e).__name__}{f':{code}' if code else ''}",
        }
    text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
    m = re.search(r"\{.*\}", text, re.S)
    try:
        ans = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        ans = {}
    return {
        "token": token,
        "source": "gemini",
        "model": GEMINI_MODEL,
        "context": GEMINI_CONTEXT,
        "status": "OK",
        "place": ans.get("place"),
        "confidence": ans.get("confidence"),
        "grounded": bool(out["candidates"][0].get("groundingMetadata")),
    }


def run(data: Path, rows: list[dict], *, use_gemini: bool = False, max_gemini: int = 50) -> dict:
    snap = data / SNAPSHOT
    src = source_index(snap)
    gold = json.loads((data / "eval/address-gold-v2/sample.json").read_text())
    excluded = set(gold["sample_operators"])
    gold_ads = set(gold["ad_ids"])
    rows = [r for r in rows if r["ad_id"] not in gold_ads]  # by id too: an ad absent from advertisers_v2
    op = {}
    for line in (data / "derived/advertisers_v2.jsonl").open():
        a = json.loads(line)
        op[a["ad_id"]] = a["operator_id"]
    seen = candidates(rows, src, excluded, op, not_place_words(data))
    resolved, pending = [], []
    for tok, query in SEEDS.items():
        resolved.append({"token": tok, "query": query, "sources": ["operator@6"]})
    for tok in sorted(seen):
        if tok in SEEDS:
            continue
        status, place, strong = vote(src[tok])
        row = {
            "token": tok,
            "status": status,
            "operators": len(seen[tok]),
            "sources": {s: sorted(v) for s, v in src[tok].items()},
        }
        if status == "resolved":
            resolved.append({"token": tok, "query": f"{place}, {GEMINI_CONTEXT}", "sources": strong})
        else:
            pending.append(row)
    asked = errors = 0
    if use_gemini:
        for row in pending:
            if asked >= max_gemini:
                break
            row["suggestion"] = gemini_suggest(row["token"])
            asked += 1
            errors += row["suggestion"]["status"] != "OK"
        log = data / "vocab" / "places.gemini-log.jsonl"  # the cumulative request tally (free tier ~500/day)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            fh.write(
                json.dumps({"at": at, "model": GEMINI_MODEL, "requests": asked, "errors": errors}) + "\n"
            )
    vocab = data / "vocab"
    vocab.mkdir(parents=True, exist_ok=True)
    version = datetime.now(UTC).strftime("0.1.0+%Y%m%dT%H%M%SZ")
    (vocab / "places.ttl").write_text(turtle(resolved, version))
    with (vocab / "places.pending.jsonl").open("w") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in pending)
    return {
        "resolved": len(resolved),
        "seeds": len(SEEDS),
        "pending": len(pending),
        "pending_by_status": dict(collections.Counter(r["status"] for r in pending)),
        "gemini_requests": asked,
        "excluded_gold_operators": len(excluded),
    }
