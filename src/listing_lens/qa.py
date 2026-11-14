"""A self-contained QA page over derived listings: what the ad wrote, what the geocoder made of
it, and what the pin measurement concluded. Every value is HTML-escaped; no script, no remote asset."""

from __future__ import annotations

import html
import urllib.parse

from .derive import km

COLUMNS = [
    "Ad",
    "City label",
    "Phrase as written",
    "Kind",
    "Geocoder resolution",
    "Verdict",
    "Confidence",
    "km pin→text",
    "km pin→city",
]


def _maps(point: dict | None) -> str:
    if not point:
        return ""
    q = urllib.parse.quote(f"{point['lat']},{point['lon']}")
    return f' <a href="https://www.google.com/maps/search/?api=1&amp;query={q}" rel="noreferrer">map</a>'


def render(rows: list[dict], target: dict | None = None, title: str = "Listing QA") -> str:
    """target, when given, sorts rows by distance from it. It is a run argument, never a constant."""
    if target:

        def dist(r: dict) -> float:
            p = r["location"].get("pin") or (r["location"].get("geocode") or {}).get("point")
            return km(target, p) if p else float("inf")

        rows = sorted(rows, key=dist)
    e = html.escape
    body = []
    for r in rows:
        loc, d = r["location"], r.get("derived") or {}
        hint, geo = loc.get("text_hint") or {}, loc.get("geocode") or {}
        cells = [
            f'<a href="{e(r["source"]["url"], quote=True)}" rel="noreferrer">{e(r["ad_id"])}</a>',
            e(loc.get("city_label") or ""),
            e(hint.get("raw", "")),
            e(hint.get("kind", "")),
            e(geo.get("formatted") or geo.get("status", "")) + _maps(geo.get("point")),
            e(str(d.get("pin_verdict") or "")) + _maps(loc.get("pin")),
            e(d.get("confidence", "")),
            e("" if d.get("km_pin_text") is None else f"{d['km_pin_text']:.2f}"),
            e("" if d.get("km_pin_city") is None else f"{d['km_pin_city']:.2f}"),
        ]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    head = "".join(f"<th>{e(c)}</th>" for c in COLUMNS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#fff; --fg:#1b1b1b; --line:#ddd; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --fg:#e8e8e8; --line:#333; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.4 system-ui,sans-serif; margin:16px; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ border-bottom:1px solid var(--line); padding:4px 6px;
text-align:left; vertical-align:top; }} th {{ position:sticky; top:0; background:var(--bg); }}
</style></head><body>
<h1>{e(title)}</h1><p>{len(rows)} listings. Pin and text are both advertiser-supplied: agreement shows consistency, not truth.</p>
<div style="overflow-x:auto"><table><thead><tr>{head}</tr></thead><tbody>
{chr(10).join(body)}
</tbody></table></div></body></html>
"""
