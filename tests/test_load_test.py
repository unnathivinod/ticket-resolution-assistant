"""Fast tests for the arithmetic of the load test (scripts/load_test.py). No services needed."""

from scripts.load_test import percentile, reading, summarise


def sample(seconds, status=200, triage=40, retrieval=90):
    return {"status": status, "seconds": seconds, "triage_ms": triage, "retrieval_ms": retrieval}


def test_percentile_picks_the_middle_and_the_slow_end():
    times = [0.1 * n for n in range(1, 21)]  # 0.1 s to 2.0 s
    assert round(percentile(times, 0.5), 1) == 1.0
    assert round(percentile(times, 0.95), 1) == 1.9
    assert percentile([0.3], 0.95) == 0.3
    assert percentile([], 0.5) == 0.0


def test_a_step_is_summarised_from_the_normal_answers_only():
    samples = [sample(0.2), sample(0.4), sample(0.6), sample(5.0, status=503), sample(0.01, status=429)]
    row = summarise(users=5, seconds=10, samples=samples)
    assert row["requests"] == 5 and row["errors"] == 2 and row["rate_limited"] == 1
    assert row["per_second"] == 0.3  # three normal answers in ten seconds
    assert row["p50_ms"] == 400 and row["slowest_ms"] == 600  # the failed requests do not count as fast
    assert row["triage_p50_ms"] == 40 and row["retrieval_p50_ms"] == 90


def test_a_step_with_no_answers_does_not_crash():
    row = summarise(users=3, seconds=10, samples=[sample(1.0, status=0)])
    assert row["per_second"] == 0.0 and row["p95_ms"] == 0 and row["errors"] == 1
    assert summarise(users=1, seconds=0, samples=[])["requests"] == 0


def test_the_reading_says_where_throughput_levelled_off():
    def step(users, per_second, errors=0):
        return {"users": users, "requests": 100, "per_second": per_second, "errors": errors}

    levelled = reading([step(1, 5.0), step(5, 20.0), step(10, 24.0), step(20, 23.5)])
    assert "levelled off between 23.5 and 24.0 requests per second, from 10 users on" in levelled
    # the real shape seen on a laptop: flat from 5 users, with the best number at the last step
    flat = reading([step(1, 3.8), step(5, 8.1), step(10, 8.2), step(20, 9.2)])
    assert "between 8.1 and 9.2 requests per second, from 5 users on" in flat
    growing = reading([step(1, 5.0), step(5, 20.0), step(10, 38.0)])
    assert "still growing at 10 users" in growing and "more users" in growing
    assert "Not enough clean steps" in reading([step(1, 5.0)])
    # a step with errors is not used as evidence of capacity
    assert "at 5 users" in reading([step(1, 5.0), step(5, 20.0), step(10, 60.0, errors=40)])
