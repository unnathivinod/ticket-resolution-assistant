"""Fast tests for new-class discovery: grouping similar complaints into proposals."""

import math

from services.ingestion.discovery import build_proposals, group_similar, top_keywords


def unit(*values: float) -> list[float]:
    length = math.sqrt(sum(v * v for v in values))
    return [v / length for v in values]


# Three directions in a tiny 3-number "meaning space": eSIM, fraud, and one odd complaint.
ESIM = [unit(1, 0.05, 0), unit(1, 0.1, 0), unit(1, 0, 0.05)]
FRAUD = [unit(0, 1, 0.05), unit(0.05, 1, 0)]
ODD = [unit(0, 0, 1)]


def test_similar_complaints_end_up_in_the_same_group():
    groups = group_similar(ESIM + FRAUD + ODD, threshold=0.9)
    assert groups == [[0, 1, 2], [3, 4], [5]]  # largest group first


def test_a_stricter_threshold_splits_groups_and_a_looser_one_merges_them():
    vectors = ESIM + FRAUD + ODD
    assert len(group_similar(vectors, threshold=0.9999)) > 3
    assert len(group_similar(vectors, threshold=-1.0)) == 1
    assert group_similar([], threshold=0.8) == []


def test_groups_are_chained_through_shared_neighbours():
    # a is close to b, b is close to c, a is not close to c: all three still form one group.
    a, b, c = unit(1, 0, 0), unit(1, 1, 0), unit(0, 1, 0)
    assert group_similar([a, b, c], threshold=0.7) == [[0, 1, 2]]


def test_keywords_ignore_filler_words_and_prefer_words_most_complaints_share():
    texts = [
        "Hello team, my eSIM QR code will not scan. Please help.",
        "The eSIM QR code is not working since yesterday, thanks.",
        "Hi, I cannot activate the eSIM on my new handset.",
    ]
    keywords = top_keywords(texts, limit=3)
    assert keywords[0] == "esim"
    assert {"code"} <= set(keywords)
    assert not {"hello", "please", "thanks", "the", "team"} & set(keywords)


def test_keywords_prefer_words_that_set_the_group_apart():
    group = ["My manager is furious, the eSIM will not activate", "eSIM transfer failed, manager is waiting"]
    everything = group + ["My manager is furious about the bill", "The manager wants the router replaced"]
    assert top_keywords(group, limit=1) in (["esim"], ["manager"])  # a tie without more context
    assert top_keywords(group, limit=1, background=everything) == ["esim"]  # "manager" is everywhere


def test_only_groups_that_are_big_enough_become_proposals():
    items = [
        {"id": f"r{n}", "text": text}
        for n, text in enumerate(
            ["eSIM QR code will not scan"] * 3
            + ["Text message asking for my PIN"] * 2
            + ["What is the weather?"]
        )
    ]
    proposals = build_proposals(items, ESIM + FRAUD + ODD, threshold=0.9, min_size=3)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.size == 3 and proposal.member_ids == ["r0", "r1", "r2"]
    assert proposal.suggested_name.startswith("code_esim") or "esim" in proposal.suggested_name
    assert proposal.examples[0] == "eSIM QR code will not scan"

    assert len(build_proposals(items, ESIM + FRAUD + ODD, threshold=0.9, min_size=2)) == 2
