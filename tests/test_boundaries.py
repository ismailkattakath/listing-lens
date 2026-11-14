"""The region rule by boundary (boundaries.py), radius as the fallback. TOY polygons around a SYNTHETIC label
centre; the synthetic site file's [boundaries] dataset, synthetic area codes and labels only."""

import io
import json
import math
import zipfile

import pytest
from conftest import FIXTURES
from scrape_harness.cli import main

from listing_lens import boundaries, derive, site
from listing_lens import place_apply as pa

CFG = boundaries.require()
ID = CFG.id_field

C = {"lat": 43.6, "lon": -79.5}


def at(n_km=0.0, e_km=0.0, of=C):
    return {
        "lat": of["lat"] + n_km / 111.195,
        "lon": of["lon"] + e_km / (111.195 * math.cos(math.radians(of["lat"]))),
    }


def square(w, s, e, n, of=C):
    """A polygon from km offsets (west, south, east, north) around `of`, as GeoJSON lon/lat."""
    pts = [at(s, w, of), at(s, e, of), at(n, e, of), at(n, w, of), at(s, w, of)]
    return {"type": "Polygon", "coordinates": [[[p["lon"], p["lat"]] for p in pts]]}


FEATURES = [
    {"type": "Feature", "properties": {ID: "9901"}, "geometry": square(-10, -10, 10, 10)},
    {"type": "Feature", "properties": {ID: "9902"}, "geometry": square(10, -10, 30, 10)},
]
LABELS = {
    "Synthville": ["9901"],
    "Twinburg": ["9901", "9902"],
    "Ghostlabel": ["9999"],
    "Halfghost": ["9901", "9999"],
}
B = boundaries.Boundaries(FEATURES, LABELS, CFG)


def test_inside_outside_and_the_edge_buffer():
    assert B.contains("Synthville", at(5, 5)) is True
    assert B.contains("Synthville", at(0, 12.5)) is True  # 2.5 km outside: within EDGE_M (3 km)
    assert B.contains("Synthville", at(0, -12.5)) is True  # the same on the far side
    assert B.contains("Synthville", at(0, -13.5)) is False  # 3.5 km outside


def test_a_multi_cd_label_is_the_union():
    p = at(0, 20)
    assert B.contains("Twinburg", p) is True and B.contains("Synthville", p) is False


def test_an_unmapped_label_or_an_unknown_cd_falls_back_to_the_radius():
    assert not B.covers("Nowhere") and not B.covers("Ghostlabel") and B.unknown_cds == ["9999"]
    assert not B.covers("Halfghost")  # one unknown CD: the union would be too small, so the radius decides
    p = at(48)  # like a mall 48 km from the label centre: the radius passes it, the boundary does not
    assert derive.region_validity(p, None, C, "Ghostlabel", B) == (True, None)
    assert derive.region_test("Ghostlabel", C, B) == "radius"
    assert derive.region_validity(p, None, C, "Synthville", B) == (False, None)
    assert derive.region_test("Synthville", C, B) == "boundary"
    assert derive.region_test("Synthville", None, None) is None


def test_derive_flags_by_boundary_and_records_the_test():
    listing = {"location": {"city_label": "Synthville", "pin": at(48)}}
    geo = {"provider": "fixture", "status": "OK", "precise": True, "point": at(1)}
    centre = {"label": "Synthville", "point": C, "provider": "fixture"}
    d = derive.enrich(listing, {"raw": "x", "kind": "address"}, geo, centre, {}, "m", B)["derived"]
    assert (d["location_valid_pin"], d["location_valid_text"]) == (False, True)
    assert d["location_invalid_reason"] == "out_of_region_pin" and d["location_region_test"] == "boundary"
    r = derive.enrich(listing, {"raw": "x", "kind": "address"}, geo, centre, {}, "m")["derived"]
    assert r["location_valid_pin"] is True and r["location_region_test"] == "radius"


def test_the_final_place_point_uses_the_same_boundary():
    r = {
        "source": {"site": "example.test"},
        "ad_id": "1",
        "location": {
            "city_label": "Synthville",
            "pin": at(48),
            "city_centre": {"label": "Synthville", "point": C},
        },
        "derived": {"method": "m", "geocode_decision": None, "pin_verdict": "no_text"},
    }
    f = pa.place_fields(r, None, None, None, lambda q: None, "fixture", None, B)
    assert f["place_point_source"] == "pin" and f["location_valid_place"] is False
    assert pa.place_fields(r, None, None, None, lambda q: None, "fixture")["location_valid_place"] is True


def write_data(d, features=FEATURES, labels=LABELS):
    g = d / CFG.dir
    g.mkdir(parents=True, exist_ok=True)
    (g / CFG.slim).write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    (d / "vocab").mkdir(parents=True, exist_ok=True)
    (d / boundaries.MAPPING).write_text(json.dumps({"schema": "label-cd.v1", "labels": labels}))


