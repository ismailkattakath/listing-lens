"""`llens services` on SYNTHETIC listings and the synthetic SKOS fixture (made-up concepts alpha/beta only)."""

import hashlib
import json
import shutil

import pytest
from conftest import EXAMPLES, FIXTURES
from scrape_harness.cli import main

TITLE = "synthetic: alpha service today"
DESC = "Synthetic body.\nno beta thing"
EVAL_AD, DISCOVERY_AD = "1000001", "1000002"  # synthetic ids
EMPTY_VOCAB = (
    "@prefix skos: <http://www.w3.org/2004/02/skos/core#> . <urn:li:vocab:services:> a skos:ConceptScheme .\n"
)


def seed(data, vocab=True, freeze=True):
    """Two synthetic ads; the vocabulary; by default a VALID eval freeze (gold hash matches)."""
    r = json.loads((EXAMPLES / "listing.example.json").read_text())
    r["content"]["title"], r["content"]["description"] = TITLE, DESC
    r["site"]["sample"]["services"] = [{"code": "alpha", "label": "Alpha"}, {"code": None, "label": "x"}]
    (data / "derived").mkdir(parents=True)
    rows = [{**r, "ad_id": a} for a in (EVAL_AD, DISCOVERY_AD)]
    (data / "derived" / "listings.latest.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    (data / "vocab").mkdir()
    (data / "eval").mkdir()
    if vocab:
        shutil.copy(FIXTURES / "services.synthetic.ttl", data / "vocab" / "services.ttl")
    if freeze:
        gold = data / "eval" / "services-gold.jsonl"
        gold.write_text('{"ad_id": "1000001", "synthetic": true}\n')
        (data / "eval" / "services-gold.freeze.json").write_text(
            json.dumps(
                {
                    "schema": "eval-freeze.v1",
                    "labels_path": "eval/services-gold.jsonl",
                    "labels_sha256": hashlib.sha256(gold.read_bytes()).hexdigest(),
                    "n_ads": 1,
                    "frozen_at": "2026-01-01T00:00:00Z",
                    "frozen_by": "synthetic",
                }
            )
        )
    cluster(data)  # a current v2 clustering: every run with concepts reads it (eval_exclusion)
    return r


def split(data, extra_ad=None):
    """The persona-keyed split (the analyst's layout), synthetic: one eval persona, one discovery persona."""
    personas = {
        "p-eval": {"half": "eval", "subcategory": "sample-list-a", "ad_ids": [EVAL_AD]},
        "p-disc": {"half": "discovery", "subcategory": "sample-list-a", "ad_ids": [DISCOVERY_AD]},
    }
    (data / "eval" / "services-persona-split.json").write_text(
        json.dumps({"seed": "synthetic", "personas": personas})
    )


def cluster(data, operators=None):
    """A synthetic derived/advertisers_v2.jsonl: operators = {operator_id: [ad_id, ...]}; by default every
    derived ad is its own operator. Written after derived, so it is current."""
    ads = [
        json.loads(x)["ad_id"] for x in (data / "derived" / "listings.latest.jsonl").read_text().splitlines()
    ]
    operators = operators or {f"op-{a}": [a] for a in ads}
    rows = [{"ad_id": a, "operator_id": op} for op, ids in operators.items() for a in ids]
    (data / "derived" / "advertisers_v2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def add_ad(data, ad_id):
    """Append a synthetic ad (a copy of the first, new id) to derived: a newly processed recruit."""
    rows = (data / "derived" / "listings.latest.jsonl").read_text().splitlines()
    new = {**json.loads(rows[0]), "ad_id": ad_id}
    (data / "derived" / "listings.latest.jsonl").write_text("\n".join([*rows, json.dumps(new)]) + "\n")


def records(data):
    return [json.loads(x) for x in (data / "derived" / "services.latest.jsonl").read_text().splitlines()]


def summary(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_a_missing_vocabulary_exits_2_and_writes_nothing(data_dir, capsys):
    seed(data_dir, vocab=False)
    assert main(["services"]) == 2
    assert "vocabulary not found" in capsys.readouterr().out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


def test_one_record_per_ad_with_fields_offsets_and_the_page_codes(data_dir, capsys):
    r = seed(data_dir)
    split(data_dir)
    assert main(["services"]) == 0
    s = summary(capsys)
    assert (s["ads"], s["offered"], s["refused"], s["eval_gate"]) == (2, 2, 2, "frozen")
    rec = records(data_dir)[0]
    assert (rec["schema"], rec["fetched_at"]) == ("services.v1", r["fetched_at"])
    assert rec["parser"] == r["derived"]["method"] and rec["vocab_version"] == "0.0.0-test"
    assert rec["online_services_offered"] == ["alpha"]  # a null code is not a code
    got = {(m["concept"], m["field"], m["stance"]) for m in rec["mentions"]}
    assert got == {("alpha", "title", "offered"), ("beta", "description", "refused")}
    for m in rec["mentions"]:  # offsets index the ORIGINAL field text
        text = TITLE if m["field"] == "title" else DESC
        assert text[m["start"] : m["end"]] == m["label_matched"]


def test_vocab_flag_overrides_the_default_path(data_dir, tmp_path):
    seed(data_dir, vocab=False)
    split(data_dir)
    other = tmp_path / "elsewhere.ttl"
    shutil.copy(FIXTURES / "services.synthetic.ttl", other)
    assert main(["services", "--vocab", str(other)]) == 0
    assert len(records(data_dir)) == 2


# ── The blind-eval gate: one test per branch ──────────────────────────────────────────────────────


def test_gate_an_empty_vocabulary_runs_on_every_ad_without_a_freeze(data_dir, capsys):
    seed(data_dir, vocab=False, freeze=False)
    (data_dir / "vocab" / "services.ttl").write_text(EMPTY_VOCAB)
    assert main(["services"]) == 0
    s = summary(capsys)
    assert (s["eval_gate"], s["eval_excluded"], s["ads"]) == ("empty-vocab", False, 2)


def test_gate_a_valid_freeze_runs_on_every_ad_including_the_eval_pool(data_dir, capsys):
    seed(data_dir)
    split(data_dir)  # present, but a valid freeze means nothing is excluded
    assert main(["services"]) == 0
    assert summary(capsys)["eval_excluded"] is False
    assert {x["ad_id"] for x in records(data_dir)} == {EVAL_AD, DISCOVERY_AD}


def test_gate_no_freeze_excludes_the_eval_ads_and_says_so(data_dir, capsys):
    seed(data_dir, freeze=False)
    split(data_dir)
    cluster(data_dir)
    assert main(["services"]) == 0
    cap = capsys.readouterr()
    s = json.loads(cap.out.splitlines()[-1])
    assert (s["eval_gate"], s["eval_excluded"], s["eval_ads_excluded"], s["ads"]) == ("unfrozen", True, 1, 1)
    assert s["eval_excluded_by_advertiser"] == 0
    assert "1 blind-eval ads + 0 of their v2-operator mates EXCLUDED" in " ".join(
        cap.err.split()
    )  # the run card (stderr) says it too
    assert {x["ad_id"] for x in records(data_dir)} == {DISCOVERY_AD}  # the eval ad is ABSENT
    on_disk = json.loads((data_dir / "derived" / "services.summary.json").read_text())
    assert on_disk["eval_excluded"] is True and on_disk["eval_ads_excluded"] == 1


@pytest.mark.parametrize("tamper", ["edit-gold", "delete-gold", "edit-split"])
def test_gate_a_freeze_whose_gold_hash_mismatches_refuses(data_dir, capsys, tamper):
    seed(data_dir)
    split(data_dir)  # an exclusion route exists, yet a broken freeze still refuses outright
    gold = data_dir / "eval" / "services-gold.jsonl"
    if tamper == "edit-gold":
        gold.write_text(gold.read_text() + '{"ad_id": "x"}\n')
    elif tamper == "delete-gold":
        gold.unlink()
    else:  # the marker recorded the split's hash; the split changed after the freeze
        mk = data_dir / "eval" / "services-gold.freeze.json"
        sp = data_dir / "eval" / "services-persona-split.json"
        mk.write_text(
            json.dumps(
                {
                    **json.loads(mk.read_text()),
                    "persona_split_sha256": hashlib.sha256(sp.read_bytes()).hexdigest(),
                }
            )
        )
        sp.write_text(sp.read_text().replace("synthetic", "changed"))
    assert main(["services"]) == 2
    assert "blind-eval gate" in capsys.readouterr().out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


def test_gate_no_freeze_and_no_split_refuses(data_dir, capsys):
    seed(data_dir, freeze=False)
    assert main(["services"]) == 2
    assert "blind-eval gate" in capsys.readouterr().out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


def test_gate_an_ad_the_split_does_not_list_is_not_excluded(data_dir, capsys):
    """A newly processed ad (absent from the split) was never in the eval pool: it stays."""
    seed(data_dir, freeze=False)
    split(data_dir)
    add_ad(data_dir, "1000003")
    cluster(data_dir)  # its own operator: no eval advertiser
    assert main(["services"]) == 0
    assert {x["ad_id"] for x in records(data_dir)} == {DISCOVERY_AD, "1000003"}


@pytest.mark.parametrize(
    "body",
    [
        '{"eval": ["1000001"], "discovery": ["1000002"]}',  # the old flat layout: unrecognised
        '{"seed": "s", "personas": {"p": {"half": "test", "ad_ids": ["1000001"]}}}',  # unknown half
        '{"seed": "s", "personas": {"p": {"half": "eval", "ad_ids": "1000001"}}}',  # ad_ids not a list
        '{"seed": "s", "personas": {}}',
        "not json",
    ],
)
def test_gate_an_unrecognised_split_refuses_rather_than_excluding_nothing(data_dir, capsys, body):
    seed(data_dir, freeze=False)
    (data_dir / "eval" / "services-persona-split.json").write_text(body)
    assert main(["services"]) == 2
    assert "unrecognised persona-split layout" in capsys.readouterr().out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


def test_gate_an_empty_eval_half_refuses(data_dir, capsys):
    seed(data_dir, freeze=False)
    only_disc = {
        "seed": "s",
        "personas": {"p": {"half": "discovery", "subcategory": "x", "ad_ids": [DISCOVERY_AD]}},
    }
    (data_dir / "eval" / "services-persona-split.json").write_text(json.dumps(only_disc))
    assert main(["services"]) == 2
    assert "eval half lists no ads" in capsys.readouterr().out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


# ── operator-level exclusion: a new recruit can join an eval advertiser (post-run rehearsal, 2026-11-08) ──


def test_gate_excludes_a_new_ad_sharing_a_v2_operator_with_an_eval_ad(data_dir, capsys):
    seed(data_dir, freeze=False)
    split(data_dir)
    add_ad(data_dir, "1000003")  # unlisted in the split ...
    cluster(
        data_dir, {"op-e": [EVAL_AD, "1000003"], "op-d": [DISCOVERY_AD]}
    )  # ... but the eval ad's operator
    assert main(["services"]) == 0
    cap = capsys.readouterr()
    s = json.loads(cap.out.splitlines()[-1])
    assert (s["eval_ads_excluded"], s["eval_excluded_by_advertiser"], s["ads"]) == (1, 1, 1)
    assert {x["ad_id"] for x in records(data_dir)} == {DISCOVERY_AD}  # the operator-mate is ABSENT
    assert "operator mates EXCLUDED" in " ".join(cap.err.split())
    on_disk = json.loads((data_dir / "derived" / "services.summary.json").read_text())
    assert on_disk["eval_excluded_by_advertiser"] == 1


@pytest.mark.parametrize("how", ["missing", "lacks-an-ad", "older-than-derived"])
def test_gate_a_missing_or_stale_clustering_refuses(data_dir, capsys, how):
    import os

    seed(data_dir, freeze=False)
    split(data_dir)
    if how == "missing":
        (data_dir / "derived" / "advertisers_v2.jsonl").unlink()
    elif how == "lacks-an-ad":  # clustered before a new ad was derived: it could be an eval-operator mate
        cluster(data_dir)
        add_ad(data_dir, "1000003")
    elif how == "older-than-derived":
        cluster(data_dir)
        v2, der = (
            data_dir / "derived" / "advertisers_v2.jsonl",
            data_dir / "derived" / "listings.latest.jsonl",
        )
        t = der.stat().st_mtime
        os.utime(v2, (t - 60, t - 60))
    assert main(["services"]) == 2
    out = capsys.readouterr().out
    assert "blind-eval gate" in out and "advertisers_v2" in out
    assert not (data_dir / "derived" / "services.latest.jsonl").exists()


# ── analyst rule R1: eval_adjacent on every row, the full (frozen) run included ──────────────────


def test_r1_a_frozen_full_run_flags_eval_and_operator_adjacent_ads_and_not_a_clean_one(data_dir, capsys):
    seed(data_dir)  # a VALID freeze: a full run, nothing excluded
    split(data_dir)
    add_ad(data_dir, "1000003")  # unlisted, shares the eval ad's operator
    add_ad(data_dir, "1000004")  # unlisted, its own operator
    cluster(data_dir, {"op-e": [EVAL_AD, "1000003"], "op-d": [DISCOVERY_AD], "op-x": ["1000004"]})
    assert main(["services"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["eval_excluded"] is False
    flags = {x["ad_id"]: x["eval_adjacent"] for x in records(data_dir)}
    assert flags == {EVAL_AD: True, "1000003": True, DISCOVERY_AD: False, "1000004": False}


def test_r1_an_unfrozen_run_keeps_only_non_adjacent_ads(data_dir, capsys):
    seed(data_dir, freeze=False)
    split(data_dir)
    add_ad(data_dir, "1000003")
    cluster(data_dir, {"op-e": [EVAL_AD, "1000003"], "op-d": [DISCOVERY_AD]})
    assert main(["services"]) == 0
    assert {x["ad_id"]: x["eval_adjacent"] for x in records(data_dir)} == {DISCOVERY_AD: False}


def test_r1_a_frozen_run_with_no_current_clustering_refuses(data_dir, capsys):
    """The flag cannot be computed honestly without current operators: refuse rather than write false."""
    seed(data_dir)
    split(data_dir)
    (data_dir / "derived" / "advertisers_v2.jsonl").unlink()
    assert main(["services"]) == 2
    assert "advertisers_v2" in capsys.readouterr().out
