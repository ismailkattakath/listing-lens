"""The site config: everything li knows about ONE site's world (its gazetteer, region, places), read at
runtime from DATA, never from code. The repo stays site-agnostic; its tests use a synthetic site
(tests/fixtures/site/).

Located by $LLENS_SITE_CONFIG, else <DATA>/config/site.toml. A missing or unreadable file is an error, never
an empty vocabulary: an extractor with no gazetteer would silently find nothing.

site.toml (li's tables; paths are relative to the file):
    [region]  km = <the out-of-region radius, pre-registered>
    [places]  box = [south, west, north, east]; context = "<region phrase>"; seeds = { TOKEN = "query" }
    [vocab]   hints = "<path to the hint extractor's vocabulary JSON>"
"""

from __future__ import annotations

import json
import os
import tomllib
from functools import cache
from pathlib import Path

ENV = "LLENS_SITE_CONFIG"
DEFAULT = "config/site.toml"  # under DATA
HINTS_SCHEMA = "li-hints-vocab.v2"
# v2 over v1: the region's postal grammar and the frozen @8.2/@8.3 rule words moved out of code into the vocabulary.
HINTS_REQUIRED = ("postal", "fsa", "label_noise")


class SiteConfigError(RuntimeError):
    pass


def path() -> Path:
    if raw := os.environ.get(ENV):
        return Path(raw).expanduser()
    from scrape_harness import store

    return store.data_dir() / DEFAULT


@cache
def _load(p: str) -> dict:
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise SiteConfigError(f"no site config at {p}: set {ENV}, or write DATA/{DEFAULT}") from None
    except tomllib.TOMLDecodeError as e:
        raise SiteConfigError(f"site config {p} is not valid TOML: {e}") from None


@cache
def source():
    """The configured default source (an instance): the pipeline's default_site (the site file's
    [pipeline.consumer]) resolved through the harness's plug-in registry. The core reads the site specifics
    (record_key, host, tz, regions, region(), upload_re) through it, never from the source module."""
    from scrape_harness import pipeline
    from scrape_harness.interfaces import load

    return load("sources", pipeline.consumer().default_site)


def config() -> dict:
    return _load(str(path().resolve()))


def get(table: str, key: str):
    """A required value: a missing table or key is an error naming both."""
    try:
        return config()[table][key]
    except KeyError:
        raise SiteConfigError(f"site config {path()} has no [{table}] {key}") from None


@cache
def _vocab(p: str) -> dict:
    try:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SiteConfigError(f"site vocabulary {p} (site config [vocab]) is absent") from None
    if doc.get("schema") != HINTS_SCHEMA:
        raise SiteConfigError(f"site vocabulary {p}: schema is {doc.get('schema')!r}, not {HINTS_SCHEMA!r}")
    if missing := [k for k in HINTS_REQUIRED if k not in doc]:
        raise SiteConfigError(f"site vocabulary {p}: no {', '.join(missing)} ({HINTS_SCHEMA} requires them)")
    return doc


def hints_vocab() -> dict:
    """The hint extractor's vocabulary (word lists and ordered regex fragments, by version key)."""
    rel = get("vocab", "hints")
    return _vocab(str((path().resolve().parent / rel).resolve()))
