"""Daily snapshot collector + backfill.

Collects one row per UTC date of every scalar metric the dashboard shows, by
running the shared ``metrics.compute_*`` functions against that day's finalized
slot and storing the result via ``history_db``.

The full active-validator pull dominates cost, so each snapshot fetches that set
exactly once and injects it into the compute functions (historical states bypass
the head cache). Backfill is sequential, resumable (skips dates already stored),
and gentle on the RPC.

CLI:
    python -m api.snapshot backfill [--from YYYY-MM-DD] [--days N]
    python -m api.snapshot catch-up
"""

import argparse
import asyncio
from datetime import date, datetime, time, timedelta, timezone

from backend import beacon, churn, history_db, metrics

# Electra/Pectra mainnet activation (config_fork_schedule: version 0x05000000).
PECTRA_EPOCH = 364032
PECTRA_SLOT = PECTRA_EPOCH * churn.SLOTS_PER_EPOCH

# Snapshot the slot at noon UTC — safely inside the day and clear of fork/epoch
# boundaries, so a day's figure is representative of that date.
SNAPSHOT_HOUR_UTC = 12

# Be gentle on the archive RPC between heavy historical pulls.
INTER_SNAPSHOT_DELAY = 0.5

# catch_up() self-heals only a recent window. A larger gap means the full
# historical backfill hasn't run — that is a deliberate one-time CLI job, not
# something to kick off automatically on app startup.
MAX_CATCHUP_DAYS = 14


def slot_for_utc_date(d: date) -> int:
    """First slot of the epoch containing noon UTC on date ``d``."""
    ts = datetime.combine(d, time(SNAPSHOT_HOUR_UTC, tzinfo=timezone.utc)).timestamp()
    epoch = int((ts - churn.GENESIS_TIME) // churn.SECONDS_PER_EPOCH)
    return epoch * churn.SLOTS_PER_EPOCH


def pectra_start_date() -> date:
    """The earliest UTC date whose noon slot is at/after Pectra activation."""
    d = datetime.fromtimestamp(
        churn.epoch_to_timestamp(PECTRA_EPOCH), tz=timezone.utc
    ).date()
    # If noon on the activation day still lands pre-fork, start the next day.
    return d if slot_for_utc_date(d) >= PECTRA_SLOT else d + timedelta(days=1)


def _flatten(d: date, slot: int, epoch: int, ns: dict, eq, enq: dict, pw) -> dict:
    """Map the compute_* outputs onto the history_db column set."""
    return {
        "date": d.isoformat(),
        "epoch": epoch,
        "slot": slot,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # network
        "active_validators": ns["active_validators"],
        "total_stake_gwei": ns["total_stake_gwei"],
        "compounding_count": ns["compounding_count"],
        "compounding_share": ns["compounding_share"],
        "pending_consolidations": ns["pending_consolidations"],
        "pending_consolidation_targets": ns["pending_consolidation_targets"],
        # exit queue
        "exit_count": eq.total_exiting_validators,
        "exit_balance_gwei": eq.total_exiting_balance_gwei,
        "exit_queue_depth_epochs": eq.queue_depth_epochs,
        "exit_wait_hours": eq.estimated_wait_hours,
        "churn_limit_gwei": eq.churn_limit_gwei,
        # entry queue
        "entry_pending_count": enq["pending_count"],
        "entry_pending_eth": enq["pending_eth"],
        "entry_finalized_count": enq["finalized_count"],
        "entry_finalized_eth": enq["finalized_eth"],
        "entry_drain_days": enq["drain_days"],
        "entry_severity": enq["severity"],
        # partial withdrawals
        "partial_count": pw.count,
        "partial_total_gwei": pw.total_amount_gwei,
    }


async def collect(slot: int, epoch: int, d: date) -> dict:
    """Fetch every metric at ``slot`` and return a flat snapshot row.

    The full validator set is pulled once and injected so the four compute
    functions don't each re-fetch it (no head cache at historical states)."""
    state_id = str(slot)
    summary = await beacon.get_active_validator_summary(state_id)
    tab = summary["total_balance_gwei"]
    ns, eq, enq, pw = await asyncio.gather(
        metrics.compute_network_stats(state_id, epoch, summary=summary),
        metrics.compute_exit_queue(state_id, epoch, total_active_balance=tab),
        metrics.compute_entry_queue(state_id, epoch, total_active_balance=tab),
        metrics.compute_partials(state_id),
    )
    return _flatten(d, slot, epoch, ns, eq, enq, pw)


async def snapshot_date(d: date) -> dict:
    slot = slot_for_utc_date(d)
    epoch = slot // churn.SLOTS_PER_EPOCH
    row = await collect(slot, epoch, d)
    history_db.upsert_snapshot(row)
    return row


async def _finalized_slot() -> int:
    finalized_epoch = await beacon.get_finalized_checkpoint_epoch("head")
    return finalized_epoch * churn.SLOTS_PER_EPOCH


async def backfill(start: date | None = None, limit: int | None = None) -> int:
    """Snapshot every missing UTC date from ``start`` (default Pectra) up to the
    latest finalized day. Resumable. Returns the number of new snapshots."""
    history_db.init_db()
    start = start or pectra_start_date()
    finalized_slot = await _finalized_slot()
    today = datetime.now(timezone.utc).date()
    have = history_db.existing_dates()

    done = 0
    d = start
    while d <= today:
        if limit is not None and done >= limit:
            break
        iso = d.isoformat()
        slot = slot_for_utc_date(d)
        if iso in have or slot < PECTRA_SLOT or slot > finalized_slot:
            d += timedelta(days=1)
            continue
        try:
            row = await snapshot_date(d)
            done += 1
            print(
                f"[{done}] {iso}  slot={slot}  active={row['active_validators']:,}  "
                f"compounding={row['compounding_share']*100:.2f}%  "
                f"exit_wait={row['exit_wait_hours']}h  drain={row['entry_drain_days']:.1f}d",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001 — keep going; resumable next run
            print(f"  !! {iso} (slot {slot}) failed: {e}", flush=True)
        await asyncio.sleep(INTER_SNAPSHOT_DELAY)
        d += timedelta(days=1)

    print(f"backfill complete: {done} new snapshot(s); {history_db.count()} total", flush=True)
    return done


async def catch_up() -> int:
    """Self-heal recent missing days. Bounded to the last MAX_CATCHUP_DAYS so a
    stale DB never triggers a full historical backfill on startup — use the
    `backfill` CLI for that."""
    history_db.init_db()
    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=MAX_CATCHUP_DAYS)

    latest = history_db.latest_date()
    start = (
        datetime.fromisoformat(latest).date() + timedelta(days=1)
        if latest else pectra_start_date()
    )
    start = max(start, window_start)
    if start > today:
        return 0
    return await backfill(start=start)


def _main() -> None:
    parser = argparse.ArgumentParser(description="eth/explorer daily snapshot collector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="snapshot missing days back to Pectra")
    bf.add_argument("--from", dest="from_date", help="start date YYYY-MM-DD")
    bf.add_argument("--days", dest="days", type=int, help="cap number of new snapshots")
    sub.add_parser("catch-up", help="fill from latest stored date to today")

    args = parser.parse_args()
    if args.cmd == "backfill":
        start = date.fromisoformat(args.from_date) if args.from_date else None
        asyncio.run(backfill(start=start, limit=args.days))
    elif args.cmd == "catch-up":
        asyncio.run(catch_up())


if __name__ == "__main__":
    _main()
