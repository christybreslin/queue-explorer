"""Spec-faithful Electra functions for entry / exit / consolidation queue math.

Every function below is lifted verbatim from the Ethereum consensus spec.
Each carries a docstring with the source file + line range; the literal spec
text appears in the trace_* wrappers' spec_excerpt so the methodology UI can
render it next to the live substitution.

Source: inspiration/consensus-specs/specs/electra/beacon-chain.md
        inspiration/consensus-specs/specs/phase0/beacon-chain.md

These functions are pure (no I/O, no caching). The api.beacon layer fetches
beacon state, hands plain dicts/ints to these, and the api.cursors layer
re-derives the few state fields (earliest_exit_epoch, exit_balance_to_consume,
deposit_balance_to_consume) that the standard Beacon REST API doesn't expose.
"""

from __future__ import annotations

from typing import Any

from backend.trace import TraceStep, gwei_to_eth, now_iso

# ---------- Constants (Electra) ----------
GWEI = 1_000_000_000

MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA = 128 * GWEI            # 128 ETH
MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT = 256 * GWEI    # 256 ETH
CHURN_LIMIT_QUOTIENT = 65_536                              # phase0
EFFECTIVE_BALANCE_INCREMENT = 1 * GWEI                     # 1 ETH (phase0)
MIN_ACTIVATION_BALANCE = 32 * GWEI                         # 32 ETH
MAX_EFFECTIVE_BALANCE_ELECTRA = 2_048 * GWEI               # 2048 ETH
MAX_PENDING_DEPOSITS_PER_EPOCH = 16
MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP = 8             # per payload
MAX_WITHDRAWALS_PER_PAYLOAD = 16                           # capella, inherited
MAX_SEED_LOOKAHEAD = 4                                     # phase0
MIN_VALIDATOR_WITHDRAWABILITY_DELAY = 256                  # phase0

SLOTS_PER_EPOCH = 32
SECONDS_PER_SLOT = 12
SECONDS_PER_EPOCH = SLOTS_PER_EPOCH * SECONDS_PER_SLOT     # 384

FAR_FUTURE_EPOCH = (1 << 64) - 1


# ============================================================
# Pure spec functions
# ============================================================

def get_balance_churn_limit(total_active_balance: int) -> int:
    """electra/beacon-chain.md:605-615"""
    churn = max(
        MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA,
        total_active_balance // CHURN_LIMIT_QUOTIENT,
    )
    return churn - churn % EFFECTIVE_BALANCE_INCREMENT


def get_activation_exit_churn_limit(total_active_balance: int) -> int:
    """electra/beacon-chain.md:621-625"""
    return min(
        MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT,
        get_balance_churn_limit(total_active_balance),
    )


def get_consolidation_churn_limit(total_active_balance: int) -> int:
    """electra/beacon-chain.md:631-632"""
    return get_balance_churn_limit(total_active_balance) - get_activation_exit_churn_limit(
        total_active_balance
    )


def compute_activation_exit_epoch(epoch: int) -> int:
    """phase0/beacon-chain.md:925-933"""
    return epoch + 1 + MAX_SEED_LOOKAHEAD


def compute_exit_epoch_and_update_churn(
    earliest_exit_epoch: int,
    exit_balance_to_consume: int,
    exit_balance: int,
    ae_churn: int,
    current_epoch: int,
) -> tuple[int, int, dict[str, Any]]:
    """electra/beacon-chain.md:770-793

    Returns (new_earliest_exit_epoch, new_exit_balance_to_consume, intermediate).
    The third element captures branch + sub-calc decisions for tracing.
    """
    floor = compute_activation_exit_epoch(current_epoch)
    new_earliest = max(earliest_exit_epoch, floor)
    branched_new_epoch = earliest_exit_epoch < new_earliest
    if branched_new_epoch:
        consume = ae_churn
    else:
        consume = exit_balance_to_consume

    additional_epochs = 0
    overflow = exit_balance > consume
    if overflow:
        balance_to_process = exit_balance - consume
        additional_epochs = (balance_to_process - 1) // ae_churn + 1
        new_earliest += additional_epochs
        consume += additional_epochs * ae_churn

    new_consumed = consume - exit_balance

    intermediate = {
        "floor": floor,
        "branched_new_epoch": branched_new_epoch,
        "overflow": overflow,
        "additional_epochs": additional_epochs,
    }
    return new_earliest, new_consumed, intermediate


