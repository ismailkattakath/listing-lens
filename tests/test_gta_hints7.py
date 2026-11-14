"""gta-hints@7 and `llens places-resolve` on a SYNTHETIC gazetteer written to a temp DATA root: invented
streets, malls, stations and places only. @6's own behaviour is pinned by test_gta_hints.py."""

import gzip
import json
import zipfile

import pytest

from listing_lens import places
from listing_lens.extractors import gazetteer as gz
from listing_lens.extractors.gta_hints import GtaHints, GtaHints7


def _el(name, lat=43.6, lon=-79.5, **tags):
    return {"type": "node", "id": hash(name) % 10**6, "lat": lat, "lon": lon, "tags": {"name": name, **tags}}


@pytest.fixture
def data(tmp_path):
    snap = tmp_path / gz.SNAPSHOT
    snap.mkdir(parents=True)
    (snap / "osm-places.json").write_text(json.dumps({"elements": [_el("Synthville", place="town")]}))
    (snap / "osm-malls.json").write_text(
        json.dumps({"elements": [_el("Zorblax Galleria", shop="mall"), _el("Mall", shop="mall")]})
    )
    (snap / "osm-other.json").write_text(
        json.dumps({"elements": [_el("Quixley Institute", amenity="college", short_name="QXI")]})
    )
    (snap / "osm-stations.json").write_text(
        json.dumps(
            {"elements": [_el("Vantor", railway="station"), _el("Plumbrook Junction", railway="station")]}
        )
    )
    (snap / "wikidata-aliases.json").write_text(json.dumps({"Q1": ["Quixley Institute", ["QXI"]]}))
    with zipfile.ZipFile(snap / "XX.zip", "w") as z:  # the synthetic site's GeoNames country and admin1
        z.writestr("XX.txt", "1\tGrombleton\tGrombleton\t\t44.2\t-79.5\tP\tPPL\tXX\t\t99\t\t\t\t0\t\t\t\t\n")
    with zipfile.ZipFile(snap / "alternatenames_XX.zip", "w") as z:
        z.writestr("XX.txt", "")
    geom = tmp_path / gz.GEOM_SNAPSHOT
    geom.mkdir(parents=True)
    ways = [
        {"id": 1, "name": "Vantor Road", "highway": "primary", "g": [[43.6, -79.5], [43.61, -79.5]]},
        {"id": 2, "name": "Krell Avenue", "highway": "secondary", "g": [[43.6, -79.5], [43.6, -79.49]]},
        {"id": 3, "name": "Juniper Lane", "highway": "residential", "g": [[43.7, -79.5], [43.71, -79.5]]},
        {"id": 4, "name": "Grombleton Road", "highway": "primary", "g": [[43.8, -79.5], [43.81, -79.5]]},
    ]
    with gzip.open(geom / "osm-streets-geom.slim.json.gz", "wt") as fh:
        json.dump({"ways": ways}, fh)
    return tmp_path


def test_name_and_registration():
    assert GtaHints7.name == "gta-hints@7"


def test_arterial_bases_are_street_words_residential_and_places_are_not(data):
    lex = gz.lexicon(data)
    assert {"vantor", "krell"} <= lex.roadwords
    assert "juniper" not in lex.roadwords  # residential
    assert "grombleton" not in lex.roadwords  # a GeoNames populated place


def test_intersection_of_arterial_words(data):
    h = GtaHints7(data).extract("Synthetic\nnear Vantor & Krell")
    assert h == {"raw": "Vantor & Krell", "kind": "intersection", "extractor": "gta-hints@7"}
    assert GtaHints().extract("Synthetic\nnear Vantor & Krell") is None  # @6 does not know them


def test_residential_names_do_not_make_an_intersection(data):
    assert GtaHints7(data).extract("Synthetic\nJuniper & Krell vibes") is None


def test_distinctive_landmark_matches_generic_one_does_not(data):
    e = GtaHints7(data)
    assert e.extract("Synthetic\nsteps from zorblax galleria")["raw"] == "zorblax galleria"
    assert e.extract("Synthetic\nthe Mall is closed") is None


def test_station_only_beside_a_cue(data):
    e = GtaHints7(data)
    assert e.extract("Synthetic\nnear Plumbrook Junction station")["kind"] == "landmark"
    assert e.extract("Synthetic\n5 min from MTX Plumbrook Junction")["kind"] == "landmark"
    assert e.extract("Synthetic\nPlumbrook Junction vibes") is None


def test_refuses_without_the_snapshot(tmp_path):
    with pytest.raises(SystemExit):
        GtaHints7(tmp_path)


def test_vote_needs_two_strong_sources():
    assert places.vote({"osm": {"Quixley Institute"}, "wikidata": {"Quixley Institute"}})[0] == "resolved"
    assert places.vote({"osm": {"Quixley Institute"}, "initials": {"Quixley Institute"}})[0] == "unresolved"
    assert places.vote({"osm": {"Quixley Institute"}, "wikidata": {"Zorblax Galleria"}})[0] == "conflicting"
    assert places.vote({"initials": {"Quixley Institute"}}) == ("unresolved", None, [])


def test_initials():
    assert places.initials("Sample Metro Centre") == "SMC"
    assert places.initials("Bay") is None


def test_candidates_skip_gold_operators_and_words():
    src = {"QXI": {"osm": {"Quixley Institute"}}, "NO": {"initials": {"North Oak"}}}
    rows = [
        {"ad_id": "1", "content": {"title": "t", "description": "incall near QXI, NO rush"}},
        {"ad_id": "2", "content": {"title": "t", "description": "incall near QXI"}},
    ]
    seen = places.candidates(rows, src, excluded_ops={"op2"}, op={"1": "op1", "2": "op2"}, words={"no"})
    assert seen == {"QXI": {"op1"}}


def test_resolved_scheme_feeds_the_extractor(data):
    resolved = [{"token": "QXI", "query": "Quixley Institute, Synthville", "sources": ["osm", "wikidata"]}]
    (data / "vocab").mkdir()
    (data / "vocab/places.ttl").write_text(places.turtle(resolved, "0.1.0"))
    h = GtaHints7(data).extract("Synthetic\nincall by QXI tonight")
    assert h["kind"] == "landmark" and h["query"] == "Quixley Institute, Synthville"


@pytest.mark.parametrize("text", ["near EP 1", "by ex plaza", "SMC area", "close to QTC"])
def test_at6_aliases_survive_in_at7(data, text):
    a6, a7 = GtaHints().extract(f"Synthetic\n{text}"), GtaHints7(data).extract(f"Synthetic\n{text}")
    assert a6 and a7 and (a6["raw"], a6.get("query")) == (a7["raw"], a7.get("query"))
