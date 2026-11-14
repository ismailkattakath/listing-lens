"""Advertiser clusters: ads that share an identifying key belong to one advertiser.

A generic union-find over (ad, key) edges, plus THIS site's keys. Promoted from the analyst's H4/H6
clustering (union-find over phone last-4, social handles, name, title, description prefix and the written
venue), kept key-for-key so its counts can be checked for parity. A key links ads; the cluster records
which kinds of key linked it, so a reader can see how strong a cluster is.

Venue-leak guard: a written venue ("near X St & Y Ave") is shared by different advertisers working from
one place. A cluster whose ads are linked ONLY through venue keys is flagged `venue_only` and is not an
advertiser for a panel.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from . import site

METHOD = "advertisers@1:ph4+handle+desc200+name+title+venue"
PHONE_VIEW = re.compile(r"view-(\d{4})")
PHONE = re.compile(r"\(?(\d{3})\)?[\s.\-*]*(\d{3})[\s.\-*]*(\d{4})")
HANDLE = re.compile(
    r"(?:onlyfans\.com/|linktr\.ee/|linktree\.com/|instagram\s*-?\s*|@)([A-Za-z0-9_.]{4,})", re.I
)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        root = self.parent.setdefault(x, x)
        while root != self.parent[root]:
            root = self.parent[root]
        while x != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)  # deterministic: the smaller id is the root


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def keys(listing: dict) -> set[str]:
    """This site's identifying keys of one (latest) listing.v1 record, each "<kind>:<value>"."""
    c = listing["content"]
    text = (c.get("title") or "") + "\n" + (c.get("description") or "")
    ks = {"ph4:" + m for m in PHONE_VIEW.findall(text)}
    ks |= {"ph4:" + m[2] for m in PHONE.findall(text)}
    ks |= {"h:" + m.lower().strip(".") for m in HANDLE.findall(text)}
    d = norm(c.get("description"))
    if len(d) >= 60:
        ks.add("d:" + hashlib.md5(d[:200].encode()).hexdigest())  # noqa: S324 - a fingerprint, not security
    name = norm(((listing.get("site") or {}).get(site.source().record_key) or {}).get("name"))
    if len(name) >= 3:
        ks.add("n:" + name)
    if len(norm(c.get("title"))) >= 12:
        ks.add("t:" + norm(c.get("title")))
    hint = (listing.get("location", {}).get("text_hint") or {}).get("raw")
    if hint:
        ks.add("v:" + norm(hint))  # venue: same written location; see the venue-leak guard
    return ks


def clusters(listings: list[dict], keyfn=None, method: str = METHOD, id_prefix: str = "adv-") -> list[dict]:
    """advertiser.v1 records, one per cluster, deterministic for the same input. `keyfn` (default:
    keys) is the ablation's seam: the same union-find over another key set."""
    keyfn = keyfn or keys
    uf, by_ad, site = UnionFind(), {}, {}
    for r in listings:
        ad = "ad:" + r["ad_id"]
        uf.find(ad)
        by_ad[r["ad_id"]] = keyfn(r)
        site[r["ad_id"]] = r["source"]["site"]
        for k in by_ad[r["ad_id"]]:
            uf.union(ad, k)
    members: dict[str, list[str]] = defaultdict(list)
    for aid in by_ad:
        members[uf.find("ad:" + aid)].append(aid)
    out = []
    for ads in members.values():
        ads = sorted(ads)
        counts: dict[str, int] = defaultdict(int)  # how many of the cluster's ads carry each key
        for aid in ads:
            for k in by_ad[aid]:
                counts[k] += 1
        linking = sorted({k.split(":", 1)[0] for k, n in counts.items() if n >= 2})
        out.append(
            {
                "schema": "advertiser.v1",
                "advertiser_id": id_prefix + hashlib.sha256("\n".join(ads).encode()).hexdigest()[:12],
                "site": site[ads[0]],
                "ad_ids": ads,
                "link_kinds": linking,
                "venue_only": len(ads) > 1 and linking == ["v"],
                "method": method,
            }
        )
    return sorted(out, key=lambda a: (-len(a["ad_ids"]), a["advertiser_id"]))


# The link kinds as the dashboard names them, from the key prefixes above.
LINK_KIND = {"ph4": "phone4", "h": "handle", "n": "name", "t": "title", "d": "desc", "v": "venue"}


def _link_key(prefix: str, value: str) -> str:
    """The shared value a link stands on. Title and description become a 12-hex fingerprint (the text
    itself is in the listing); the rest are the short normalised value (phone4 = the last 4 digits)."""
    if prefix in ("t", "d"):
        return hashlib.sha256(value.encode()).hexdigest()[:12]
    return value


