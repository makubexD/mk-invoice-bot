"""Which sent invoices a reimbursement amount covers (R-7.12, D107).

:func:`match_sets` enumerates the sets of candidates whose claimed amounts add up to
the target, up to ``limit`` sets, oldest invoices first. The candidate and its claimed
amount (R-7.13) live in :mod:`invoicebot.store.payments`, which owns their definition;
they are re-exported here. The module is pure: it reads no clock and performs no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence

from invoicebot.store.payments import Candidate, claimed_cents

__all__ = ["Candidate", "claimed_cents", "match_sets"]


def match_sets(candidates: Sequence[Candidate], target_cents: int, limit: int = 3) -> list[tuple[int, ...]]:
    """Return up to ``limit`` invoice-id sets whose claimed amounts sum to the target (R-7.12, D107).

    Candidates are searched oldest first, and each set is a tuple of invoice ids in that
    order. The result is sorted by the oldest invoice's ``created_at``, then by set size.
    """
    if target_cents <= 0 or limit <= 0:
        return []
    eligible = tuple(
        sorted(
            (candidate for candidate in candidates if candidate.claimed_cents > 0),
            key=lambda candidate: (candidate.created_at, candidate.invoice_id),
        )
    )
    found = _search(eligible, target_cents, limit)
    oldest = {candidate.invoice_id: candidate.created_at for candidate in eligible}
    found.sort(key=lambda ids: (oldest[ids[0]], len(ids)))
    return found


def _search(candidates: tuple[Candidate, ...], target_cents: int, limit: int) -> list[tuple[int, ...]]:
    """Depth-first search for at most ``limit`` subsets summing to the target (R-7.12)."""
    suffix_sums = _suffix_sums(candidates)
    found: list[tuple[int, ...]] = []

    def walk(start: int, running: int, chosen: list[int]) -> None:
        if running == target_cents:
            found.append(tuple(chosen))
            return
        for index in range(start, len(candidates)):
            if len(found) >= limit:
                return
            if running + suffix_sums[index] < target_cents:
                return
            claimed = candidates[index].claimed_cents
            if running + claimed > target_cents:
                continue
            chosen.append(candidates[index].invoice_id)
            walk(index + 1, running + claimed, chosen)
            chosen.pop()

    walk(0, 0, [])
    return found


def _suffix_sums(candidates: tuple[Candidate, ...]) -> tuple[int, ...]:
    """Return the sum of ``claimed_cents`` from each index to the end, plus a zero (R-7.12)."""
    sums = [0] * (len(candidates) + 1)
    for index in range(len(candidates) - 1, -1, -1):
        sums[index] = sums[index + 1] + candidates[index].claimed_cents
    return tuple(sums)