def compute_consolidation_epoch_and_update_churn(
    earliest_consolidation_epoch: int,
    consolidation_balance_to_consume: int,
    consolidation_balance: int,
    cons_churn: int,
    current_epoch: int,
) -> tuple[int, int, dict[str, Any]]:
    """electra/beacon-chain.md:798-824

    Identical structure to compute_exit_epoch_and_update_churn but draws from
    get_consolidation_churn_limit. If cons_churn == 0 the queue is stalled —
    the caller should detect and surface that.
    """
    if cons_churn == 0:
        return earliest_consolidation_epoch, consolidation_balance_to_consume, {
            "stalled": True,
            "reason": "get_consolidation_churn_limit == 0 (total_active_balance ≤ activation/exit cap)",
        }

    floor = compute_activation_exit_epoch(current_epoch)
    new_earliest = max(earliest_consolidation_epoch, floor)
    branched = earliest_consolidation_epoch < new_earliest
    if branched:
        consume = cons_churn
    else:
        consume = consolidation_balance_to_consume

    additional_epochs = 0
    overflow = consolidation_balance > consume
    if overflow:
        balance_to_process = consolidation_balance - consume
        additional_epochs = (balance_to_process - 1) // cons_churn + 1
        new_earliest += additional_epochs
        consume += additional_epochs * cons_churn

    new_consumed = consume - consolidation_balance
    return new_earliest, new_consumed, {
        "stalled": False,
        "floor": floor,
        "branched_new_epoch": branched,
        "overflow": overflow,
        "additional_epochs": additional_epochs,
    }