def links(listings: list[dict], advertisers: list[dict], keyfn=None) -> list[dict]:
    """advertiser_link.v1 rows: inside each advertiser, ONE row per (ad pair, key kind) for every key two
    ads share directly (the union-find's edges, not its transitive closure). ad_a < ad_b; deterministic."""
    by_ad = {r["ad_id"]: (keyfn or keys)(r) for r in listings}
    out = []
    for a in advertisers:
        if len(a["ad_ids"]) < 2:
            continue
        holders: dict[str, list[str]] = defaultdict(list)
        for aid in a["ad_ids"]:
            for k in by_ad.get(aid, ()):
                holders[k].append(aid)
        seen: set[tuple] = set()
        for k, ads in sorted(holders.items()):
            if len(ads) < 2:
                continue
            prefix, value = k.split(":", 1)
            kind, key = LINK_KIND[prefix], _link_key(prefix, value)
            ads = sorted(set(ads))
            for i, x in enumerate(ads):
                for y in ads[i + 1 :]:
                    if (x, y, kind) in seen:  # two phones' last 4 digits can coincide: one row per kind
                        continue
                    seen.add((x, y, kind))
                    out.append(
                        _link(a["advertiser_id"], a["advertiser_id"], a["site"], x, y, kind, key, True)
                    )
    out += photo_links(listings, advertisers)
    return sorted(
        out, key=lambda r: (r["advertiser_id"], r["ad_a"], r["ad_b"], r["link_kind"], r["link_key"])
    )


def _link(adv_a, adv_b, site, x, y, kind, key, clustering):
    return {
        "schema": "advertiser_link.v1",
        "advertiser_id": adv_a,
        "advertiser_b": adv_b,
        "site": site,
        "ad_a": x,
        "ad_b": y,
        "link_kind": kind,
        "link_key": key,
        "used_for_clustering": clustering,
    }


def photo_links(listings: list[dict], advertisers: list[dict]) -> list[dict]:
    """`photo` edges: two ads carrying the SAME media URL, exactly as the site gave it, one row per
    (ad pair, URL). NOT a clustering key (used_for_clustering false): an agency or stock photo would
    merge unrelated advertisers, which is what the analyst's photo ablation measures. These edges may
    cross advertisers (advertiser_b)."""
    adv = {aid: a["advertiser_id"] for a in advertisers for aid in a["ad_ids"]}
    site = {r["ad_id"]: r["source"]["site"] for r in listings}
    holders: dict[str, set[str]] = defaultdict(set)
    for r in listings:
        for u in (r.get("content") or {}).get("media_urls") or []:
            holders[u].add(r["ad_id"])
    out = []
    for u, ads in sorted(holders.items()):
        if len(ads) < 2:
            continue
        key = hashlib.sha256(u.encode()).hexdigest()[:12]
        ads_sorted = sorted(ads)
        for i, x in enumerate(ads_sorted):
            for y in ads_sorted[i + 1 :]:
                out.append(_link(adv.get(x), adv.get(y), site[x], x, y, "photo", key, False))
    return out


# ── ablation: the same clustering with one key kind dropped, or photos added ─────────────────

ABLATION = {
    "baseline": None,
    "-desc": "d",
    "-name": "n",
    "-title": "t",
    "-phone4": "ph4",
    "-handle": "h",
    "-venue": "v",
    "+photo": "+p",
}


def _photo_keys(listing: dict) -> set[str]:
    return {
        "p:" + hashlib.sha256(u.encode()).hexdigest()[:12]
        for u in (listing.get("content") or {}).get("media_urls") or []
    }


def _variant_keys(variant: str, base=None):
    base = base or keys
    prefix = ABLATION[variant]
    if prefix is None:
        return base
    if prefix == "+p":
        return lambda r: base(r) | _photo_keys(r)
    return lambda r: {k for k in base(r) if k.split(":", 1)[0] != prefix}


def ablation(listings: list[dict], base=None) -> list[dict]:
    """advertiser_ablation.v1 rows: for every variant (baseline, one key kind dropped, photos added),
    the advertiser each ad lands in. An advertiser id is a hash of its ad set, so the same id as in
    baseline means the same cluster. The real clusters (advertisers.jsonl) are never changed by this."""
    out = []
    for variant in ABLATION:
        for a in clusters(listings, _variant_keys(variant, base)):
            for aid in a["ad_ids"]:
                out.append(
                    {
                        "schema": "advertiser_ablation.v1",
                        "variant": variant,
                        "site": a["site"],
                        "ad_id": aid,
                        "advertiser_id": a["advertiser_id"],
                        "n_ads": len(a["ad_ids"]),
                        "link_kinds": [LINK_KIND.get(k, "photo" if k == "p" else k) for k in a["link_kinds"]],
                    }
                )
    return sorted(out, key=lambda r: (list(ABLATION).index(r["variant"]), r["ad_id"]))


