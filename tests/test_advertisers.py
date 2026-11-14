"""Advertiser clusters over SYNTHETIC listings: each key kind links, a venue alone does not make an
advertiser, and the result is deterministic and schema-valid."""

import copy
import json

import pytest
from conftest import EXAMPLES
from scrape_harness import contracts

from listing_lens.advertisers import UnionFind, clusters, keys

BASE = json.loads((EXAMPLES / "listing.example.json").read_text())


def ad(aid, title="Synthetic", desc="", name=None, hint=None):
    r = copy.deepcopy(BASE)
    r["ad_id"] = aid
    r["content"]["title"], r["content"]["description"] = title, desc
    r["site"]["sample"]["name"] = name
    r["location"]["text_hint"] = {"raw": hint, "kind": "area", "extractor": "t"} if hint else None
    return r


def by_ad(rows):
    return {aid: a for a in rows for aid in a["ad_ids"]}


def test_union_find_is_deterministic():
    uf = UnionFind()
    uf.union("b", "a")
    uf.union("c", "b")
    assert uf.find("c") == uf.find("a") == "a"


def test_each_key_kind_links_two_ads():
    long = "a synthetic description invented for this test only " * 6  # > 200 normalised characters
    pairs = {
        "ph4": (ad("1", desc="call view-0142"), ad("2", desc="text 555-010-0142")),
        "h": (ad("3", desc="see @synthetic_handle"), ad("4", desc="onlyfans.com/synthetic_handle")),
        "d": (ad("5", desc=long), ad("6", desc=long + " plus a different ending")),
        "n": (ad("7", name="Exampla"), ad("8", name="exampla")),
        "t": (ad("9", title="Synthetic example title here"), ad("10", title="synthetic-example title here")),
    }
    for kind, (a, b) in pairs.items():
        rows = clusters([a, b])
        assert len(rows) == 1 and kind in rows[0]["link_kinds"], kind
        assert not rows[0]["venue_only"]


def test_a_shared_venue_alone_is_flagged_not_an_advertiser():
    rows = clusters([ad("11", hint="Example St & Sample Ave"), ad("12", hint="example st & sample ave")])
    (cl,) = rows
    assert cl["ad_ids"] == ["11", "12"] and cl["link_kinds"] == ["v"] and cl["venue_only"]


def test_unrelated_ads_stay_apart_and_the_result_validates():
    rows = clusters([ad("13", name="Alpha"), ad("14", name="Beta"), ad("15", name="Alpha")])
    m = by_ad(rows)
    assert m["13"] is m["15"] and m["14"] is not m["13"]
    for a in rows:
        contracts.validate("advertiser.v1", a)
    assert clusters([ad("15", name="Alpha"), ad("13", name="Alpha"), ad("14", name="Beta")]) == rows


def test_short_values_are_not_keys():
    assert keys(ad("16", title="short", desc="tiny", name="ab")) == set()


def test_only_the_first_200_normalised_characters_of_a_description_count():
    head = "x" * 200
    assert len(clusters([ad("17", desc=head + "one ending"), ad("18", desc=head + "another")])) == 1
    assert len(clusters([ad("19", desc="y" * 120 + "one"), ad("20", desc="y" * 120 + "two")])) == 2


def test_links_name_every_directly_shared_key_once_per_pair():
    from listing_lens.advertisers import links

    ads = [
        ad("1", desc="call view-0142", name="Exampla"),
        ad("2", desc="text 555-010-0142", name="exampla"),
        ad("3", title="Synthetic example title here", name="Exampla"),
        ad("9", desc="nothing shared"),
    ]
    rows = clusters(ads)
    got = links(ads, rows)
    pairs = {(x["ad_a"], x["ad_b"], x["link_kind"]) for x in got}
    assert pairs == {("1", "2", "phone4"), ("1", "2", "name"), ("1", "3", "name"), ("2", "3", "name")}
    adv = by_ad(rows)
    assert all(
        x["advertiser_id"] == adv[x["ad_a"]]["advertiser_id"] == adv[x["ad_b"]]["advertiser_id"] for x in got
    )
    assert {x["link_key"] for x in got if x["link_kind"] == "phone4"} == {"0142"}
    for x in got:
        assert x["ad_a"] < x["ad_b"]
        contracts.validate("advertiser_link.v1", x)
    assert links(ads, rows) == got  # deterministic