def simulate_process_pending_deposits(
    pending_deposits: list[dict],
    ae_churn: int,
    deposit_balance_to_consume: int,
    finalized_slot: int,
    validators_by_pubkey: dict[str, dict],
    current_epoch: int,
    max_epochs: int = 100_000,
    treat_all_finalized: bool = True,
) -> dict[str, Any]:
    """electra/beacon-chain.md:978-1054

    Simulates the per-epoch drain of state.pending_deposits.

    Returns:
      {
        epochs_to_drain: int,
        first_deposit_lands_in_epoch: int,
        last_deposit_lands_in_epoch: int,
        per_epoch: list[{epoch, processed_count, processed_eth, free_pass_count,
                        postponed_count, stop_reason}],
        skipped_unfinalized: int,
        free_pass_withdrawn: int,
        churn_consumed_total: int,
        # for the trace UI:
        first_n_epochs: list[...]   # first 5 epochs for the animation
      }
    """
    # Working copy of the queue (FIFO with postponement)
    queue = list(pending_deposits)
    epoch = current_epoch
    per_epoch: list[dict] = []
    skipped_unfinalized = 0
    free_pass_withdrawn = 0
    churn_consumed_total = 0
    next_epoch_dbc = deposit_balance_to_consume

    last_deposit_lands_in_epoch = current_epoch
    first_deposit_lands_in_epoch = None
    epochs_processed = 0

    while queue and epochs_processed < max_epochs:
        epoch += 1
        epochs_processed += 1
        next_epoch_boundary = epoch + 1  # next_epoch in spec language
        available_for_processing = next_epoch_dbc + ae_churn
        processed_amount = 0
        next_deposit_index = 0
        deposits_to_postpone: list[dict] = []
        is_churn_limit_reached = False
        epoch_processed_count = 0
        epoch_processed_eth = 0
        epoch_free_pass = 0
        epoch_postponed = 0
        stop_reason: str | None = None

        for deposit in queue:
            # 1) Eth1 bridge ordering guard — we don't track eth1_deposit_index;
            #    assume bridge deposits are caught up (true on mainnet for years).

            # 2) Deposit must be finalized. In the live spec finality moves forward
            #    as epochs progress; for a multi-day forward simulation we treat
            #    all currently-queued deposits as eligible by the time we reach them.
            if (not treat_all_finalized) and int(deposit["slot"]) > finalized_slot:
                stop_reason = "unfinalized"
                skipped_unfinalized += 1
                break

            # 3) Per-epoch deposit-count cap.
            if next_deposit_index >= MAX_PENDING_DEPOSITS_PER_EPOCH:
                stop_reason = "max_deposits_per_epoch"
                break

            pk = deposit["pubkey"]
            amount = int(deposit["amount"])
            target = validators_by_pubkey.get(pk)
            is_validator_exited = False
            is_validator_withdrawn = False
            if target is not None:
                exit_epoch = int(target["exit_epoch"])
                withdrawable_epoch = int(target["withdrawable_epoch"])
                is_validator_exited = exit_epoch < FAR_FUTURE_EPOCH
                is_validator_withdrawn = withdrawable_epoch < next_epoch_boundary

            if is_validator_withdrawn:
                # Free pass: balance applied without consuming churn.
                free_pass_withdrawn += 1
                epoch_free_pass += 1
            elif is_validator_exited:
                # Postpone — does not consume churn but counts toward 16/epoch.
                deposits_to_postpone.append(deposit)
                epoch_postponed += 1
            else:
                if processed_amount + amount > available_for_processing:
                    is_churn_limit_reached = True
                    stop_reason = "churn_limit_reached"
                    break
                processed_amount += amount
                epoch_processed_eth += amount
                churn_consumed_total += amount
                epoch_processed_count += 1
                if first_deposit_lands_in_epoch is None:
                    first_deposit_lands_in_epoch = epoch

            next_deposit_index += 1

        # Apply the spec's post-loop adjustments to the working queue.
        queue = queue[next_deposit_index:] + deposits_to_postpone
        next_epoch_dbc = (available_for_processing - processed_amount) if is_churn_limit_reached else 0

        per_epoch.append({
            "epoch": epoch,
            "processed_count": epoch_processed_count,
            "processed_eth": gwei_to_eth(epoch_processed_eth),
            "free_pass_count": epoch_free_pass,
            "postponed_count": epoch_postponed,
            "stop_reason": stop_reason,
            "carry_dbc_eth": gwei_to_eth(next_epoch_dbc),
            "remaining_count": len(queue),
        })

        if epoch_processed_count > 0:
            last_deposit_lands_in_epoch = epoch
        # Note: an epoch with no progress isn't a bug — large compounding deposits
        # at the queue head need multiple epochs of carry to fit. The spec naturally
        # terminates when the queue is empty or when we hit max_epochs.

    return {
        "epochs_to_drain": epochs_processed,
        "queue_remaining": len(queue),
        "first_deposit_lands_in_epoch": first_deposit_lands_in_epoch,
        "last_deposit_lands_in_epoch": last_deposit_lands_in_epoch,
        "per_epoch": per_epoch,
        "first_n_epochs": per_epoch[:5],
        "skipped_unfinalized": skipped_unfinalized,
        "free_pass_withdrawn": free_pass_withdrawn,
        "churn_consumed_total": churn_consumed_total,
    }


# ============================================================
# Trace wrappers — each runs the spec function and packages the result
# ============================================================

def _excerpt_balance_churn() -> str:
    return (
        "def get_balance_churn_limit(state: BeaconState) -> Gwei:\n"
        "    churn = max(\n"
        "        MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA,\n"
        "        get_total_active_balance(state) // CHURN_LIMIT_QUOTIENT\n"
        "    )\n"
        "    return churn - churn % EFFECTIVE_BALANCE_INCREMENT"
    )


def _excerpt_ae_churn() -> str:
    return (
        "def get_activation_exit_churn_limit(state: BeaconState) -> Gwei:\n"
        "    return min(MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT,\n"
        "               get_balance_churn_limit(state))"
    )


def _excerpt_cons_churn() -> str:
    return (
        "def get_consolidation_churn_limit(state: BeaconState) -> Gwei:\n"
        "    return get_balance_churn_limit(state) - get_activation_exit_churn_limit(state)"
    )


def _excerpt_activation_exit_epoch() -> str:
    return (
        "def compute_activation_exit_epoch(epoch: Epoch) -> Epoch:\n"
        "    return Epoch(epoch + 1 + MAX_SEED_LOOKAHEAD)"
    )


