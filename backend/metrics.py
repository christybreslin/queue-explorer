"""State-addressable metric assembly.

Each ``compute_*`` function reproduces exactly what a home-tab endpoint returns,
but for an arbitrary ``state_id`` (``"head"`` for live, a slot string for a
historical snapshot). The live endpoints in ``main.py`` are thin wrappers over
these, and ``snapshot.py`` calls them at past slots — so the historical figures
can never drift from what the dashboard shows now.

The heaviest beacon call is the full active-validator pull
(``get_active_validator_summary`` / ``get_total_active_balance``). At ``"head"``
it is cached, but historical states bypass the cache, so a naive snapshot would
pull the full set up to three times. To keep backfill at one heavy pull per day,
each function accepts an optional pre-fetched ``summary`` / ``total_active_balance``
that the collector fetches once and injects; the live endpoints pass nothing and
behave identically to before.
"""

import asyncio

from backend import beacon, churn, cursors, models, spec

EPOCHS_PER_DAY = 86400 / spec.SECONDS_PER_EPOCH


async def compute_network_stats(
    state_id: str,
    current_epoch: int,
    *,
    summary: dict | None = None,
) -> dict:
    """High-level network overview — mirrors GET /network/stats."""
    if summary is None:
        summary, consolidations = await asyncio.gather(
            beacon.get_active_validator_summary(state_id),
            beacon.get_pending_consolidations(state_id),
        )
    else:
        consolidations = await beacon.get_pending_consolidations(state_id)

    ae_churn_gwei = spec.get_activation_exit_churn_limit(summary["total_balance_gwei"])

    # Distinct targets give a clearer picture of how many validators are
    # actually involved than the raw pending_consolidations length.
    pc_target_indices = {
        int(c["target_index"])
        for c in consolidations
        if c.get("target_index") is not None
    }

    return {
        "current_epoch": current_epoch,
        "active_validators": summary["count"],
        "total_stake_gwei": summary["total_balance_gwei"],
        "total_stake_eth": round(summary["total_balance_gwei"] / spec.GWEI, 4),
        "compounding_count": summary["compounding_count"],
        "compounding_share": (summary["compounding_count"] / summary["count"]) if summary["count"] else 0.0,
        "ae_churn_eth": ae_churn_gwei / spec.GWEI,
        "ae_churn_per_day_eth": (ae_churn_gwei / spec.GWEI) * EPOCHS_PER_DAY,
        "pending_consolidations": len(consolidations),
        "pending_consolidation_targets": len(pc_target_indices),
    }


async def compute_exit_queue(
    state_id: str,
    current_epoch: int,
    *,
    total_active_balance: int | None = None,
) -> models.ExitQueueResponse:
    """Spec-faithful exit queue summary — mirrors GET /exit-queue.

    queue_depth_epochs / estimated_wait_hours measure epochs until
    state.earliest_exit_epoch (the cursor): how long a new exit submitted at
    this state would wait before its exit_epoch can be assigned.
    """
    if total_active_balance is None:
        exiting, total_active_balance = await asyncio.gather(
            beacon.get_active_exiting_validators(state_id),
            beacon.get_total_active_balance(state_id),
        )
    else:
        exiting = await beacon.get_active_exiting_validators(state_id)

    ae_churn_gwei = spec.get_activation_exit_churn_limit(total_active_balance)
    cursor = cursors.derive_exit_cursor(exiting, current_epoch, total_active_balance)

    queue = churn.build_exit_queue(exiting)
    total_balance = sum(info["balance"] for info in queue.values())
    total_count = sum(info["count"] for info in queue.values())
    per_epoch = [
        models.EpochBreakdown(
            epochs_from_now=epoch - current_epoch,
            validator_count=info["count"],
            total_balance_gwei=info["balance"],
            total_balance_eth=round(info["balance"] / spec.GWEI, 4),
        )
        for epoch, info in queue.items()
    ]

    queue_depth = max(0, cursor["earliest_exit_epoch"] - current_epoch)
    estimated_hours = queue_depth * spec.SECONDS_PER_EPOCH / 3600

    return models.ExitQueueResponse(
        current_epoch=current_epoch,
        churn_limit_gwei=ae_churn_gwei,
        total_exiting_validators=total_count,
        total_exiting_balance_gwei=total_balance,
        total_exiting_balance_eth=round(total_balance / spec.GWEI, 4),
        queue_depth_epochs=queue_depth,
        estimated_wait_hours=round(estimated_hours, 2),
        per_epoch=per_epoch,
    )


async def compute_entry_queue(
    state_id: str,
    current_epoch: int,
    *,
    total_active_balance: int | None = None,
) -> dict:
    """Lightweight entry-queue summary — mirrors GET /entry-queue."""
    pending_agg = await beacon.get_pending_deposits_aggregate(state_id)
    if total_active_balance is None:
        total_active_balance = await beacon.get_total_active_balance(state_id)

    ae_churn_gwei = spec.get_activation_exit_churn_limit(total_active_balance)
    ae_churn_eth = ae_churn_gwei / spec.GWEI

    finalized_count = pending_agg.get("finalized_count", 0)
    finalized_eth = pending_agg.get("finalized_eth", 0.0)
    pending_count = pending_agg.get("count", 0)
    pending_eth = pending_agg.get("total_eth", 0.0)

    # Conservative drain estimate: total finalised ETH / per-day churn. Ignores
    # the per-epoch deposit-count cap (16); under-states only when the head is
    # full of tiny deposits, which is rare.
    churn_per_day_eth = ae_churn_eth * EPOCHS_PER_DAY
    drain_days = (finalized_eth / churn_per_day_eth) if churn_per_day_eth > 0 else 0.0
    drain_seconds = drain_days * 86_400

    severity = (
        "clear" if drain_days < 1
        else "short" if drain_days < 7
        else "moderate" if drain_days < 30
        else "congested"
    )

    return {
        "current_epoch": current_epoch,
        "pending_count": pending_count,
        "pending_eth": pending_eth,
        "finalized_count": finalized_count,
        "finalized_eth": finalized_eth,
        "ae_churn_eth": ae_churn_eth,
        "drain_days": drain_days,
        "drain_seconds": drain_seconds,
        "severity": severity,
    }


async def compute_partials(state_id: str) -> models.PendingPartialWithdrawalsResponse:
    """Pending partial withdrawals — mirrors GET /pending-partial-withdrawals."""
    withdrawals = await beacon.get_pending_partial_withdrawals(state_id)

    FAR_FUTURE = "18446744073709551615"
    parsed = []
    for w in withdrawals:
        we = w.get("withdrawable_epoch", "0")
        if we == FAR_FUTURE:
            continue
        epoch = int(we)
        amount = int(w["amount"])
        parsed.append(models.PendingPartialWithdrawal(
            validator_index=str(w.get("validator_index", w.get("index", ""))),
            amount_gwei=amount,
            amount_eth=round(amount / churn.GWEI, 4),
            withdrawable_epoch=epoch,
            withdrawable_time=churn.epoch_to_datetime(epoch).isoformat(),
        ))
    total_amount = sum(w.amount_gwei for w in parsed)

    return models.PendingPartialWithdrawalsResponse(
        count=len(parsed),
        total_amount_gwei=total_amount,
        total_amount_eth=round(total_amount / churn.GWEI, 4),
        withdrawals=parsed,
    )