def test_long_text_keys_are_fingerprints_not_the_text():
    from listing_lens.advertisers import links

    long = "a synthetic description invented for this test only " * 6
    ads = [
        ad("5", desc=long, title="Synthetic example title here"),
        ad("6", desc=long, title="synthetic-example title here"),
    ]
    got = {x["link_kind"]: x["link_key"] for x in links(ads, clusters(ads))}
    assert set(got) == {"desc", "title"}
    assert all(len(v) == 12 and "synthetic" not in v for v in got.values())


def test_photo_links_cross_advertisers_and_never_cluster():
    from listing_lens.advertisers import links

    shared = "https://imx.example.invalid/synthetic-shared.jpg"
    a1, a2, a3 = ad("21", name="Exampla"), ad("22", name="exampla"), ad("23", name="Otherone")
    a1["content"]["media_urls"] = [shared, "https://imx.example.invalid/only-21.jpg"]
    a3["content"]["media_urls"] = [shared]
    rows = clusters([a1, a2, a3])
    adv = by_ad(rows)
    assert (
        adv["21"]["advertiser_id"] == adv["22"]["advertiser_id"] != adv["23"]["advertiser_id"]
    )  # photos did not merge
    photo = [x for x in links([a1, a2, a3], rows) if x["link_kind"] == "photo"]
    assert len(photo) == 1
    p = photo[0]
    assert (p["ad_a"], p["ad_b"], p["used_for_clustering"]) == ("21", "23", False)
    assert (p["advertiser_id"], p["advertiser_b"]) == (adv["21"]["advertiser_id"], adv["23"]["advertiser_id"])
    assert len(p["link_key"]) == 12 and "synthetic" not in p["link_key"]
    contracts.validate("advertiser_link.v1", p)
    name = [x for x in links([a1, a2, a3], rows) if x["link_kind"] == "name"]
    assert name and all(x["used_for_clustering"] and x["advertiser_b"] == x["advertiser_id"] for x in name)


def test_ablation_variants_drop_one_kind_or_add_photos():
    from listing_lens.advertisers import ablation

    shared = "https://imx.example.invalid/synthetic-shared.jpg"
    a1, a2, a3 = (
        ad("31", name="Exampla"),
        ad("32", name="exampla", desc="call view-0142"),
        ad("33", desc="text 555-010-0142"),
    )
    a4 = ad("34")
    a1["content"]["media_urls"] = [shared]
    a4["content"]["media_urls"] = [shared]
    ads = [a1, a2, a3, a4]
    rows = ablation(ads)
    by = {(r["variant"], r["ad_id"]): r for r in rows}
    variants = {r["variant"] for r in rows}
    assert variants == {"baseline", "-desc", "-name", "-title", "-phone4", "-handle", "-venue", "+photo"}
    assert len(rows) == len(variants) * len(ads)  # one row per (variant, ad)
    base = {aid: by[("baseline", aid)]["advertiser_id"] for aid in ("31", "32", "33", "34")}
    assert base["31"] == base["32"] == base["33"] != base["34"]  # name + phone4 chain them; photos do not
    assert base == {
        a["ad_ids"][i]: a["advertiser_id"] for a in clusters(ads) for i in range(len(a["ad_ids"]))
    }
    assert (
        by[("-name", "31")]["advertiser_id"] != by[("-name", "32")]["advertiser_id"]
    )  # name was 31-32's only link
    assert by[("-phone4", "32")]["advertiser_id"] != by[("-phone4", "33")]["advertiser_id"]
    assert (
        by[("+photo", "31")]["advertiser_id"] == by[("+photo", "34")]["advertiser_id"]
    )  # a photo merges them
    assert by[("+photo", "34")]["n_ads"] == 4
    for r in rows:
        contracts.validate("advertiser_ablation.v1", r)


# ── advertiser.v2 (analyst spec h7, 2026-11-08): pairwise edges, a NEW method next to v1 ─────