def _excerpt_exit_epoch_and_churn() -> str:
    return (
        "def compute_exit_epoch_and_update_churn(state, exit_balance) -> Epoch:\n"
        "    earliest_exit_epoch = max(\n"
        "        state.earliest_exit_epoch,\n"
        "        compute_activation_exit_epoch(get_current_epoch(state)))\n"
        "    per_epoch_churn = get_activation_exit_churn_limit(state)\n"
        "    if state.earliest_exit_epoch < earliest_exit_epoch:\n"
        "        exit_balance_to_consume = per_epoch_churn\n"
        "    else:\n"
        "        exit_balance_to_consume = state.exit_balance_to_consume\n"
        "    if exit_balance > exit_balance_to_consume:\n"
        "        balance_to_process = exit_balance - exit_balance_to_consume\n"
        "        additional_epochs = (balance_to_process - 1) // per_epoch_churn + 1\n"
        "        earliest_exit_epoch += additional_epochs\n"
        "        exit_balance_to_consume += additional_epochs * per_epoch_churn\n"
        "    state.exit_balance_to_consume = exit_balance_to_consume - exit_balance\n"
        "    state.earliest_exit_epoch = earliest_exit_epoch\n"
        "    return state.earliest_exit_epoch"
    )


def _excerpt_consolidation_epoch_and_churn() -> str:
    return (
        "def compute_consolidation_epoch_and_update_churn(state, consolidation_balance) -> Epoch:\n"
        "    # ... identical shape to compute_exit_epoch_and_update_churn ...\n"
        "    # using get_consolidation_churn_limit and state.earliest_consolidation_epoch /\n"
        "    # state.consolidation_balance_to_consume"
    )


def _excerpt_process_pending_deposits() -> str:
    return (
        "def process_pending_deposits(state: BeaconState) -> None:\n"
        "    available_for_processing = state.deposit_balance_to_consume\n"
        "                              + get_activation_exit_churn_limit(state)\n"
        "    for deposit in state.pending_deposits:\n"
        "        if deposit.slot > finalized_slot:               break\n"
        "        if next_deposit_index >= MAX_PENDING_DEPOSITS_PER_EPOCH: break\n"
        "        if is_validator_withdrawn:  apply_pending_deposit(state, deposit)  # free pass\n"
        "        elif is_validator_exited:   deposits_to_postpone.append(deposit)\n"
        "        else:\n"
        "            if processed_amount + deposit.amount > available_for_processing:\n"
        "                break\n"
        "            processed_amount += deposit.amount\n"
        "            apply_pending_deposit(state, deposit)\n"
        "        next_deposit_index += 1\n"
        "    state.pending_deposits = state.pending_deposits[next_deposit_index:]\n"
        "                            + deposits_to_postpone\n"
        "    if is_churn_limit_reached:\n"
        "        state.deposit_balance_to_consume = available_for_processing - processed_amount\n"
        "    else:\n"
        "        state.deposit_balance_to_consume = Gwei(0)"
    )


