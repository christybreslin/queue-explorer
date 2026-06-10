"""Re-derive the Electra state cursors that the standard Beacon REST API
does NOT expose.

The Beacon API exposes the pending_* lists but not the small per-cursor fields:
  - state.earliest_exit_epoch
  - state.exit_balance_to_consume
  - state.earliest_consolidation_epoch
  - state.consolidation_balance_to_consume
  - state.deposit_balance_to_consume

Beaconcha.in (queues.go) re-derives these by walking the same data the REST
API does expose. This module reproduces that pattern. Every returned cursor
carries provenance="derived" so the methodology UI flags it as a re-emulation.
"""

from __future__ import annotations

from backend.spec import (
    SECONDS_PER_EPOCH,
    compute_activation_exit_epoch,
    get_activation_exit_churn_limit,
    get_consolidation_churn_limit,
)


def derive_exit_cursor(
    active_exiting_validators: list[dict],
    current_epoch: int,
    total_active_balance: int,
) -> dict:
    """Walk validators with status=active_exiting (each has exit_epoch already set)
    and re-derive (earliest_exit_epoch, exit_balance_to_consume).

    Logic mirrors how `compute_exit_epoch_and_update_churn` consumed state —
    the latest exit_epoch is the cursor; the budget remaining in that epoch is
    ae_churn minus the effective balances already booked into it.
    """
    ae_churn = get_activation_exit_churn_limit(total_active_balance)
    floor = compute_activation_exit_epoch(current_epoch)

    if not active_exiting_validators:
        return {
            "earliest_exit_epoch": floor,
            "exit_balance_to_consume_gwei": ae_churn,
            "provenance": "derived",
            "by_epoch": [],
        }

    # Group exiting validators by exit_epoch, sum effective_balance per epoch.
    by_epoch: dict[int, int] = {}
    for v in active_exiting_validators:
        exit_epoch = int(v["validator"]["exit_epoch"])
        eb = int(v["validator"]["effective_balance"])
        by_epoch[exit_epoch] = by_epoch.get(exit_epoch, 0) + eb

    earliest = max(by_epoch.keys())
    earliest = max(earliest, floor)

    # The budget remaining in `earliest` is ae_churn minus what's already booked into it.
    booked = by_epoch.get(earliest, 0)
    remaining = max(0, ae_churn - booked)

    # If `earliest` was bumped to floor (queue ahead of cursor) the remaining is fresh ae_churn.
    if floor > max(by_epoch.keys()):
        remaining = ae_churn

    return {
        "earliest_exit_epoch": earliest,
        "exit_balance_to_consume_gwei": remaining,
        "ae_churn_gwei": ae_churn,
        "provenance": "derived",
        "by_epoch": sorted(by_epoch.items()),
    }


def derive_consolidation_cursor(
    pending_consolidations: list[dict],
    current_epoch: int,
    total_active_balance: int,
) -> dict:
    """Approximate (earliest_consolidation_epoch, consolidation_balance_to_consume).

    Unlike exits, the on-chain consolidation cursor lives on the source-validator
    side. We don't have a clean way to recover it without diffing two states.
    For the methodology view we report the floor + full cons_churn as the working
    estimate, and surface this approximation in the trace's notes.
    """
    cons_churn = get_consolidation_churn_limit(total_active_balance)
    floor = compute_activation_exit_epoch(current_epoch)
    return {
        "earliest_consolidation_epoch": floor,
        "consolidation_balance_to_consume_gwei": cons_churn,
        "cons_churn_gwei": cons_churn,
        "provenance": "derived-approx",
        "pending_count": len(pending_consolidations),
    }


def derive_deposit_carry(
    pending_aggregate: dict,
    ae_churn_gwei: int,
) -> dict:
    """Approximate state.deposit_balance_to_consume.

    Under saturated mainnet conditions (queue >> cap, head deposits ≤ 32 ETH),
    this is 0 at every epoch boundary — the cap fires cleanly. We surface it
    as 0 and label as derived-approx; max error ≤ ae_churn (one epoch ≈ 6 min).
    """
    return {
        "deposit_balance_to_consume_gwei": 0,
        "provenance": "derived-approx",
        "max_error_gwei": ae_churn_gwei,
        "max_error_epochs": 1,
    }