def ad_sub(aid, sub="sample-list-a", **kw):
    r = ad(aid, **kw)
    r["source"]["subcategory"] = sub
    return r


def v2_groups(rows, col="operator_id"):
    by = {}
    for r in rows:
        by.setdefault(r[col], set()).add(r["ad_id"])
    return sorted(sorted(v) for v in by.values())


def test_v2_a_weak_key_alone_does_not_link_but_two_kinds_do():
    from listing_lens.advertisers import clusters_v2

    rows = clusters_v2([ad_sub("41", name="Exampla"), ad_sub("42", name="exampla")])
    assert v2_groups(rows) == [["41"], ["42"]]  # v1 would merge them on the name alone
    rows = clusters_v2(
        [
            ad_sub("41", name="Exampla", desc="call view-0142"),
            ad_sub("42", name="exampla", desc="text 555-010-0142"),
        ]
    )
    assert v2_groups(rows) == [["41", "42"]]  # name + phone4: two weak kinds
    assert {r["ad_id"]: r["edge_evidence"] for r in rows} == {"41": ["n", "ph4"], "42": ["n", "ph4"]}


def test_v2_one_strong_key_links_within_a_subcategory_not_across():
    from listing_lens.advertisers import clusters_v2

    long = "a synthetic description invented for this test only " * 6
    same = clusters_v2([ad_sub("51", desc=long), ad_sub("52", desc=long)])
    assert v2_groups(same) == [["51", "52"]]
    cross = clusters_v2([ad_sub("51", desc=long), ad_sub("52", sub="sample-list-b", desc=long)])
    assert v2_groups(cross) == [["51"], ["52"]]  # across FE/TS one strong kind is not enough


def test_v2_photo_upload_id_is_a_strong_key():
    from listing_lens.advertisers import clusters_v2

    a, b = ad_sub("61"), ad_sub("62")
    a["content"]["media_urls"] = ["https://img.example.invalid/UPLOAD123/a.jpg"]
    b["content"]["media_urls"] = [
        "https://img.example.invalid/UPLOAD123/b-large.jpg"
    ]  # same upload, another size
    rows = clusters_v2([a, b])
    assert v2_groups(rows) == [["61", "62"]] and rows[0]["edge_evidence"] == ["img"]


def test_v2_hub_guard_ignores_a_key_on_more_than_60_ads():
    from listing_lens.advertisers import clusters_v2

    many = [ad_sub(str(100 + i), name="Commonname", desc=f"call view-{1000 + i}") for i in range(61)]
    assert all(len(g) == 1 for g in v2_groups(clusters_v2(many)))  # the name is on 61 ads: ignored


def test_v2_operator_level_and_its_inputs():
    from listing_lens.advertisers import clusters_v2

    long = "a synthetic description invented for this test only " * 6
    names = ["Alphaa", "Bravoo", "Charly", "Deltaa", "Echoo"]
    op = [ad_sub(str(70 + i), desc=long, name=n) for i, n in enumerate(names)]
    other = "another synthetic text written only for the persona case here " * 5
    persona = [ad_sub(str(80 + i), desc=other, name="Samename") for i in range(5)]
    rows = {r["ad_id"]: r for r in clusters_v2(op + persona)}
    assert rows["70"]["level"] == "operator" and rows["70"]["n_names"] == 5
    assert rows["70"]["top_shared_key_share"] == 1.0
    assert rows["80"]["level"] == "persona" and rows["80"]["n_names"] == 1  # one name: a persona
    assert all(
        r["operator_id"].startswith("v2:") and r["persona_id"].startswith("p2:") for r in rows.values()
    )
    for r in rows.values():
        contracts.validate("advertiser.v2", r)


def test_v1_is_untouched_by_v2():
    import inspect

    from listing_lens import advertisers

    assert advertisers.METHOD.startswith("advertisers@1")
    assert "keyfn" in inspect.signature(advertisers.clusters).parameters  # v1's only seam is the ablation's