def test_load_needs_both_files_and_the_schema(tmp_path):
    assert boundaries.Boundaries.load(tmp_path) is None
    write_data(tmp_path)
    assert boundaries.Boundaries.load(tmp_path).covers("Synthville")
    (tmp_path / boundaries.MAPPING).write_text(json.dumps({"labels": {}}))
    with pytest.raises(ValueError, match="label-cd.v1"):
        boundaries.Boundaries.load(tmp_path)


def test_build_keeps_the_configured_areas_and_reprojects_to_wgs84(tmp_path):
    import shapefile
    from pyproj import Transformer

    to_m = Transformer.from_crs("EPSG:4326", CFG.source_crs, always_xy=True).transform
    ring = FEATURES[0]["geometry"]["coordinates"][0]
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON, encoding="cp1252")
    for f in (ID, *CFG.fields, CFG.keep_field):
        w.field(f, "C")
    for code, keep in (("9901", CFG.keep_value), ("2499", "24")):  # one kept area, one elsewhere
        w.poly([[list(to_m(lon, lat)) for lon, lat in reversed(ring)]])
        w.record(code, f"Synthétique {code}", "CTY", keep)  # a Windows-1252 é, as in accented French names
    w.close()
    z = tmp_path / CFG.zip
    with zipfile.ZipFile(z, "w") as zf:
        for ext, buf in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            zf.writestr("sample-areas" + ext, buf.getvalue())
    out = boundaries.build(z, tmp_path / CFG.slim, CFG)
    assert out["features"] == 1
    feats = json.loads((tmp_path / CFG.slim).read_text())["features"]
    assert list(feats[0]["properties"]) == [ID, *CFG.fields]  # the id, then the kept fields, in order
    assert feats[0]["properties"][ID] == "9901"
    assert (
        feats[0]["properties"][CFG.fields[0]] == "Synthétique 9901"
    )  # no .cpg in the zip: decoded as cp1252
    lons = [p[0] for p in feats[0]["geometry"]["coordinates"][0]]
    lats = [p[1] for p in feats[0]["geometry"]["coordinates"][0]]
    want = [p for p in ring]
    assert abs(min(lons) - min(x for x, _ in want)) < 1e-3 and abs(max(lats) - max(y for _, y in want)) < 1e-3


FAST = ["--max-tasks-per-minute", "6000", "--approve-politeness-override"]
FIX = ["--driver", "fixture", "--fixtures", str(FIXTURES), *FAST]
GEO = ["--geocoder", "fixture", "--geocode-fixture", str(FIXTURES / "geocode-answers.json")]


def test_derive_uses_the_mapping_and_reports_unmapped_labels(data_dir):
    exampleton = {"lat": 43.75, "lon": -79.45}  # the fixture geocoder's label centre
    write_data(
        data_dir,
        [
            {
                "type": "Feature",
                "properties": {ID: "9901"},
                "geometry": square(-30, -30, 30, 30, exampleton),
            }
        ],
        {"Exampleton": ["9901"]},
    )
    assert main(["crawl-lists", "--subcategory", "sample-list-a", "--stride", "2", "--start", "1", *FIX]) == 0
    assert main(["fetch-ads", *FIX]) == 0
    assert main(["geocode", *GEO]) == 0
    assert main(["derive", "--provider", "fixture-geocoding"]) == 0
    rows = [json.loads(x) for x in (data_dir / "derived/listings.latest.jsonl").read_text().splitlines()]
    by = {r["location"].get("city_label"): r["derived"]["location_region_test"] for r in rows}
    assert by.get("Exampleton") == "boundary" and "+cd2021+" in rows[0]["derived"]["method"]
    s = json.loads((data_dir / "derived/summary.json").read_text())["region_test"]
    assert s["by_test"].get("boundary", 0) >= 1 and "Exampleton" not in s["unmapped_labels"]


# ── no [boundaries]: no boundary rule, and the build refuses (never a default dataset) ──────────────────────


@pytest.fixture
def site_file(tmp_path, monkeypatch):
    site._load.cache_clear()

    def write(text: str):
        (tmp_path / "site.toml").write_text(text)
        monkeypatch.setenv(site.ENV, str(tmp_path / "site.toml"))
        site._load.cache_clear()

    yield write
    site._load.cache_clear()


def test_without_a_boundaries_table_there_is_no_boundary_rule(site_file, tmp_path):
    site_file("[region]\nkm = 60.0\n")
    assert boundaries.config() is None
    write_data(tmp_path)  # even with a slim file and a mapping on disk
    assert boundaries.Boundaries.load(tmp_path) is None  # the radius rule alone
    with pytest.raises(boundaries.BoundariesConfigError, match=r"no \[boundaries\] table"):
        boundaries.require()


def test_an_incomplete_boundaries_table_refuses_naming_the_key(site_file):
    site_file('[boundaries]\ndir = "x"\nzip = "y.zip"\n')
    with pytest.raises(boundaries.BoundariesConfigError, match="'slim'"):
        boundaries.config()


def test_boundaries_build_refuses_without_the_table(site_file, data_dir):
    site_file("[region]\nkm = 60.0\n")
    with pytest.raises(SystemExit, match=r"refusing: .*no \[boundaries\] table"):
        main(["boundaries-build"])