def trace_balance_churn(total_active_balance: int) -> TraceStep:
    churn = max(MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA, total_active_balance // CHURN_LIMIT_QUOTIENT)
    rounded = churn - churn % EFFECTIVE_BALANCE_INCREMENT
    tab_eth = gwei_to_eth(total_active_balance)
    raw_eth = gwei_to_eth(churn)
    final_eth = gwei_to_eth(rounded)
    return TraceStep(
        id="balance-churn",
        function="get_balance_churn_limit",
        spec_file="electra/beacon-chain.md",
        spec_lines=(605, 615),
        spec_excerpt=_excerpt_balance_churn(),
        inputs={"total_active_balance_eth": tab_eth},
        substituted=(
            f"churn = max(128 ETH, {tab_eth:,.0f} ETH ÷ 65,536)\n"
            f"      = max(128, {tab_eth / 65_536:,.2f})\n"
            f"      = {raw_eth:,.4f} ETH\n"
            f"return {raw_eth:,.4f} − ({raw_eth:,.4f} mod 1 ETH) = {final_eth:,.0f} ETH"
        ),
        intermediate={
            "raw_churn_eth": raw_eth,
            "min_floor_eth": 128,
            "quotient_share_eth": round(tab_eth / 65_536, 4),
        },
        result={"balance_churn_eth": final_eth, "balance_churn_gwei": rounded},
        notes=[],
        refreshed_at=now_iso(),
        provenance="computed",
    )


def trace_ae_churn(total_active_balance: int) -> TraceStep:
    balance_churn = get_balance_churn_limit(total_active_balance)
    ae = min(MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT, balance_churn)
    return TraceStep(
        id="ae-churn",
        function="get_activation_exit_churn_limit",
        spec_file="electra/beacon-chain.md",
        spec_lines=(621, 625),
        spec_excerpt=_excerpt_ae_churn(),
        inputs={"balance_churn_eth": gwei_to_eth(balance_churn)},
        substituted=(
            f"return min(256 ETH, {gwei_to_eth(balance_churn):,.0f} ETH) "
            f"= {gwei_to_eth(ae):,.0f} ETH/epoch"
        ),
        intermediate={"saturated_at_cap": ae == MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT},
        result={"ae_churn_eth": gwei_to_eth(ae), "ae_churn_gwei": ae},
        notes=[
            "This is the per-epoch ETH budget shared by activations (deposits) and exits."
        ],
        refreshed_at=now_iso(),
        provenance="computed",
    )


def trace_consolidation_churn(total_active_balance: int) -> TraceStep:
    balance_churn = get_balance_churn_limit(total_active_balance)
    ae = get_activation_exit_churn_limit(total_active_balance)
    cons = balance_churn - ae
    stalled = cons == 0
    notes = []
    if stalled:
        notes.append(
            "Stalled: total_active_balance is at or below the activation/exit cap, "
            "leaving no budget for consolidations."
        )
    return TraceStep(
        id="cons-churn",
        function="get_consolidation_churn_limit",
        spec_file="electra/beacon-chain.md",
        spec_lines=(631, 632),
        spec_excerpt=_excerpt_cons_churn(),
        inputs={
            "balance_churn_eth": gwei_to_eth(balance_churn),
            "ae_churn_eth": gwei_to_eth(ae),
        },
        substituted=(
            f"return {gwei_to_eth(balance_churn):,.0f} − {gwei_to_eth(ae):,.0f} "
            f"= {gwei_to_eth(cons):,.0f} ETH/epoch"
        ),
        result={"cons_churn_eth": gwei_to_eth(cons), "cons_churn_gwei": cons, "stalled": stalled},
        notes=notes,
        refreshed_at=now_iso(),
        provenance="computed",
    )


def trace_activation_exit_epoch(current_epoch: int) -> TraceStep:
    out = compute_activation_exit_epoch(current_epoch)
    return TraceStep(
        id="activation-exit-epoch",
        function="compute_activation_exit_epoch",
        spec_file="phase0/beacon-chain.md",
        spec_lines=(925, 933),
        spec_excerpt=_excerpt_activation_exit_epoch(),
        inputs={"current_epoch": current_epoch},
        substituted=f"return {current_epoch} + 1 + 4 = {out}",
        result={"floor_epoch": out},
        notes=[
            "The earliest any new activation or exit can land. MAX_SEED_LOOKAHEAD = 4."
        ],
        refreshed_at=now_iso(),
        provenance="computed",
    )


def trace_exit_epoch_and_churn(
    earliest_exit_epoch: int,
    exit_balance_to_consume: int,
    exit_balance: int,
    ae_churn: int,
    current_epoch: int,
    cursor_provenance: str = "derived",
) -> TraceStep:
    new_earliest, new_consumed, inter = compute_exit_epoch_and_update_churn(
        earliest_exit_epoch=earliest_exit_epoch,
        exit_balance_to_consume=exit_balance_to_consume,
        exit_balance=exit_balance,
        ae_churn=ae_churn,
        current_epoch=current_epoch,
    )
    branches = []
    if inter["branched_new_epoch"]:
        branches.append("new_epoch_for_exit (cursor advanced to floor)")
    else:
        branches.append("same_epoch_as_cursor (carry exit_balance_to_consume)")
    if inter["overflow"]:
        branches.append(f"overflow → spent +{inter['additional_epochs']} epochs")
    else:
        branches.append("fits_in_current_epoch")

    return TraceStep(
        id="exit-epoch",
        function="compute_exit_epoch_and_update_churn",
        spec_file="electra/beacon-chain.md",
        spec_lines=(770, 793),
        spec_excerpt=_excerpt_exit_epoch_and_churn(),
        inputs={
            "earliest_exit_epoch": earliest_exit_epoch,
            "exit_balance_to_consume_eth": gwei_to_eth(exit_balance_to_consume),
            "exit_balance_eth": gwei_to_eth(exit_balance),
            "ae_churn_eth": gwei_to_eth(ae_churn),
            "current_epoch": current_epoch,
        },
        substituted=(
            f"floor = compute_activation_exit_epoch({current_epoch}) = {inter['floor']}\n"
            f"earliest_exit_epoch = max({earliest_exit_epoch}, {inter['floor']}) = "
            f"{max(earliest_exit_epoch, inter['floor'])}\n"
            + (
                f"overflow: balance_to_process = {gwei_to_eth(exit_balance):,.0f} − "
                f"{gwei_to_eth(exit_balance_to_consume):,.0f}, "
                f"additional_epochs = {inter['additional_epochs']}\n"
                if inter["overflow"] else "exit fits in current epoch's remaining churn\n"
            )
            + f"→ exit_epoch = {new_earliest}, withdrawable = {new_earliest + MIN_VALIDATOR_WITHDRAWABILITY_DELAY}"
        ),
        branches=branches,
        intermediate=inter,
        result={
            "exit_epoch": new_earliest,
            "withdrawable_epoch": new_earliest + MIN_VALIDATOR_WITHDRAWABILITY_DELAY,
            "new_exit_balance_to_consume_eth": gwei_to_eth(new_consumed),
        },
        notes=[
            f"earliest_exit_epoch and exit_balance_to_consume are {cursor_provenance} "
            f"(not exposed by standard Beacon REST API)."
        ] if cursor_provenance != "live" else [],
        refreshed_at=now_iso(),
        provenance=cursor_provenance,
    )


def trace_consolidation_epoch(
    earliest_consolidation_epoch: int,
    consolidation_balance_to_consume: int,
    consolidation_balance: int,
    cons_churn: int,
    current_epoch: int,
    cursor_provenance: str = "derived",
) -> TraceStep:
    new_earliest, new_consumed, inter = compute_consolidation_epoch_and_update_churn(
        earliest_consolidation_epoch=earliest_consolidation_epoch,
        consolidation_balance_to_consume=consolidation_balance_to_consume,
        consolidation_balance=consolidation_balance,
        cons_churn=cons_churn,
        current_epoch=current_epoch,
    )
    if inter.get("stalled"):
        return TraceStep(
            id="consolidation-epoch",
            function="compute_consolidation_epoch_and_update_churn",
            spec_file="electra/beacon-chain.md",
            spec_lines=(798, 824),
            spec_excerpt=_excerpt_consolidation_epoch_and_churn(),
            inputs={
                "cons_churn_eth": gwei_to_eth(cons_churn),
                "consolidation_balance_eth": gwei_to_eth(consolidation_balance),
            },
            substituted="cons_churn = 0 — consolidations are stalled.",
            branches=["stalled"],
            intermediate=inter,
            result={"stalled": True, "exit_epoch": None, "withdrawable_epoch": None},
            notes=[inter["reason"]],
            refreshed_at=now_iso(),
            provenance=cursor_provenance,
        )
    return TraceStep(
        id="consolidation-epoch",
        function="compute_consolidation_epoch_and_update_churn",
        spec_file="electra/beacon-chain.md",
        spec_lines=(798, 824),
        spec_excerpt=_excerpt_consolidation_epoch_and_churn(),
        inputs={
            "earliest_consolidation_epoch": earliest_consolidation_epoch,
            "consolidation_balance_to_consume_eth": gwei_to_eth(consolidation_balance_to_consume),
            "consolidation_balance_eth": gwei_to_eth(consolidation_balance),
            "cons_churn_eth": gwei_to_eth(cons_churn),
            "current_epoch": current_epoch,
        },
        substituted=(
            f"floor = {inter['floor']}, additional_epochs = {inter['additional_epochs']}\n"
            f"→ consolidation_epoch = {new_earliest}"
        ),
        branches=[
            "new_epoch" if inter["branched_new_epoch"] else "same_epoch_as_cursor",
            "overflow" if inter["overflow"] else "fits_in_current_epoch",
        ],
        intermediate=inter,
        result={
            "consolidation_epoch": new_earliest,
            "withdrawable_epoch": new_earliest + MIN_VALIDATOR_WITHDRAWABILITY_DELAY,
            "new_consolidation_balance_to_consume_eth": gwei_to_eth(new_consumed),
        },
        notes=[
            f"earliest_consolidation_epoch and consolidation_balance_to_consume are "
            f"{cursor_provenance} (not exposed by standard Beacon REST API)."
        ] if cursor_provenance != "live" else [],
        refreshed_at=now_iso(),
        provenance=cursor_provenance,
    )


def trace_simulate_pending_deposits(
    pending_aggregate: dict,
    pending_deposits: list[dict],
    ae_churn: int,
    deposit_balance_to_consume: int,
    finalized_slot: int,
    validators_by_pubkey: dict[str, dict],
    current_epoch: int,
) -> TraceStep:
    sim = simulate_process_pending_deposits(
        pending_deposits=pending_deposits,
        ae_churn=ae_churn,
        deposit_balance_to_consume=deposit_balance_to_consume,
        finalized_slot=finalized_slot,
        validators_by_pubkey=validators_by_pubkey,
        current_epoch=current_epoch,
    )
    epochs = sim["epochs_to_drain"] or 0
    drain_seconds = epochs * SECONDS_PER_EPOCH
    drain_days = drain_seconds / 86_400
    last_lands = sim["last_deposit_lands_in_epoch"]

    return TraceStep(
        id="simulate-deposits",
        function="process_pending_deposits (simulated)",
        spec_file="electra/beacon-chain.md",
        spec_lines=(978, 1054),
        spec_excerpt=_excerpt_process_pending_deposits(),
        inputs={
            "pending_count": pending_aggregate.get("count"),
            "pending_eth": pending_aggregate.get("total_eth"),
            "finalized_count": pending_aggregate.get("finalized_count"),
            "finalized_eth": pending_aggregate.get("finalized_eth"),
            "ae_churn_eth": gwei_to_eth(ae_churn),
            "deposit_balance_to_consume_eth": gwei_to_eth(deposit_balance_to_consume),
        },
        substituted=(
            f"per epoch: process ≤ 16 deposits AND ≤ "
            f"{gwei_to_eth(ae_churn):,.0f} ETH (+ {gwei_to_eth(deposit_balance_to_consume):,.0f} carryover)\n"
            f"queue: {pending_aggregate.get('finalized_count', 0):,} finalized deposits, "
            f"{pending_aggregate.get('finalized_eth', 0):,.0f} ETH\n"
            f"→ {epochs:,} epochs to drain ≈ {drain_days:,.1f} days"
        ),
        branches=[f"first 5 epochs simulated → {len(sim['first_n_epochs'])} steps captured"],
        intermediate={
            "first_n_epochs": sim["first_n_epochs"],
            "free_pass_withdrawn": sim["free_pass_withdrawn"],
            "churn_consumed_total_eth": gwei_to_eth(sim["churn_consumed_total"]),
        },
        result={
            "epochs_to_drain": epochs,
            "drain_seconds": drain_seconds,
            "drain_days": round(drain_days, 2),
            "last_deposit_lands_in_epoch": last_lands,
        },
        notes=[
            "The spec has three paths per deposit — consume churn, free-pass for fully-withdrawn targets, or postpone for exiting targets. Only the normal path consumes the per-epoch budget.",
        ],
        refreshed_at=now_iso(),
        provenance="computed",
    )


# ============================================================
# Wait-time helpers
# ============================================================

def epoch_to_timestamp(epoch: int, genesis_time: int = 1_606_824_023) -> int:
    return genesis_time + epoch * SECONDS_PER_EPOCH


def hours_between(from_epoch: int, to_epoch: int) -> float:
    return (to_epoch - from_epoch) * SECONDS_PER_EPOCH / 3600