@pytest.mark.parametrize(
    ("text", "handle"),
    [
        ("click to view@gmail.com", None),  # an email domain is not a handle
        ("x@yahoo.ca", None),
        ("Telegram:@SamplesTss", "samplestss"),
        ("telegram...@Samplez10", "samplez10"),
        ("@lexi.doll", "lexi.doll"),  # not a listed TLD
        ("onlyfans.com/synthetic_handle", "synthetic_handle"),  # prefix captures untouched
    ],
)
def test_v2_handles_drop_email_domains_only(text, handle):
    from listing_lens.advertisers import keys_v2

    hs = {k[2:] for k in keys_v2(ad_sub("90", desc=text)) if k.startswith("h:")}
    assert hs == ({handle} if handle else set())


def test_v1_keeps_its_known_email_domain_defect():
    """advertisers@1 known defect (ADR-0015): "…@gmail.com" becomes the handle gmail.com. Recorded, not fixed."""
    assert "h:gmail.com" in keys(ad_sub("91", desc="click to view@gmail.com"))


def test_personas_nest_inside_operators():
    from listing_lens.advertisers import clusters_v2

    long = "a synthetic description invented for this test only " * 6
    ads = [
        ad_sub("a1", desc=long + " see @handle_one", name="Alphaa"),
        ad_sub("a2", desc=long, name="alphaa", title="Synthetic example title here"),
        ad_sub("a3", desc=long, name="Bravoo"),  # same template, another name: same operator, own persona
        ad_sub("a4", desc="@handle_one", name="Charly"),  # the handle alone: a persona edge to a1
        ad_sub("a5", sub="sample-list-b", desc="@handle_one"),  # handle alone across FE/TS: no edge
    ]
    rows = clusters_v2(ads)
    by = {r["ad_id"]: r for r in rows}
    assert v2_groups(rows, "persona_id") == [["a1", "a2", "a4"], ["a3"], ["a5"]]
    assert by["a1"]["operator_id"] == by["a3"]["operator_id"]  # one operator, two personas
    for p in {r["persona_id"] for r in rows}:  # a persona never spans two operators
        assert len({r["operator_id"] for r in rows if r["persona_id"] == p}) == 1
    assert by["a2"]["persona_edge_evidence"] == ["d", "n"] and by["a3"]["persona_edge_evidence"] == []


def test_v2_links_explain_every_merge_and_carry_no_text():
    """advertiser_v2_link.v1: one row per (accepted v2 edge, kind), from the SAME pair pass as clusters_v2, so
    the edges' components are exactly the operators and each ad's edge kinds are its edge_evidence."""
    from listing_lens.advertisers import clusters_v2, links_v2

    long = "a synthetic description invented for this test only " * 6
    ads = [
        ad_sub("a1", desc=long + " see @handle_one", name="Alphaa"),
        ad_sub("a2", desc=long, name="alphaa", title="Synthetic example title here"),
        ad_sub("a3", desc=long, name="Bravoo"),
        ad_sub("a4", desc="@handle_one", name="Charly"),
        ad_sub("a5", sub="sample-list-b", desc="@handle_one"),  # handle alone across FE/TS: no edge
        ad_sub("a6", name="Alphaa"),  # a weak name alone: no edge
    ]
    ads[0]["content"]["media_urls"] = ["https://img.example.invalid/UPLOADX/a.jpg"]
    ads[3]["content"]["media_urls"] = ["https://img.example.invalid/UPLOADX/b.jpg"]
    rows = clusters_v2(ads)
    links = links_v2(ads, rows)
    for x in links:
        contracts.validate("advertiser_v2_link.v1", x)
    by = {r["ad_id"]: r for r in rows}
    uf = UnionFind()
    kinds: dict = {}
    for x in links:
        assert (
            x["ad_a"] < x["ad_b"]
            and by[x["ad_a"]]["operator_id"] == by[x["ad_b"]]["operator_id"] == x["operator_id"]
        )
        uf.union(x["ad_a"], x["ad_b"])
        for a in (x["ad_a"], x["ad_b"]):
            kinds.setdefault(a, set()).update(x["edge_kinds"])
    comps: dict = {}
    for r in rows:
        comps.setdefault(uf.find(r["ad_id"]), set()).add(r["ad_id"])
    assert sorted(sorted(v) for v in comps.values()) == v2_groups(rows)
    short = {
        "phone4": "ph4",
        "handle": "h",
        "name": "n",
        "title": "t",
        "desc": "d",
        "venue": "v",
        "image": "img",
    }
    for r in rows:
        assert sorted(short[k] for k in kinds.get(r["ad_id"], ())) == r["edge_evidence"]
    pair = {(x["ad_a"], x["ad_b"], x["link_kind"]): x for x in links}
    assert (
        pair[("a1", "a4", "handle")]["link_key"] == "handle_one"
        and pair[("a1", "a4", "handle")]["persona_edge"]
    )
    img = pair[("a1", "a4", "image")]["link_key"]
    assert len(img) == 12 and "UPLOADX" not in img  # the upload id is part of a URL: fingerprinted
    assert all("synthetic description" not in json.dumps(x) for x in links)  # never the text
    assert not any("a6" in (x["ad_a"], x["ad_b"]) or "a5" in (x["ad_a"], x["ad_b"]) for x in links)


