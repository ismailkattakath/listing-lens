"""Region by BOUNDARY: a point is in region iff it lies inside the union of its label's areas (e.g. census
divisions) from a boundary dataset the site file names; the radius rule (derive.REGION_KM from the label centre)
stays as the fallback for a label with no mapping, and for a site file with no [boundaries] table.

The dataset is the site's, configured in its site file (never in code), DATA only (never git):

    [boundaries]
    dir = "<folder under DATA>"             # holds the original zip, its manifest (licence, attribution), the slim file
    zip = "<the original shapefile zip>"
    slim = "<the slim GeoJSON llens boundaries-build writes>"
    source_crs = "EPSG:<n>"                 # the zip's own METRIC projection: simplification and containment run in it
    id_field = "<the area id column>"       # the ids vocab/label-cd.json maps labels to
    fields = ["<name column>", ...]         # kept beside the id, in this order
    keep = { field = "<column>", value = "<value>" }   # the rows kept, e.g. one province's
    dbf_encoding = "cp1252"                 # the .dbf's text encoding when the zip ships no .cpg
    describe = "<one line for the run card>"

- the slim file: written by `llens boundaries-build`, the kept areas only, reprojected from source_crs to WGS84,
  simplified within SIMPLIFY_M;
- vocab/label-cd.json (label-cd.v1): {"labels": {"<site city label>": ["<area id>", ...]}}, the operator's
  mapping; a multi-area label is the union.

Edge allowance EDGE_M: a point within it of the union still counts as inside (the operator's decision, see the
constant). It also covers the two geometric reasons for a small buffer: the simplification moves an edge by up
to SIMPLIFY_M, and a cartographic file erases coastal water, so a waterfront pin just offshore of the drawn
shoreline is not another region.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import site

MAPPING = "vocab/label-cd.json"
SIMPLIFY_M = 50.0
# Operator decision (2026-11-13): a 3 km edge allowance. Evidence: with 500 m, the boundary rule newly flagged 30
# current points, 1.4-28.1 km outside their label's areas (p25 3.0 km, median 9.6 km), every one in a NEIGHBOURING
# area; 3 km forgives a near-border spill between two adjacent labels while the real mismatches stay flagged.
EDGE_M = 3000.0


class BoundariesConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    dir: str
    zip: str
    slim: str
    source_crs: str
    id_field: str
    fields: tuple[str, ...]
    keep_field: str
    keep_value: str
    dbf_encoding: str
    describe: str


def config() -> Config | None:
    """The site file's [boundaries] table; None when it has none (no boundary rule: the radius alone). A table
    that is there but incomplete refuses, naming the missing key: never a silent default dataset."""
    raw = site.config().get("boundaries")
    if raw is None:
        return None
    keys = ("dir", "zip", "slim", "source_crs", "id_field", "fields", "keep", "dbf_encoding", "describe")
    if missing := [k for k in keys if k not in raw] + [
        f"keep.{k}" for k in ("field", "value") if k not in raw.get("keep", {})
    ]:
        raise BoundariesConfigError(f"site config {site.path()}: [boundaries] has no {missing[0]!r}")
    return Config(
        dir=raw["dir"],
        zip=raw["zip"],
        slim=raw["slim"],
        source_crs=raw["source_crs"],
        id_field=raw["id_field"],
        fields=tuple(raw["fields"]),
        keep_field=raw["keep"]["field"],
        keep_value=str(raw["keep"]["value"]),
        dbf_encoding=raw["dbf_encoding"],
        describe=raw["describe"],
    )


def require() -> Config:
    """config(), or a refusal: the boundary step never runs on a default dataset."""
    cfg = config()
    if cfg is None:
        raise BoundariesConfigError(
            f"site config {site.path()} has no [boundaries] table: the boundary dataset is the site's, "
            "named there (dir, zip, slim, source_crs, id_field, fields, keep, dbf_encoding, describe)"
        )
    return cfg


def build(zip_path: Path, out: Path, cfg: Config, simplify_m: float = SIMPLIFY_M) -> dict:
    """The slim file from the original zip: the kept areas, simplified in metres (source_crs), then WGS84."""
    import io
    import zipfile

    import shapefile
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    to_wgs = Transformer.from_crs(cfg.source_crs, "EPSG:4326", always_xy=True).transform
    with zipfile.ZipFile(zip_path) as z:
        stem = next(n[:-4] for n in z.namelist() if n.lower().endswith(".shp"))
        parts = {ext: io.BytesIO(z.read(stem + ext)) for ext in (".shp", ".shx", ".dbf")}
        prj = next((z.read(n).decode() for n in z.namelist() if n.lower().endswith(".prj")), None)
        cpg = next((z.read(n).decode().strip() for n in z.namelist() if n.lower().endswith(".cpg")), None)
    # A .dbf's text encoding is named by its .cpg; without one it is the configured dbf_encoding (the ESRI
    # default is Windows-1252, in which accented names are not UTF-8).
    r = shapefile.Reader(
        shp=parts[".shp"], shx=parts[".shx"], dbf=parts[".dbf"], encoding=cpg or cfg.dbf_encoding
    )
    names = [f[0] for f in r.fields[1:]]
    feats = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(names, sr.record, strict=True))
        if str(rec.get(cfg.keep_field)) != cfg.keep_value:
            continue
        g = shape(sr.shape.__geo_interface__).simplify(simplify_m, preserve_topology=True)
        feats.append(
            {
                "type": "Feature",
                "properties": {cfg.id_field: str(rec[cfg.id_field]), **{f: rec.get(f) for f in cfg.fields}},
                "geometry": mapping(transform(to_wgs, g)),
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}) + "\n")
    return {"features": len(feats), "simplify_m": simplify_m, "source_prj": (prj or "")[:80], "out": out.name}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Boundaries:
    """label -> the buffered union of its areas, in metres; `contains` answers None for an unmapped label."""

    def __init__(
        self, features: list[dict], labels: dict[str, list[str]], cfg: Config, edge_m: float = EDGE_M
    ):
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform, unary_union
        from shapely.prepared import prep

        self._to_m = Transformer.from_crs("EPSG:4326", cfg.source_crs, always_xy=True).transform
        cds = {f["properties"][cfg.id_field]: transform(self._to_m, shape(f["geometry"])) for f in features}
        self.unknown_cds = sorted({c for v in labels.values() for c in v} - set(cds))
        self.labels: dict = {}
        for label, codes in labels.items():
            geoms = [cds[c] for c in codes if c in cds]
            if geoms and len(geoms) == len(codes):  # a label naming an unknown area is not mapped (fallback)
                self.labels[label] = prep(unary_union(geoms).buffer(edge_m))
        self.edge_m = edge_m

    @classmethod
    def load(cls, data: Path) -> Boundaries | None:
        """None: no [boundaries] in the site file, or no slim file or mapping -> the radius rule alone."""
        cfg = config()
        if cfg is None:
            return None
        slim, mp = data / cfg.dir / cfg.slim, data / MAPPING
        if not (slim.exists() and mp.exists()):
            return None
        m = json.loads(mp.read_text())
        if m.get("schema") != "label-cd.v1":
            raise ValueError(f"{MAPPING}: schema must be label-cd.v1")
        return cls(json.loads(slim.read_text())["features"], m["labels"], cfg)

    def covers(self, label: str | None) -> bool:
        return bool(label) and label in self.labels

    def contains(self, label: str | None, p: dict | None) -> bool | None:
        if not (p and self.covers(label)):
            return None
        from shapely.geometry import Point

        return self.labels[label].contains(Point(self._to_m(p["lon"], p["lat"])))
