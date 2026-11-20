"""Price correlated omissions and refine candidate worker schedules."""

from bisect import bisect_left
from collections import defaultdict
from math import floor, prod

MISS_COST_MS = 300_000
WORKER_COST_MS = 2_000
OBSERVED_CALL_RISK = 0.90
COVERAGE_GAP_RISK = 0.01
FILE_GAP_RISK = 0.005
MAX_CANDIDATES = 8


def omission_risk(omitted, scenarios):
    """Each latent cause charges once if it can fail any omitted test."""
    return 1 - prod(1 - probability for probability, group in scenarios if not omitted.isdisjoint(group))


def worker_cost(workers, durations):
    return max((sum(durations[test] for test in worker) for worker in workers), default=0) + (
        WORKER_COST_MS * len(workers)
    )


def rebalance(workers, durations):
    """Improve the LPT schedule with bounded moves/swaps between extreme loads."""
    workers = [worker.copy() for worker in workers]
    if len(workers) < 2:
        return workers
    loads = [sum(durations[test] for test in worker) for worker in workers]
    for _ in range(6):
        light = loads.index(min(loads))
        heavy = loads.index(max(loads))
        gap = loads[heavy] - loads[light]
        if gap < 2:
            break
        target = gap // 2
        # A swap is useful only when its net transfer lies strictly inside
        # the load gap. Search nearest to half the gap for each potential move.
        smaller = sorted((durations[test], test) for test in workers[light])
        if len(workers[heavy]) > 1:
            smaller.insert(0, (0, -1))  # Also consider moving without a swap.
        smaller_times = [duration for duration, _ in smaller]
        best = None
        for test in workers[heavy]:
            duration = durations[test]
            pos = bisect_left(smaller_times, duration - target)
            for other_duration, other in smaller[max(pos - 1, 0):pos + 1]:
                transfer = duration - other_duration
                if 0 < transfer < gap:
                    candidate = (abs(gap - 2 * transfer), test, other, transfer)
                    if best is None or candidate < best:
                        best = candidate
        if best is None:
            break
        _, test, other, transfer = best
        workers[heavy].remove(test)
        workers[light].append(test)
        if other != -1:
            workers[light].remove(other)
            workers[heavy].append(other)
        loads[light] += transfer
        loads[heavy] -= transfer
    return workers


def plan(tests, required_ids, observed_groups, schedule):
    """Compare schedules under a latent-cause risk model and fixed penalty."""
    durations = {test["number"]: floor(test["seconds"] * 1000 + 0.5) for test in tests}
    by_id = {test["id"]: test["number"] for test in tests}
    required = required_ids.intersection(by_id)
    observed = dict.fromkeys(
        frozenset(by_id[key] for key in group if key in by_id) for group in observed_groups
    )
    observed_numbers = set().union(*observed)
    optional_by_file = defaultdict(set)
    for test in tests:
        if test["id"] not in required:
            optional_by_file[test["file"]].add(test["number"])

    uncertain = set().union(*optional_by_file.values()) - observed_numbers
    scenarios = [(OBSERVED_CALL_RISK, group) for group in observed if group]
    if uncertain:
        scenarios.append((COVERAGE_GAP_RISK, uncertain))
        for file_group in optional_by_file.values():
            if group := file_group & uncertain:
                scenarios.append((FILE_GAP_RISK, group))

    # Keep the individual marginal distinct from the joint omission risk.
    survival = defaultdict(lambda: 1.0)
    for probability, group in scenarios:
        for number in group:
            survival[number] *= 1 - probability

    # Dropping a test from an observed group costs at least 270 seconds. That
    # exceeds the entire per-commit schedule for the provided test inventory,
    # but keep the comparison valid for longer inventories too.
    full = rebalance(schedule(tests), durations)
    upper_bound = worker_cost(full, durations)
    blocks = []
    for file_group in optional_by_file.values():
        individually_eligible = {
            number for number in file_group
            if MISS_COST_MS * (1 - survival[number]) < upper_bound
        }
        for group in (individually_eligible & observed_numbers, individually_eligible - observed_numbers):
            if not group:
                continue
            risk = omission_risk(group, scenarios)
            if risk > 0 and MISS_COST_MS * risk >= upper_bound:
                continue
            duration = sum(durations[test] for test in group)
            blocks.append((risk / max(duration, 1), min(group), group))
    blocks.sort()

    # Examine bounded prefixes of low-risk, expensive groups. For each prefix
    # re-run the worker-count search because the cheapest count can change.
    best_score = upper_bound
    best = full
    omitted = set()
    last_index = 0
    for index, (_, _, group) in enumerate(blocks, start=1):
        omitted.update(group)
        if index < len(blocks) and index * MAX_CANDIDATES // len(blocks) == last_index:
            continue
        last_index = index * MAX_CANDIDATES // len(blocks)
        penalty = MISS_COST_MS * omission_risk(omitted, scenarios)
        if penalty >= best_score:
            break
        selected = [test for test in tests if test["number"] not in omitted]
        workers = rebalance(schedule(selected), durations)
        score = worker_cost(workers, durations) + penalty
        if score < best_score:
            best_score, best = score, workers
    return best