# ── advertiser.v2: a NEW method, side by side with v1 (analyst spec h7, 2026-11-08) ──────────
# v1 unions every ad sharing ANY single key, so one common name chains whole groups (the ablation:
# dropping name moves 247 of 602 ads). v2 keeps v1's keys (one key function) plus the photo upload id,
# but links two ads only by a PAIR rule, then takes connected components. v1 is not changed.

METHOD_V2 = "advertisers@2:pairwise strong=d,h,img weak=n,t,ph4,v hub>60 no-email-handle persona=h|n+1"
STRONG = {"d", "h", "img"}
HUB_MAX = 60  # a key carried by more ads than this is generic text: ignored


# A handle captured through a bare "@" that is an email DOMAIN ("…view@gmail.com" -> "gmail.com") is a
# false strong key: advertisers@1's known defect (ADR-0015), fixed for v2 only. Prefix captures
# (onlyfans.com/, linktr.ee/, instagram) are untouched, and no lookbehind: "telegram...@Name" is real.
EMAIL_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.(com|ca|net|org|me|co|io)$")


def handles_v2(listing: dict) -> set[str]:
    c = listing["content"]
    text = (c.get("title") or "") + "\n" + (c.get("description") or "")
    out = set()
    for m in HANDLE.finditer(text):
        value = m.group(1).lower().strip(".")
        if m.group(0).startswith("@") and EMAIL_DOMAIN.match(value):
            continue
        out.add("h:" + value)
    return out


def keys_v2(listing: dict) -> set[str]:
    """v1's keys with the email-domain handles removed, plus img:<upload id> per media URL (every size
    of one upload shares it)."""
    out = {k for k in keys(listing) if _kind(k) != "h"} | handles_v2(listing)
    upload = site.source().upload_re
    for u in (listing.get("content") or {}).get("media_urls") or []:
        m = upload.match(u)
        out.add("img:" + (m.group(1) if m else u))
    return out


def _kind(k: str) -> str:
    return k.split(":", 1)[0]


def _edge(kinds: set[str], cross: bool) -> bool:
    """Same subcategory: >= 1 strong kind or >= 2 distinct kinds. Across subcategories: >= 2 kinds."""
    return len(kinds) >= 2 if cross else bool(kinds & STRONG) or len(kinds) >= 2


def _level(ads: list[str], ks: dict[str, set[str]]) -> tuple[str, int, float]:
    """(level, n_names, top_shared_key_share). operator iff size >= 5 AND names >= 3 AND (one d:/img: key
    on >= half the ads OR names >= 5): a judgement threshold (analyst), so its inputs are emitted too."""
    names = {k for a in ads for k in ks[a] if _kind(k) == "n"}
    carriers: dict[str, int] = defaultdict(int)
    for a in ads:
        for k in ks[a]:
            if _kind(k) in ("d", "img"):
                carriers[k] += 1
    share = round(max(carriers.values()) / len(ads), 4) if carriers else 0.0
    operator = len(ads) >= 5 and len(names) >= 3 and (share >= 0.5 or len(names) >= 5)
    return ("operator" if operator else "persona"), len(names), share


def clusters_v2(listings: list[dict]) -> list[dict]:
    """advertiser.v2 records, ONE PER AD: its v2 advertiser ("v2:" + a hash of the component's ad set;
    not stable across crawls, join on ad_id), level, and the kinds of the v2 edges that pulled it in."""
    ks, site, edges = _v2_edges(listings)
    op_uf, pe_uf = UnionFind(), UnionFind()
    evidence, p_evidence = defaultdict(set), defaultdict(set)
    for aid in ks:
        op_uf.find(aid)
        pe_uf.find(aid)
    for (x, y), (kinds, _keys, _cross) in edges.items():
        op_uf.union(x, y)
        evidence[x] |= kinds
        evidence[y] |= kinds
        if _persona_edge(kinds):  # a v2 edge too, so a persona can never span two operators
            pe_uf.union(x, y)
            p_evidence[x] |= kinds
            p_evidence[y] |= kinds
    operators = _components(op_uf, ks)
    persona_of = {aid: "p2:" + _hash(ads) for ads in _components(pe_uf, ks) for aid in ads}
    out = []
    for ads in operators:
        level, n_names, share = _level(ads, ks)
        op = "v2:" + _hash(ads)
        for aid in ads:
            out.append(
                {
                    "schema": "advertiser.v2",
                    "method": METHOD_V2,
                    "site": site[aid],
                    "ad_id": aid,
                    "operator_id": op,
                    "persona_id": persona_of[aid],
                    "n_ads": len(ads),
                    "level": level,
                    "n_names": n_names,
                    "top_shared_key_share": share,
                    "edge_evidence": sorted(evidence[aid]),
                    "persona_edge_evidence": sorted(p_evidence[aid]),
                }
            )
    return sorted(out, key=lambda r: (r["operator_id"], r["ad_id"]))


