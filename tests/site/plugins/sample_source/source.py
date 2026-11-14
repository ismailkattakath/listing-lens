"""SampleSite: the tests' SYNTHETIC source, modelled on scrape-harness's examples/example_site. It stands in
for the real site's source, which lives in DATA (the site file's [pipeline.plugins]); the core reads every
site specific through it (listing_lens.site.source()), so the repo's tests exercise that seam.

The markup is invented for the tests (tests/fixtures/*.html):
  list page  <main id="results"> <article class="result" [data-sponsored] [data-verified] [data-available]>
             with <a class="result__link"> <h2 class="result__title">, and <span class="result__city|name|age|
             ethnicity|rate">; a <nav class="pager"> of ?page=N links.
  ad page    <h1>, <div id="description">, <span id="city|name|age|ethnicity">, <p id="tagline">,
             <time class="posted" datetime="<wall time>">N units ago</time>, <dl class="facts"> rows,
             <ul class="contact"> items (phone/email/website/whatsapp, `hidden` = absent), <ul id="services">
             of <li data-code>, <ul class="photos"> of <img src>; schema.org via the harness's schemaorg.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, CData, NavigableString, Tag
from scrape_harness import schemaorg
from scrape_harness.interfaces import ListPage, Region

SITE = "example.invalid"
ORIGIN = f"https://www.{SITE}"
IMG = f"https://img.{SITE}/"
SITE_TZ = ZoneInfo("Etc/GMT+5")  # a fixed UTC-5: the site prints zone-less wall time in its own zone
AD_ID = re.compile(r"-(\d+)/?$")
# One invented region; the box is the synthetic site file's own places box, so the fixture points fall in it.
REGIONS = {"sample-region": Region(query_suffix="Sample Province", bounds="43.40,-80.10|44.10,-78.80")}


def squash(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text()).strip() if node else ""


BLOCK = {"br", "p", "div", "li", "ul", "ol"}


def body_text(node: Tag | None) -> str:
    """Line breaks (<br>, block elements) stay newlines; spaces collapse within a line; blank lines drop."""
    if not node:
        return ""
    parts: list[str] = []
    for el in node.descendants:
        if isinstance(el, Tag):
            if el.name in BLOCK:
                parts.append("\n")
        elif type(el) in (NavigableString, CData):
            parts.append(str(el))
    lines = (re.sub(r"[^\S\n]+", " ", ln).strip() for ln in "".join(parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)


def ad_id(url: str) -> str | None:
    m = AD_ID.search(urlsplit(url).path)
    return m.group(1) if m else None


def _int(s: str, pattern: str) -> int | None:
    return int(s) if re.fullmatch(pattern, s) else None


def card_hash(title: str | None, card: dict) -> str:
    canon = json.dumps({"title": title, **card}, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()[:16]


def page_count(doc: BeautifulSoup) -> int | None:
    pages = [
        int(m.group(1))
        for a in doc.select('nav.pager a[href*="page="]')
        if (m := re.search(r"[?&]page=(\d+)", a.get("href", "")))
    ]
    return max(pages) if pages else None


# (shortest, longest, slack) of one relative unit as the site floors it: "N units ago" = N <= elapsed < N+1.
UNIT = {
    "minute": (timedelta(minutes=1), timedelta(minutes=1), timedelta(minutes=5)),
    "hour": (timedelta(hours=1), timedelta(hours=1), timedelta(minutes=5)),
    "day": (timedelta(days=1), timedelta(days=1), timedelta(days=1)),
}


def parse_bump(doc: BeautifulSoup, now: datetime) -> str | None:
    """The posted wall time, kept only if the relative text agrees with it (a wrong zone is caught)."""
    el = doc.select_one("time.posted[datetime]")
    rel = re.search(r"(\d+|an?|one)\s+(minute|hour|day)s?\s+ago", squash(el), re.I) if el else None
    if not (el and rel):
        return None
    at = datetime.fromisoformat(el["datetime"]).replace(tzinfo=SITE_TZ)
    n = int(rel.group(1)) if rel.group(1).isdigit() else 1
    shortest, longest, slack = UNIT[rel.group(2).lower()]
    ok = n * shortest - slack <= now - at <= (n + 1) * longest + slack
    return at.isoformat() if ok else None


def facts(doc: BeautifulSoup) -> dict[str, str]:
    out = {}
    for dt in doc.select("dl.facts dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None and squash(dt) and squash(dd):
            out[squash(dt).lower()] = squash(dd)
    return out


def _contact(doc: BeautifulSoup, item: str) -> Tag | None:
    el = doc.select_one(f"ul.contact li.{item}")
    return None if el is None or el.has_attr("hidden") else el


def services(doc: BeautifulSoup) -> list[dict] | None:
    box = doc.select_one("ul#services")
    if box is None or box.has_attr("hidden"):
        return None
    out = [
        {"code": li.get("data-code") or None, "label": squash(li) or None}
        for li in box.select("li")
        if not li.has_attr("hidden") and (squash(li) or li.get("data-code"))
    ]
    return out or None


class SampleSite:
    site = SITE
    host = SITE
    record_key = "sample"
    tz = SITE_TZ
    regions = REGIONS
    upload_re = re.compile(r"^https?://img\.example\.invalid/([^/?#]+)")
    # Bump when parse_ad extracts anything new or differently; `llens reparse` tags versions with it.
    parser = "sample-ad@1"
    home_url = f"{ORIGIN}/"
    robots_url = f"{ORIGIN}/robots.txt"
    expect = {"list": "#results", "ad": "#description"}
    ignore_selectors = ("#login-dialog",)
    gone_selectors = ("main.ad-gone",)
    default_variant = {"list": "desktop", "ad": "desktop"}
    coverage_fields = (
        "card.title",
        "card.city_label",
        "bumped_at",
        "card.sample.name",
        "card.sample.age",
        "card.sample.ethnicity",
        "card.sample.verified",
        "card.sample.rate",
        "card.sample.available",
    )

    def admin_area(self, name: str) -> str | None:
        return {"sample-region": "SP"}.get(name)

    def region(self, name: str) -> Region:
        if name not in REGIONS:
            raise SystemExit(f"{SITE}: no region config for {name!r}; known: {', '.join(REGIONS)}")
        return REGIONS[name]

    def list_url(self, subcategory: str, region: str, page: int) -> str:
        return f"{ORIGIN}/listings/{subcategory}/{region}?page={page}"

    def parse_list(self, html: str, *, url: str, subcategory: str, page: int, seen_at: str) -> ListPage:
        doc = BeautifulSoup(html, "html.parser")
        out = []
        for pos, card in enumerate(doc.select("#results article.result"), 1):
            link = card.select_one("a.result__link[href]")
            href = urljoin(ORIGIN, link.get("href", "")) if link else ""
            aid = ad_id(href)
            if not aid:
                continue
            title = squash(card.select_one(".result__title")) or None
            fields = {
                "city_label": squash(card.select_one(".result__city")) or None,
                "sample": {
                    "name": squash(card.select_one(".result__name")) or None,
                    "age": _int(squash(card.select_one(".result__age")), r"\d{2}"),
                    "ethnicity": squash(card.select_one(".result__ethnicity")) or None,
                    "verified": card.has_attr("data-verified"),
                    "rate": _int(squash(card.select_one(".result__rate")), r"\d+"),
                    "available": card.has_attr("data-available"),
                },
            }
            out.append(
                {
                    "schema": "observation.v1",
                    "ad_id": aid,
                    "site": self.site,
                    "seen_at": seen_at,
                    "ad_url": href,
                    "list": {
                        "url": url,
                        "subcategory": subcategory,
                        "page": page,
                        "page_count": None,
                        "position": pos,
                        "sponsored": card.has_attr("data-sponsored"),
                        "sample_weight": None,
                    },
                    "card": {"title": title, **fields},
                    "bumped_at": None,  # list pages carry no absolute bump time
                    "card_hash": card_hash(title, fields),
                }
            )
        return ListPage(observations=out, page_count=page_count(doc))

    def parse_ad(self, html: str, *, url: str, fetched_at: str, fetch_reason: str) -> dict:
        doc = BeautifulSoup(html, "html.parser")
        now = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).astimezone(UTC)
        parts = [p for p in urlsplit(url).path.split("/") if p]
        slug = parts[-1] if parts else ""
        fx = facts(doc)
        sdo = schemaorg.extract(html, url)
        pins = schemaorg.pins(sdo)
        photos = list(
            dict.fromkeys(
                img.get("src", "")
                for img in doc.select("ul.photos img[src]")
                if img.get("src", "").startswith(IMG)
            )
        )
        phone, email, web = (_contact(doc, k) for k in ("phone", "email", "website"))
        return {
            "schema": "listing.v1",
            "ad_id": ad_id(url) or "",
            "fetched_at": fetched_at,
            "fetch_reason": fetch_reason,
            "source": {
                "site": self.site,
                "url": url,
                "category": parts[0] if len(parts) > 0 else None,
                "subcategory": parts[1] if len(parts) > 1 else None,
                "region": parts[2] if len(parts) > 2 else None,
            },
            "content": {
                "title": squash(doc.select_one("h1")),
                "description": body_text(doc.select_one("#description")),
                "contact": {
                    "phone_masked": squash(phone) or None,
                    "email_masked": squash(email) or None,
                    "website": squash(web) or None,
                    "whatsapp": _contact(doc, "whatsapp") is not None,
                },
                "media_urls": photos,
            },
            "time": {"first_published": schemaorg.date_published(sdo), "bumped_at": parse_bump(doc, now)},
            "location": {
                "city_label": squash(doc.select_one("#city")) or None,
                "pin": {**pins[0], "approximate": True} if pins else None,
                "text_hint": None,
                "geocode": None,
                "city_centre": None,
            },
            "site": {
                "sample": {
                    "city_code": slug.split("_", 1)[0] if "_" in slug else None,
                    "name": squash(doc.select_one("#name")) or None,
                    "age": _int(squash(doc.select_one("#age")), r"\d{2}"),
                    "ethnicity": squash(doc.select_one("#ethnicity")) or None,
                    "service_mode": list(
                        dict.fromkeys(re.findall(r"incall|outcall", (fx.get("availability") or "").lower()))
                    ),
                    "tagline": squash(doc.select_one("#tagline")) or None,
                    "services": services(doc),
                    "page_schema_type": schemaorg.main_type(sdo),
                }
            },
        }