def test_derive_v2_writes_its_edge_file_after_the_clusters(data_dir, capsys):
    from scrape_harness import store
    from scrape_harness.cli import main

    long = "a synthetic description invented for this test only " * 6
    store.append("derived", [ad_sub("71", desc=long), ad_sub("72", desc=long)])
    capsys.readouterr()
    assert main(["derive-advertisers", "--method", "v2"]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["links"] == 1 and out["links_file"] == "derived/advertiser_v2_links.jsonl"
    d = data_dir / "derived"
    assert (d / "advertiser_v2_links.jsonl").stat().st_mtime >= (d / "advertisers_v2.jsonl").stat().st_mtime


# ── v1.1 (operator decision 2026-11-08): v1 minus email-domain handles, the DEFAULT; v1 frozen ──


def test_email_only_ads_link_under_v1_but_not_v1_1_or_v2():
    from listing_lens.advertisers import clusters_v1_1, clusters_v2

    a, b = ad_sub("e1", desc="click to view x@gmail.com"), ad_sub("e2", desc="contact y@gmail.com")
    v1 = clusters([a, b])
    assert len(v1) == 1 and v1[0]["link_kinds"] == ["h"]  # v1's known defect, frozen as the record
    v11 = clusters_v1_1([a, b])
    assert len(v11) == 2 and all(x["advertiser_id"].startswith("v1.1:") for x in v11)
    assert all(x["method"].startswith("advertisers@1.1") for x in v11)
    assert v2_groups(clusters_v2([a, b])) == [["e1"], ["e2"]]
    assert v1[0]["advertiser_id"].startswith("adv-") and v1[0]["method"].startswith("advertisers@1:")


def test_v1_1_equals_v1_when_no_email_is_involved():
    from listing_lens.advertisers import clusters_v1_1

    ads = [
        ad("1", desc="call view-0142"),
        ad("2", desc="text 555-010-0142"),
        ad("3", desc="see @synthetic_handle"),
        ad("4", desc="onlyfans.com/synthetic_handle"),
    ]
    groups = lambda rows: sorted(sorted(x["ad_ids"]) for x in rows)  # noqa: E731
    assert groups(clusters_v1_1(ads)) == groups(clusters(ads))


def test_the_default_method_is_v1_1(data_dir, capsys):
    from scrape_harness import store
    from scrape_harness.cli import main

    from listing_lens.commands import register  # noqa: F401  (the CLI below uses it)

    store.append(
        "derived", [ad_sub("e1", desc="click to view x@gmail.com"), ad_sub("e2", desc="contact y@gmail.com")]
    )
    capsys.readouterr()
    assert main(["derive-advertisers"]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["method"] == "v1.1" and out["advertisers"] == 2
    rows = list(store.read("advertisers"))
    assert all(r["advertiser_id"].startswith("v1.1:") for r in rows)
    assert main(["derive-advertisers", "--method", "v1"]) == 0  # the frozen record, its own file
    assert len(list(store.read("advertisers_v1"))) == 1 and len(list(store.read("advertisers"))) == 2