def _v2_edges(listings: list[dict]):
    """The v2 pair pass, ONCE for the clustering and its edge file: (keys per ad, site per ad, the accepted
    edges {(ad_a, ad_b): (kinds, the shared keys, cross-subcategory)}), ad_a < ad_b, hub keys ignored."""
    ks = {r["ad_id"]: keys_v2(r) for r in listings}
    sub = {r["ad_id"]: (r.get("source") or {}).get("subcategory") for r in listings}
    site = {r["ad_id"]: r["source"]["site"] for r in listings}
    holders: dict[str, list[str]] = defaultdict(list)
    for aid in sorted(ks):
        for k in ks[aid]:
            holders[k].append(aid)
    shared: dict[tuple[str, str], set[str]] = defaultdict(set)
    for k, ads in holders.items():
        if len(ads) < 2 or len(ads) > HUB_MAX:
            continue
        for i, x in enumerate(ads):
            for y in ads[i + 1 :]:
                shared[(x, y)].add(k)
    edges = {}
    for (x, y), keyset in shared.items():
        kinds, cross = {_kind(k) for k in keyset}, sub[x] != sub[y]
        if _edge(kinds, cross):
            edges[(x, y)] = (kinds, keyset, cross)
    return ks, site, edges


# v2's kind names on the dashboard: v1's, plus the photo UPLOAD id (img), which is not v1's `photo` (an exact
# media URL that never clusters); img is a v2 strong kind.
LINK_KIND_V2 = {**LINK_KIND, "img": "image"}


def links_v2(listings: list[dict], rows: list[dict]) -> list[dict]:
    """advertiser_v2_link.v1 rows: WHY v2 merged two ads. ONE row per (accepted v2 edge, key kind), from the
    same pair pass as clusters_v2 (so the edges are exactly the ones that built its components). link_key
    follows advertiser_link.v1 (phone4 last 4 digits; handle, name, venue normalised; title, desc
    fingerprinted) and img's upload id is fingerprinted too (it is part of a URL). Two keys of one kind on
    one pair: the smallest key is the row's link_key and n_keys counts them."""
    _ks, site, edges = _v2_edges(listings)
    by_ad = {r["ad_id"]: r for r in rows}
    out = []
    for (x, y), (kinds, keyset, cross) in sorted(edges.items()):
        persona = _persona_edge(kinds)
        for prefix in sorted(kinds):
            vals = sorted(k.split(":", 1)[1] for k in keyset if _kind(k) == prefix)
            key = (
                hashlib.sha256(vals[0].encode()).hexdigest()[:12]
                if prefix == "img"
                else _link_key(prefix, vals[0])
            )
            out.append(
                {
                    "schema": "advertiser_v2_link.v1",
                    "method": METHOD_V2,
                    "operator_id": by_ad[x]["operator_id"],
                    "site": site[x],
                    "ad_a": x,
                    "ad_b": y,
                    "link_kind": LINK_KIND_V2[prefix],
                    "link_key": key,
                    "n_keys": len(vals),
                    "edge_kinds": sorted(LINK_KIND_V2[k] for k in kinds),
                    "cross_subcategory": cross,
                    "persona_edge": persona,
                }
            )
    return out


def _persona_edge(kinds: set[str]) -> bool:
    """Persona: the pair shares a (non-email) handle, OR a name AND at least one other key kind."""
    return "h" in kinds or ("n" in kinds and len(kinds - {"n"}) >= 1)


def _components(uf: UnionFind, ks: dict) -> list[list[str]]:
    members: dict[str, list[str]] = defaultdict(list)
    for aid in ks:
        members[uf.find(aid)].append(aid)
    return [sorted(v) for v in members.values()]


def _hash(ads: list[str]) -> str:
    return hashlib.sha256("\n".join(ads).encode()).hexdigest()[:12]


# ── v1.1 (operator decision 2026-11-08): v1 EXCEPT email domains are no longer handles. The DEFAULT. ──
# advertisers@1 stays frozen and reproducible (`--method v1`) as the historical record.

METHOD_V1_1 = "advertisers@1.1:ph4+handle(no-email-domain)+desc200+name+title+venue"


def keys_v1_1(listing: dict) -> set[str]:
    """v1's keys with the email-domain handles removed (the same fix as v2's, one function)."""
    return {k for k in keys(listing) if _kind(k) != "h"} | handles_v2(listing)


def clusters_v1_1(listings: list[dict]) -> list[dict]:
    return clusters(listings, keys_v1_1, METHOD_V1_1, "v1.1:")
