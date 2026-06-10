import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend import beacon, churn, cursors, history_db, metrics, models, snapshot, spec, trace


async def _history_collector_loop():
    """Forward-only self-heal: fill any days missing since the latest stored
    snapshot, on boot and ~every 6h while running. Never triggers the full
    Pectra backfill (that is a deliberate one-time CLI job) — if there is no
    history yet, it simply waits until a backfill seeds the DB."""
    while True:
        try:
            if history_db.latest_date() is not None:
                n = await snapshot.catch_up()
                if n:
                    print(f"[history] caught up {n} day(s)", flush=True)
        except Exception as e:  # noqa: BLE001 — never let collection crash the app
            print(f"[history] catch-up failed: {e}", flush=True)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    history_db.init_db()
    task = asyncio.create_task(_history_collector_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Ethereum Monitor API", version="0.1.0", lifespan=lifespan)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class NoStaleStaticMiddleware(BaseHTTPMiddleware):
    """Force browsers (looking at you, Safari) to revalidate the dashboard
    assets on every load. The dashboard is small and changes often during
    development; cache hits cause confusing 'why did nothing change?' bugs.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.endswith((".html", ".js", ".css")) or path in ("/", ""):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.add_middleware(NoStaleStaticMiddleware)


@app.get("/network")
async def network_info():
    url = beacon.BEACON_URL
    # Derive network name from the URL
    lower = url.lower()
    if "hoodi" in lower:
        name = "Hoodi"
    elif "holesky" in lower:
        name = "Holesky"
    elif "sepolia" in lower:
        name = "Sepolia"
    elif "mainnet" in lower or "ethereum" in lower:
        name = "Mainnet"
    else:
        name = "Unknown"
    return {"name": name, "rpc": url}


@app.get("/exit-queue", response_model=models.ExitQueueResponse)
async def exit_queue():
    """Spec-faithful exit queue summary using api.spec + api.cursors.

    queue_depth_epochs and estimated_wait_hours measure how many epochs until
    state.earliest_exit_epoch (the cursor) — i.e. how long a new exit submitted
    right now would wait before its exit_epoch can be assigned.
    """
    head_slot = await beacon.get_head_slot()
    current_epoch = head_slot // spec.SLOTS_PER_EPOCH
    return await metrics.compute_exit_queue("head", current_epoch)


@app.get("/network/stats")
async def network_stats():
    """High-level snapshot for the home-tab network overview card."""
    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)
    return await metrics.compute_network_stats("head", current_epoch)


@app.get("/entry-queue")
async def entry_queue():
    """Lightweight entry-queue summary — no simulator, just aggregates.
    Powers the home-tab gauge alongside /exit-queue.
    """
    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)
    return await metrics.compute_entry_queue("head", current_epoch)


@app.get("/exit-queue/history", response_model=models.ExitQueueHistoryResponse)
async def exit_queue_history(epochs_back: int = Query(default=50, ge=1, le=500)):
    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)

    # Sample at intervals to avoid too many RPC calls
    step = max(1, epochs_back // 10)
    entries = []
    for offset in range(0, epochs_back, step):
        epoch = current_epoch - offset
        if epoch < 0:
            break
        slot = epoch * churn.SLOTS_PER_EPOCH
        try:
            exiting = await beacon.get_active_exiting_validators(str(slot))
            total_bal = sum(
                int(v["validator"]["effective_balance"]) for v in exiting
            )
            entries.append(models.ExitQueueHistoryEntry(
                epoch=epoch,
                exiting_validators=len(exiting),
                total_balance_gwei=total_bal,
            ))
        except Exception:
            continue

    return models.ExitQueueHistoryResponse(
        current_epoch=current_epoch,
        entries=entries,
    )


@app.get("/history/daily")
async def history_daily(
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
):
    """Daily scalar snapshots oldest→newest. Optional inclusive ?from=&to= dates
    (YYYY-MM-DD). Populated by the snapshot collector; empty until a backfill has
    run. Per-row fields are the same scalar metrics the home tab shows live."""
    snaps = history_db.get_range(from_date, to_date)
    return {
        "count": len(snaps),
        "first": snaps[0]["date"] if snaps else None,
        "last": snaps[-1]["date"] if snaps else None,
        "snapshots": snaps,
    }


def _credential_type(withdrawal_credentials: str) -> str:
    if withdrawal_credentials.startswith("0x02"):
        return "compounding"
    if withdrawal_credentials.startswith("0x01"):
        return "execution"
    return "bls"


@app.get("/validator/{pubkey_or_index}/status", response_model=models.ValidatorStatusResponse)
async def validator_status(pubkey_or_index: str):
    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)

    try:
        v = await beacon.get_validator("head", pubkey_or_index)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"Validator not found on this network: {pubkey_or_index}",
        )
    exit_epoch = v["validator"]["exit_epoch"]
    withdrawable_epoch = v["validator"]["withdrawable_epoch"]

    estimated_exit_time = None
    estimated_wait_hours = None
    if exit_epoch != "18446744073709551615":  # FAR_FUTURE_EPOCH
        exit_ep = int(exit_epoch)
        if exit_ep > current_epoch:
            wait_epochs = exit_ep - current_epoch
            estimated_wait_hours = churn.epochs_to_hours(wait_epochs)
            estimated_exit_time = churn.epoch_to_datetime(exit_ep).isoformat()

    balance = int(v["balance"])
    return models.ValidatorStatusResponse(
        index=v["index"],
        pubkey=v["validator"]["pubkey"],
        status=v["status"],
        balance_gwei=balance,
        balance_eth=round(balance / churn.GWEI, 4),
        effective_balance_gwei=int(v["validator"]["effective_balance"]),
        credential_type=_credential_type(v["validator"].get("withdrawal_credentials", "")),
        exit_epoch=exit_epoch,
        withdrawable_epoch=withdrawable_epoch,
        estimated_exit_time=estimated_exit_time,
        estimated_wait_hours=estimated_wait_hours,
    )


def _pending_deposit_response(match: dict, current_epoch: int, head_slot: int) -> dict:
    """Shape a /validator/{pubkey}/lookup response for a pubkey that only exists
    in state.pending_deposits (no Validator object yet). The frontend marks the
    lifecycle rail at state 1 (Queued deposits) for this case.
    """
    d = match["deposit"]
    amount_gwei = int(d["amount"])
    creds = d.get("withdrawal_credentials", "") or ""
    creds_clean = creds.lower().removeprefix("0x")
    creds_addr = None
    if creds_clean[:2] in ("01", "02") and len(creds_clean) >= 64:
        creds_addr = "0x" + creds_clean[-40:]

    # ETA: actual ETH ahead in the queue / per-day churn budget.
    # find_pending_deposit_by_pubkey sums real `amount` values up to this entry,
    # so the estimate handles a head full of compounding deposits correctly.
    EPOCHS_PER_DAY = 86400 / spec.SECONDS_PER_EPOCH
    ahead_eth = match.get("ahead_gwei", 0) / spec.GWEI
    churn_per_day_eth = 256 * EPOCHS_PER_DAY  # ae_churn on mainnet today
    eta_days = ahead_eth / churn_per_day_eth if churn_per_day_eth else 0
    eta_seconds = int(eta_days * 86400)

    return {
        "is_pending_deposit": True,
        "index": None,
        "pubkey": d.get("pubkey"),
        "status": "pending_deposit",
        "slashed": False,
        "balance_gwei": amount_gwei,
        "balance_eth": round(amount_gwei / spec.GWEI, 4),
        "effective_balance_gwei": 0,
        "effective_balance_eth": 0.0,
        "credential_type": _credential_type(creds),
        "credential_address": creds_addr,
        "activation_epoch": None,
        "exit_epoch": None,
        "withdrawable_epoch": None,
        "current_epoch": current_epoch,
        "current_slot": head_slot,
        "seconds_per_epoch": spec.SECONDS_PER_EPOCH,
        "pending_partial_withdrawals": [],
        "pending_deposits": [{
            "amount_gwei": amount_gwei,
            "amount_eth": round(amount_gwei / spec.GWEI, 4),
            "position": match["position"],
            "queue_total": match["queue_total"],
            "ahead_eth": round(ahead_eth, 0),
            "slot": int(d["slot"]) if d.get("slot") else None,
            "eta_seconds": eta_seconds,
        }],
        "pending_consolidations": [],
        "exit_queue_position": None,
    }


@app.get("/validator/{pubkey_or_index}/lookup")
async def validator_lookup(pubkey_or_index: str):
    """Bundle everything beacon state knows about a validator into one response:
    status, balances, lifecycle epochs, queue positions, and any pending
    partial withdrawals / deposits / consolidations involving it.

    Powers the validator-info prototype UI.
    """
    FAR_FUTURE = "18446744073709551615"

    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)

    try:
        v = await beacon.get_validator("head", pubkey_or_index)
    except Exception:
        # Fallback: if the query is a pubkey, the validator object may not exist
        # on the CL yet — check state.pending_deposits before giving up.
        q = pubkey_or_index.strip()
        is_pubkey = q.lower().startswith("0x") and len(q) >= 66
        if is_pubkey:
            match = await beacon.find_pending_deposit_by_pubkey(q)
            if match:
                return _pending_deposit_response(match, current_epoch, head_slot)
        raise HTTPException(404, detail=f"Validator not found: {pubkey_or_index}")

    vd = v["validator"]
    pubkey = vd["pubkey"]
    index = int(v["index"])
    status = v["status"]
    balance_gwei = int(v["balance"])
    effective_balance_gwei = int(vd["effective_balance"])
    activation_epoch = vd["activation_epoch"]
    exit_epoch = vd["exit_epoch"]
    withdrawable_epoch = vd["withdrawable_epoch"]
    slashed = bool(vd.get("slashed", False))
    withdrawal_credentials = vd.get("withdrawal_credentials", "")
    is_exiting_scheduled = exit_epoch != FAR_FUTURE
    is_currently_in_exit_queue = status in ("active_exiting", "active_slashed")

    # Pull the queues we need to compute positions.
    pending_partials, pending_consolidations = await asyncio.gather(
        beacon.get_pending_partial_withdrawals(),
        beacon.get_pending_consolidations(),
    )
    # Only fetch the live exiting set if the validator is *currently* in it
    # (not already exited). Saves a fetch on most lookups.
    exiting_vals: list[dict] = []
    if is_currently_in_exit_queue:
        exiting_vals = await beacon.get_active_exiting_validators()

    # Pending deposit lookup is opt-in via header — pending_deposits is 14 MB
    # on mainnet so we skip it unless the caller explicitly asks for it.
    pd_results: list[dict] = []

    # ---- Pending partial withdrawals involving this validator ----
    pp_results: list[dict] = []
    PARTIALS_PER_SLOT = 8  # MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP
    for i, p in enumerate(pending_partials):
        if int(p.get("validator_index", -1)) != index:
            continue
        amount_gwei = int(p["amount"])
        eta_slots = i // PARTIALS_PER_SLOT
        pp_results.append({
            "amount_gwei": amount_gwei,
            "amount_eth": round(amount_gwei / spec.GWEI, 4),
            "position": i + 1,
            "queue_total": len(pending_partials),
            "withdrawable_epoch": int(p.get("withdrawable_epoch", 0)),
            "eta_seconds": int(eta_slots * 12),
        })

    # ---- Pending consolidations involving this validator ----
    pc_results: list[dict] = []
    for i, c in enumerate(pending_consolidations):
        src = int(c.get("source_index", -1))
        tgt = int(c.get("target_index", -1))
        if src != index and tgt != index:
            continue
        pc_results.append({
            "source_index": src,
            "target_index": tgt,
            "role": "source" if src == index else "target",
            "position": i + 1,
            "queue_total": len(pending_consolidations),
        })

    # ---- Exit-queue position (rank by exit_epoch, tie-break by index) ----
    # Only meaningful while the validator is still active_exiting.
    exit_queue_position = None
    if is_currently_in_exit_queue and exiting_vals:
        target_ep = int(exit_epoch)
        ahead = 0
        for ev in exiting_vals:
            ep = int(ev["validator"]["exit_epoch"])
            if ep < target_ep:
                ahead += 1
            elif ep == target_ep and int(ev["index"]) < index:
                ahead += 1
        wait_epochs = max(0, target_ep - current_epoch)
        exit_queue_position = {
            "position": ahead + 1,
            "total": len(exiting_vals),
            "exit_epoch": target_ep,
            "eta_seconds": int(wait_epochs * spec.SECONDS_PER_EPOCH),
        }

    # ---- Withdrawal credentials → execution address (0x01/0x02) ----
    creds_addr = None
    creds_clean = withdrawal_credentials.lower().removeprefix("0x")
    if creds_clean[:2] in ("01", "02") and len(creds_clean) >= 64:
        creds_addr = "0x" + creds_clean[-40:]

    return {
        "index": index,
        "pubkey": pubkey,
        "status": status,
        "slashed": slashed,
        "balance_gwei": balance_gwei,
        "balance_eth": round(balance_gwei / spec.GWEI, 4),
        "effective_balance_gwei": effective_balance_gwei,
        "effective_balance_eth": round(effective_balance_gwei / spec.GWEI, 4),
        "credential_type": _credential_type(withdrawal_credentials),
        "credential_address": creds_addr,
        "activation_epoch": int(activation_epoch) if activation_epoch != FAR_FUTURE else None,
        "exit_epoch": int(exit_epoch) if is_exiting_scheduled else None,
        "withdrawable_epoch": int(withdrawable_epoch) if withdrawable_epoch != FAR_FUTURE else None,
        "current_epoch": current_epoch,
        "current_slot": head_slot,
        "seconds_per_epoch": spec.SECONDS_PER_EPOCH,
        "pending_partial_withdrawals": pp_results,
        "pending_deposits": pd_results,  # always [] for now — opt-in later
        "pending_consolidations": pc_results,
        "exit_queue_position": exit_queue_position,
    }


@app.get("/predict/exit", response_model=models.ExitPredictionResponse)
async def predict_exit(balance: int = Query(..., ge=1, description="Balance in gwei")):
    """Spec-faithful exit prediction via compute_exit_epoch_and_update_churn.
    Matches /methodology/exit-queue exactly.
    """
    head_slot = await beacon.get_head_slot()
    current_epoch = head_slot // spec.SLOTS_PER_EPOCH

    exiting, total_active_balance = await asyncio.gather(
        beacon.get_active_exiting_validators(),
        beacon.get_total_active_balance(),
    )

    ae_churn_gwei = spec.get_activation_exit_churn_limit(total_active_balance)
    cursor = cursors.derive_exit_cursor(exiting, current_epoch, total_active_balance)

    predicted_epoch, _, _ = spec.compute_exit_epoch_and_update_churn(
        earliest_exit_epoch=cursor["earliest_exit_epoch"],
        exit_balance_to_consume=cursor["exit_balance_to_consume_gwei"],
        exit_balance=balance,
        ae_churn=ae_churn_gwei,
        current_epoch=current_epoch,
    )

    wait_epochs = max(0, predicted_epoch - current_epoch)
    withdrawable_epoch = predicted_epoch + spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY

    return models.ExitPredictionResponse(
        balance_gwei=balance,
        balance_eth=round(balance / spec.GWEI, 4),
        predicted_exit_epoch=predicted_epoch,
        estimated_wait_epochs=wait_epochs,
        estimated_wait_hours=round(wait_epochs * spec.SECONDS_PER_EPOCH / 3600, 2),
        withdrawable_epoch=withdrawable_epoch,
        estimated_withdrawable_hours=round(
            (withdrawable_epoch - current_epoch) * spec.SECONDS_PER_EPOCH / 3600, 2
        ),
        current_epoch=current_epoch,
        churn_limit_gwei=ae_churn_gwei,
    )


@app.get("/predict/partial-withdrawal", response_model=models.PartialWithdrawalPredictionResponse)
async def predict_partial_withdrawal(
    amount: int = Query(..., ge=1, description="Withdrawal amount in gwei"),
):
    """Partial withdrawals are NOT churn-gated. A new EL withdrawal request:
      1) sits in pending_partial_withdrawals with
         withdrawable_epoch = current_epoch + MIN_VALIDATOR_WITHDRAWABILITY_DELAY (256),
      2) then becomes eligible for the sweep at ≤ 8 partials/payload × 32 slots/epoch.

    `predicted_epoch` = withdrawable_epoch (when the new request becomes eligible).
    `withdrawable_epoch` = withdrawable_epoch + any sweep-queue position offset.
    """
    head_slot, total_active_balance, pending_partials = await asyncio.gather(
        beacon.get_head_slot(),
        beacon.get_total_active_balance(),
        beacon.get_pending_partial_withdrawals(),
    )
    current_epoch = head_slot // spec.SLOTS_PER_EPOCH
    ae_churn_gwei = spec.get_activation_exit_churn_limit(total_active_balance)

    rate_per_epoch = spec.MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP * spec.SLOTS_PER_EPOCH  # 256

    # Eligibility delay: a freshly submitted partial waits 256 epochs before the sweep can touch it.
    eligible_epoch = current_epoch + spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY

    # Sweep-position offset: the new partial appends to the end of the queue.
    new_position = len(pending_partials)
    sweep_offset_epochs = new_position // rate_per_epoch if rate_per_epoch else 0
    process_epoch = eligible_epoch + sweep_offset_epochs
    wait_epochs = max(0, process_epoch - current_epoch)

    return models.PartialWithdrawalPredictionResponse(
        amount_gwei=amount,
        amount_eth=round(amount / spec.GWEI, 4),
        predicted_epoch=eligible_epoch,
        estimated_wait_epochs=wait_epochs,
        estimated_wait_hours=round(wait_epochs * spec.SECONDS_PER_EPOCH / 3600, 2),
        withdrawable_epoch=process_epoch,
        estimated_withdrawable_hours=round(
            (process_epoch - current_epoch) * spec.SECONDS_PER_EPOCH / 3600, 2
        ),
        current_epoch=current_epoch,
        churn_limit_gwei=ae_churn_gwei,
    )


@app.get("/pending-partial-withdrawals", response_model=models.PendingPartialWithdrawalsResponse)
async def pending_partial_withdrawals():
    return await metrics.compute_partials("head")


MAX_SOURCE_RESOLVE = 500  # cap individual validator lookups


async def _resolve_validators(indices: list[str]) -> dict[str, dict]:
    """Resolve validators concurrently."""
    result: dict[str, dict] = {}
    resolved = await asyncio.gather(
        *[beacon.get_validator("head", idx) for idx in indices],
        return_exceptions=True,
    )
    for idx, v in zip(indices, resolved):
        if not isinstance(v, Exception):
            result[idx] = v
    return result


MAX_BATCH_VALIDATORS = 100


@app.post("/validators/batch", response_model=models.BatchValidatorsResponse)
async def validators_batch(req: models.BatchValidatorsRequest):
    if len(req.validators) > MAX_BATCH_VALIDATORS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_VALIDATORS} validators per request",
        )

    head_slot = await beacon.get_head_slot()
    current_epoch = churn.slot_to_epoch(head_slot)

    results: list[models.ValidatorStatusResponse] = []
    errors: list[str] = []

    resolved = await asyncio.gather(
        *[beacon.get_validator("head", vid) for vid in req.validators],
        return_exceptions=True,
    )
    resolved_map: dict[str, dict] = {}
    unresolved_errors: dict[str, str] = {}
    for vid, v in zip(req.validators, resolved):
        if isinstance(v, Exception):
            unresolved_errors[vid] = str(v)
        else:
            resolved_map[vid] = v

    # ---- Pending-deposit fallback ----
    # Any unresolved entry that looks like a pubkey gets one shot at
    # state.pending_deposits before being declared a hard error.
    pending_entries: dict[str, dict] = {}  # vid -> pending-entry dict
    unresolved_pubkeys = [
        vid for vid in unresolved_errors
        if vid.strip().lower().startswith("0x") and len(vid.strip()) >= 66
    ]
    if unresolved_pubkeys:
        deposits = await beacon.get_pending_deposits()
        targets = {vid.strip().lower(): vid for vid in unresolved_pubkeys}
        ahead_gwei = 0
        for i, d in enumerate(deposits):
            pk = (d.get("pubkey") or "").lower()
            if pk in targets:
                pending_entries[targets[pk]] = {
                    "deposit": d,
                    "position": i + 1,
                    "queue_total": len(deposits),
                    "ahead_gwei": ahead_gwei,
                }
                # Don't break — multiple inputs might match different deposits in one pass.
            try:
                ahead_gwei += int(d.get("amount") or 0)
            except (TypeError, ValueError):
                pass

    EPOCHS_PER_DAY = 86400 / spec.SECONDS_PER_EPOCH
    CHURN_PER_DAY_ETH = 256 * EPOCHS_PER_DAY  # mainnet ae_churn

    for vid in req.validators:
        v = resolved_map.get(vid)
        if v is not None:
            exit_epoch = v["validator"]["exit_epoch"]
            estimated_exit_time = None
            estimated_wait_hours = None
            if exit_epoch != "18446744073709551615":
                exit_ep = int(exit_epoch)
                if exit_ep > current_epoch:
                    estimated_wait_hours = churn.epochs_to_hours(exit_ep - current_epoch)
                    estimated_exit_time = churn.epoch_to_datetime(exit_ep).isoformat()

            balance = int(v["balance"])
            results.append(models.ValidatorStatusResponse(
                index=v["index"],
                pubkey=v["validator"]["pubkey"],
                status=v["status"],
                balance_gwei=balance,
                balance_eth=round(balance / churn.GWEI, 4),
                effective_balance_gwei=int(v["validator"]["effective_balance"]),
                credential_type=_credential_type(v["validator"].get("withdrawal_credentials", "")),
                exit_epoch=exit_epoch,
                withdrawable_epoch=v["validator"]["withdrawable_epoch"],
                estimated_exit_time=estimated_exit_time,
                estimated_wait_hours=estimated_wait_hours,
            ))
            continue

        # Pending-deposit fallback hit?
        match = pending_entries.get(vid)
        if match is not None:
            d = match["deposit"]
            amount_gwei = int(d["amount"])
            ahead_eth = match["ahead_gwei"] / spec.GWEI
            eta_seconds = int((ahead_eth / CHURN_PER_DAY_ETH) * 86400) if CHURN_PER_DAY_ETH else 0
            results.append(models.ValidatorStatusResponse(
                index=None,
                pubkey=d.get("pubkey") or vid,
                status="pending_deposit",
                balance_gwei=amount_gwei,
                balance_eth=round(amount_gwei / spec.GWEI, 4),
                effective_balance_gwei=0,
                credential_type=_credential_type(d.get("withdrawal_credentials", "") or ""),
                exit_epoch=None,
                withdrawable_epoch=None,
                is_pending_deposit=True,
                pending_deposit_position=match["position"],
                pending_deposit_queue_total=match["queue_total"],
                pending_deposit_amount_eth=round(amount_gwei / spec.GWEI, 4),
                pending_deposit_ahead_eth=round(ahead_eth, 0),
                pending_deposit_eta_seconds=eta_seconds,
                pending_deposit_slot=int(d["slot"]) if d.get("slot") else None,
            ))
            continue

        # Genuinely not found.
        errors.append(f"{vid}: {unresolved_errors.get(vid, 'not found')}")

    return models.BatchValidatorsResponse(validators=results, errors=errors)


@app.get("/consolidations", response_model=models.ConsolidationsResponse)
async def consolidations():
    raw = await beacon.get_pending_consolidations()

    by_target: dict[str, list[str]] = defaultdict(list)
    for c in raw:
        by_target[str(c["target_index"])].append(str(c["source_index"]))

    # Collect all unique validator indices to resolve
    target_indices = list(by_target.keys())
    all_source_indices: list[str] = []
    for sources in by_target.values():
        all_source_indices.extend(sources)

    # Deduplicate and cap
    unique_sources = list(dict.fromkeys(all_source_indices))
    resolve_sources = unique_sources[:MAX_SOURCE_RESOLVE]

    # Resolve in batches to avoid rate limits
    all_lookups = list(dict.fromkeys(target_indices + resolve_sources))
    val_map = await _resolve_validators(all_lookups)

    targets = []
    total_eth = 0.0
    for tidx in target_indices:
        tv = val_map.get(tidx)
        if tv is None:
            continue
        source_list = by_target[tidx]

        # Build resolved source entries
        sources = []
        incoming = 0.0
        for sidx in source_list:
            sv = val_map.get(sidx)
            if sv:
                bal = round(int(sv.get("balance", 0)) / churn.GWEI, 4)
                eff = round(int(sv.get("validator", {}).get("effective_balance", 0)) / churn.GWEI, 4)
                status = sv.get("status", "unknown")
            else:
                bal = 32.0
                eff = 32.0
                status = "unresolved"
            incoming += bal
            sources.append(models.ConsolidationSource(
                index=sidx,
                balance_eth=bal,
                effective_balance_eth=eff,
                status=status,
            ))

        total_eth += incoming
        t_balance = int(tv.get("balance", 0))
        t_eff = int(tv.get("validator", {}).get("effective_balance", 0))

        targets.append(models.ConsolidationTarget(
            target_index=tidx,
            target_pubkey=tv.get("validator", {}).get("pubkey", ""),
            target_status=tv.get("status", "unknown"),
            target_balance_eth=round(t_balance / churn.GWEI, 4),
            target_effective_balance_eth=round(t_eff / churn.GWEI, 4),
            source_count=len(source_list),
            sources=sources,
            total_incoming_eth=round(incoming, 4),
        ))

    targets.sort(key=lambda t: t.source_count, reverse=True)

    return models.ConsolidationsResponse(
        count=len(raw),
        target_count=len(targets),
        total_eth=round(total_eth, 4),
        targets=targets,
    )


@app.get("/churn", response_model=models.ChurnResponse)
async def churn_info():
    """Spec-faithful churn report via api.spec.

    Returns the activation/exit churn limit (the budget shared by activations
    and exits). The full balance churn is balance_churn = ae_churn + cons_churn
    if the user needs to break it out.
    """
    total_active_balance = await beacon.get_total_active_balance()

    balance_churn = spec.get_balance_churn_limit(total_active_balance)
    ae_churn = spec.get_activation_exit_churn_limit(total_active_balance)
    cons_churn = spec.get_consolidation_churn_limit(total_active_balance)

    explanation = (
        f"balance_churn = max(128 ETH, {trace.gwei_to_eth(total_active_balance):,.0f} ETH ÷ 65,536) "
        f"rounded to 1 ETH = {trace.gwei_to_eth(balance_churn):,.0f} ETH/epoch. "
        f"activation/exit churn = min(256 ETH, balance_churn) = {trace.gwei_to_eth(ae_churn):,.0f} ETH/epoch. "
        f"consolidation churn = balance_churn − ae_churn = {trace.gwei_to_eth(cons_churn):,.0f} ETH/epoch."
    )

    return models.ChurnResponse(
        churn_limit_gwei=ae_churn,
        churn_limit_eth=round(ae_churn / spec.GWEI, 4),
        total_active_balance_gwei=total_active_balance,
        total_active_balance_eth=round(total_active_balance / spec.GWEI, 4),
        min_per_epoch_churn_limit=spec.MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA,
        max_per_epoch_churn_limit=spec.MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT,
        churn_limit_quotient=spec.CHURN_LIMIT_QUOTIENT,
        explanation=explanation,
    )


# ============================================================
# Methodology: live spec waterfall
# ============================================================

async def _gather_state_context():
    """Fetch the common beacon-state inputs every methodology endpoint needs."""
    head_slot, total_active_balance, fork, finalized_epoch = await asyncio.gather(
        beacon.get_head_slot(),
        beacon.get_total_active_balance(),
        beacon.get_fork(),
        beacon.get_finalized_checkpoint_epoch(),
    )
    current_epoch = head_slot // spec.SLOTS_PER_EPOCH
    return {
        "head_slot": head_slot,
        "current_epoch": current_epoch,
        "total_active_balance_gwei": total_active_balance,
        "finalized_epoch": finalized_epoch,
        "fork_current_version": fork.get("current_version"),
        "fork_epoch": int(fork.get("epoch", 0)),
    }


def _trace_state_inputs(ctx: dict) -> trace.TraceStep:
    """Step 0 — the beacon-state values we read live."""
    return trace.TraceStep(
        id="state-inputs",
        function="(beacon state — live REST)",
        spec_file="beacon-APIs",
        spec_lines=(0, 0),
        spec_excerpt=(
            "GET /eth/v1/beacon/headers/head           → head_slot, current_epoch\n"
            "GET /eth/v1/beacon/states/head/finality_checkpoints → finalized_epoch\n"
            "GET /eth/v1/beacon/states/head/fork       → current fork version\n"
            "GET /eth/v1/beacon/states/head/validators?status=active_*\n"
            "                                          → Σ effective_balance"
        ),
        inputs={},
        substituted=(
            f"head_slot           = {ctx['head_slot']:,}\n"
            f"current_epoch       = {ctx['current_epoch']:,}\n"
            f"finalized_epoch     = {ctx['finalized_epoch']:,}\n"
            f"fork_current_version= {ctx['fork_current_version']}\n"
            f"total_active_balance= {trace.gwei_to_eth(ctx['total_active_balance_gwei']):,.0f} ETH"
        ),
        result={
            "head_slot": ctx["head_slot"],
            "current_epoch": ctx["current_epoch"],
            "finalized_epoch": ctx["finalized_epoch"],
            "fork_current_version": ctx["fork_current_version"],
            "total_active_balance_eth": trace.gwei_to_eth(ctx["total_active_balance_gwei"]),
        },
        notes=[
            "Live values from the Beacon REST API. The remaining steps are computed from these."
        ],
        refreshed_at=trace.now_iso(),
        provenance="live",
    )


def _churn_steps(tab_gwei: int) -> list[trace.TraceStep]:
    """Steps 1-3 — balance churn, activation/exit churn, consolidation churn."""
    return [
        spec.trace_balance_churn(tab_gwei),
        spec.trace_ae_churn(tab_gwei),
        spec.trace_consolidation_churn(tab_gwei),
    ]


@app.get("/methodology/state-summary")
async def methodology_state_summary():
    """Light methodology trace: state inputs + the three churn limits.
    No queue simulation — fast, used by the home tab too.
    """
    ctx = await _gather_state_context()
    steps = [_trace_state_inputs(ctx), *_churn_steps(ctx["total_active_balance_gwei"])]
    return {
        "kind": "state-summary",
        "trace": [s.to_dict() for s in steps],
        "summary": steps[-1].result,
        "refreshed_at": trace.now_iso(),
    }


@app.get("/methodology/entry-queue")
async def methodology_entry_queue():
    """Full entry-queue waterfall: state → churn → pending_deposits aggregate
    → process_pending_deposits simulation → final drain time.
    """
    ctx = await _gather_state_context()
    pending_agg = await beacon.get_pending_deposits_aggregate()
    pending_full = await beacon.get_pending_deposits()

    ae_churn = spec.get_activation_exit_churn_limit(ctx["total_active_balance_gwei"])
    # For the simulator we need the validator state for any deposit whose pubkey
    # already exists. On mainnet most deposits are new validators (=32 ETH) so the
    # free-pass branches are rare; we skip the validator lookup for performance
    # and acknowledge this in the notes.
    validators_by_pubkey: dict[str, dict] = {}

    deposit_carry = cursors.derive_deposit_carry(pending_agg, ae_churn)

    sim_step = spec.trace_simulate_pending_deposits(
        pending_aggregate=pending_agg,
        pending_deposits=pending_full,
        ae_churn=ae_churn,
        deposit_balance_to_consume=deposit_carry["deposit_balance_to_consume_gwei"],
        finalized_slot=pending_agg["finalized_slot"],
        validators_by_pubkey=validators_by_pubkey,
        current_epoch=ctx["current_epoch"],
    )
    sim_step.notes.append(
        "Approximation — every deposit is treated as the normal path. Checking the free-pass and postpone branches would require a validator lookup for each of the 31k pending pubkeys. On mainnet today those branches shave hours at most, so the drain estimate is a conservative upper bound."
    )

    # Aggregate snapshot step (between churn and simulation).
    deposits_step = trace.TraceStep(
        id="pending-deposits",
        function="state.pending_deposits",
        spec_file="electra/beacon-chain.md",
        spec_lines=(978, 1054),
        spec_excerpt=(
            "GET /eth/v1/beacon/states/head/pending_deposits\n"
            "→ a FIFO queue of deposits awaiting process_pending_deposits"
        ),
        inputs={"finalized_slot": pending_agg["finalized_slot"]},
        substituted=(
            f"queue = {pending_agg['count']:,} entries / "
            f"{pending_agg['total_eth']:,.0f} ETH\n"
            f"finalized = {pending_agg['finalized_count']:,} entries / "
            f"{pending_agg['finalized_eth']:,.0f} ETH (eligible to process)"
        ),
        intermediate={"buckets": pending_agg["buckets"], "first_10": pending_agg["first_10"]},
        result={
            "count": pending_agg["count"],
            "total_eth": pending_agg["total_eth"],
            "finalized_count": pending_agg["finalized_count"],
            "finalized_eth": pending_agg["finalized_eth"],
        },
        notes=[
            f"{pending_agg['unfinalized_count']} deposits not yet finalized "
            f"(behind current finalized epoch {ctx['finalized_epoch']})."
        ],
        refreshed_at=trace.now_iso(),
        provenance="live",
    )

    # Derived deposit_balance_to_consume step.
    dbc_step = trace.TraceStep(
        id="deposit-cursor",
        function="state.deposit_balance_to_consume (derived)",
        spec_file="electra/beacon-chain.md",
        spec_lines=(1050, 1054),
        spec_excerpt=(
            "if is_churn_limit_reached:\n"
            "    state.deposit_balance_to_consume = available_for_processing - processed_amount\n"
            "else:\n"
            "    state.deposit_balance_to_consume = Gwei(0)"
        ),
        substituted=(
            "Under saturated mainnet conditions the cap fires cleanly each epoch, "
            "leaving the carry at 0 ETH. Max error ≤ 1 epoch of churn."
        ),
        result={
            "deposit_balance_to_consume_eth": 0.0,
            "max_error_eth": trace.gwei_to_eth(ae_churn),
        },
        notes=[
            "Not exposed via standard Beacon REST. Approximated as 0 (saturated cap).",
        ],
        refreshed_at=trace.now_iso(),
        provenance="derived-approx",
    )

    epochs_to_drain = sim_step.result["epochs_to_drain"]
    drain_days = sim_step.result["drain_days"]
    last_lands = sim_step.result.get("last_deposit_lands_in_epoch")
    activation_tail_epochs = 1 + 2 + spec.MAX_SEED_LOOKAHEAD + 1  # eligibility+1, finality~2, +5

    final_step = trace.TraceStep(
        id="final",
        function="entry-queue summary",
        spec_file="(derived)",
        spec_lines=(0, 0),
        spec_excerpt=(
            "wait ≈ epochs_to_drain × SECONDS_PER_EPOCH + activation tail\n"
            "activation tail = 1 (eligibility set) + ~2 (finality) + MAX_SEED_LOOKAHEAD + 1 ≈ 8 epochs"
        ),
        substituted=(
            f"queue_drain = {epochs_to_drain:,} epochs ≈ {drain_days:,.1f} days\n"
            f"activation_tail = {activation_tail_epochs} epochs ≈ "
            f"{activation_tail_epochs * spec.SECONDS_PER_EPOCH / 60:.0f} min\n"
            f"→ new 32-ETH deposit at the tail waits ≈ {drain_days:,.1f} days"
        ),
        result={
            "epochs_to_drain": epochs_to_drain,
            "drain_days": drain_days,
            "activation_tail_epochs": activation_tail_epochs,
            "last_deposit_lands_in_epoch": last_lands,
            "current_epoch": ctx["current_epoch"],
        },
        refreshed_at=trace.now_iso(),
        provenance="computed",
    )

    steps = [
        _trace_state_inputs(ctx),
        *_churn_steps(ctx["total_active_balance_gwei"]),
        deposits_step,
        dbc_step,
        sim_step,
        final_step,
    ]

    return {
        "kind": "entry-queue",
        "trace": [s.to_dict() for s in steps],
        "summary": final_step.result,
        "refreshed_at": trace.now_iso(),
    }


@app.get("/methodology/exit-queue")
async def methodology_exit_queue(balance_eth: float = Query(default=32, gt=0, le=2048)):
    """Exit-queue waterfall for a hypothetical exit of `balance_eth` ETH."""
    ctx = await _gather_state_context()
    exiting = await beacon.get_active_exiting_validators()

    ae_churn = spec.get_activation_exit_churn_limit(ctx["total_active_balance_gwei"])
    cursor = cursors.derive_exit_cursor(
        active_exiting_validators=exiting,
        current_epoch=ctx["current_epoch"],
        total_active_balance=ctx["total_active_balance_gwei"],
    )
    exit_balance_gwei = int(balance_eth * spec.GWEI)

    cursor_step = trace.TraceStep(
        id="exit-cursor",
        function="state.earliest_exit_epoch / exit_balance_to_consume (derived)",
        spec_file="electra/beacon-chain.md",
        spec_lines=(770, 793),
        spec_excerpt=(
            "Re-derived by grouping validators with status=active_exiting by exit_epoch\n"
            "and computing the budget remaining in the latest such epoch."
        ),
        substituted=(
            f"exiting validators by epoch: "
            f"{len(cursor['by_epoch'])} distinct exit_epoch buckets\n"
            f"derived earliest_exit_epoch = {cursor['earliest_exit_epoch']:,}\n"
            f"derived exit_balance_to_consume = "
            f"{trace.gwei_to_eth(cursor['exit_balance_to_consume_gwei']):,.2f} ETH"
        ),
        intermediate={
            "by_epoch_first_5": cursor["by_epoch"][:5],
            "total_exiting_buckets": len(cursor["by_epoch"]),
        },
        result={
            "earliest_exit_epoch": cursor["earliest_exit_epoch"],
            "exit_balance_to_consume_eth": trace.gwei_to_eth(cursor["exit_balance_to_consume_gwei"]),
        },
        notes=[
            "earliest_exit_epoch and exit_balance_to_consume are not exposed via standard Beacon REST. "
            "Re-derived from the active_exiting validator set (same pattern beaconcha.in uses)."
        ],
        refreshed_at=trace.now_iso(),
        provenance="derived",
    )

    walk_step = spec.trace_exit_epoch_and_churn(
        earliest_exit_epoch=cursor["earliest_exit_epoch"],
        exit_balance_to_consume=cursor["exit_balance_to_consume_gwei"],
        exit_balance=exit_balance_gwei,
        ae_churn=ae_churn,
        current_epoch=ctx["current_epoch"],
        cursor_provenance="derived",
    )

    exit_epoch = walk_step.result["exit_epoch"]
    withdrawable_epoch = walk_step.result["withdrawable_epoch"]
    final_step = trace.TraceStep(
        id="final",
        function="exit-queue summary",
        spec_file="(derived)",
        spec_lines=(0, 0),
        spec_excerpt=(
            "exit_at_seconds  = GENESIS + exit_epoch × SECONDS_PER_EPOCH\n"
            "withdrawable_at  = GENESIS + (exit_epoch + 256) × SECONDS_PER_EPOCH"
        ),
        substituted=(
            f"exit_epoch          = {exit_epoch:,}\n"
            f"withdrawable_epoch  = {withdrawable_epoch:,} (= exit + 256)\n"
            f"wait_to_exit        ≈ {(exit_epoch - ctx['current_epoch']) * spec.SECONDS_PER_EPOCH / 60:.1f} min\n"
            f"wait_to_withdrawable≈ {(withdrawable_epoch - ctx['current_epoch']) * spec.SECONDS_PER_EPOCH / 3600:.2f} h"
        ),
        result={
            "exit_epoch": exit_epoch,
            "withdrawable_epoch": withdrawable_epoch,
            "wait_to_exit_seconds": (exit_epoch - ctx["current_epoch"]) * spec.SECONDS_PER_EPOCH,
            "wait_to_withdrawable_seconds": (withdrawable_epoch - ctx["current_epoch"]) * spec.SECONDS_PER_EPOCH,
        },
        refreshed_at=trace.now_iso(),
        provenance="computed",
    )

    steps = [
        _trace_state_inputs(ctx),
        *_churn_steps(ctx["total_active_balance_gwei"]),
        spec.trace_activation_exit_epoch(ctx["current_epoch"]),
        cursor_step,
        walk_step,
        final_step,
    ]
    return {
        "kind": "exit-queue",
        "input": {"balance_eth": balance_eth},
        "trace": [s.to_dict() for s in steps],
        "summary": final_step.result,
        "refreshed_at": trace.now_iso(),
    }


@app.get("/methodology/consolidation")
async def methodology_consolidation(balance_eth: float = Query(default=32, gt=0, le=2048)):
    ctx = await _gather_state_context()
    pending = await beacon.get_pending_consolidations()
    cons_churn = spec.get_consolidation_churn_limit(ctx["total_active_balance_gwei"])
    cursor = cursors.derive_consolidation_cursor(
        pending_consolidations=pending,
        current_epoch=ctx["current_epoch"],
        total_active_balance=ctx["total_active_balance_gwei"],
    )
    cons_balance_gwei = int(balance_eth * spec.GWEI)

    cursor_step = trace.TraceStep(
        id="cons-cursor",
        function="state.earliest_consolidation_epoch (derived)",
        spec_file="electra/beacon-chain.md",
        spec_lines=(798, 824),
        spec_excerpt=(
            "Not exposed by Beacon REST. Approximated as the activation/exit floor + full cons_churn."
        ),
        substituted=(
            f"pending consolidations queued: {len(pending):,}\n"
            f"approximated cursor = compute_activation_exit_epoch({ctx['current_epoch']}) = "
            f"{cursor['earliest_consolidation_epoch']}"
        ),
        result={
            "earliest_consolidation_epoch": cursor["earliest_consolidation_epoch"],
            "cons_churn_eth": trace.gwei_to_eth(cursor["cons_churn_gwei"]),
        },
        notes=["Approximation. Recovery requires diffing two consecutive states."],
        refreshed_at=trace.now_iso(),
        provenance="derived-approx",
    )

    walk_step = spec.trace_consolidation_epoch(
        earliest_consolidation_epoch=cursor["earliest_consolidation_epoch"],
        consolidation_balance_to_consume=cursor["consolidation_balance_to_consume_gwei"],
        consolidation_balance=cons_balance_gwei,
        cons_churn=cons_churn,
        current_epoch=ctx["current_epoch"],
        cursor_provenance="derived-approx",
    )
    # Augment with wait timings so the UI can render days+hours from this summary alone.
    cons_ep = walk_step.result.get("consolidation_epoch")
    with_ep = walk_step.result.get("withdrawable_epoch")
    if cons_ep is not None:
        walk_step.result["wait_to_consolidation_seconds"] = (cons_ep - ctx["current_epoch"]) * spec.SECONDS_PER_EPOCH
    if with_ep is not None:
        walk_step.result["wait_to_withdrawable_seconds"] = (with_ep - ctx["current_epoch"]) * spec.SECONDS_PER_EPOCH

    steps = [
        _trace_state_inputs(ctx),
        *_churn_steps(ctx["total_active_balance_gwei"]),
        spec.trace_activation_exit_epoch(ctx["current_epoch"]),
        cursor_step,
        walk_step,
    ]
    return {
        "kind": "consolidation",
        "input": {"balance_eth": balance_eth},
        "trace": [s.to_dict() for s in steps],
        "summary": walk_step.result,
        "refreshed_at": trace.now_iso(),
    }


@app.get("/methodology/partial-withdrawal")
async def methodology_partial_withdrawal(amount_eth: float = Query(default=1, gt=0)):
    """Partial withdrawals are rate-limited by the withdrawal sweep, not the
    activation/exit churn budget. Different model.
    """
    ctx = await _gather_state_context()
    partials = await beacon.get_pending_partial_withdrawals()

    # Per spec, MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP = 8 per payload.
    # Combined with MAX_WITHDRAWALS_PER_PAYLOAD = 16 (which also runs the
    # full-withdrawal sweep), 8 is the per-block ceiling.
    rate_per_epoch = spec.MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP * spec.SLOTS_PER_EPOCH

    queue_step = trace.TraceStep(
        id="pending-partials",
        function="state.pending_partial_withdrawals",
        spec_file="electra/beacon-chain.md",
        spec_lines=(1222, 1377),
        spec_excerpt=(
            "MAX_PENDING_PARTIALS_PER_WITHDRAWALS_SWEEP = 8 per payload\n"
            "Sweep runs every block (32 blocks per epoch) — at most 256 partials/epoch.\n"
            "FIFO; entry must satisfy withdrawable_epoch ≤ current_epoch to process."
        ),
        substituted=(
            f"queue length              = {len(partials):,}\n"
            f"max partials per block    = 8\n"
            f"max partials per epoch    = 8 × 32 = {rate_per_epoch}\n"
            f"epochs to drain queue     ≈ {len(partials) / rate_per_epoch:,.1f}"
        ),
        result={
            "queue_length": len(partials),
            "rate_per_epoch": rate_per_epoch,
            "epochs_to_drain": round(len(partials) / rate_per_epoch, 2) if rate_per_epoch else None,
        },
        notes=[
            "Partial withdrawals are not churn-gated. They share the per-payload sweep cap "
            "with the full-withdrawal sweep, so real throughput can be lower than the 256/epoch ceiling.",
        ],
        refreshed_at=trace.now_iso(),
        provenance="live",
    )

    # Eligibility delay: a freshly submitted partial waits 256 epochs.
    eligible_epoch = ctx["current_epoch"] + spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY
    delay_step = trace.TraceStep(
        id="withdrawability-delay",
        function="MIN_VALIDATOR_WITHDRAWABILITY_DELAY",
        spec_file="electra/beacon-chain.md",
        spec_lines=(1, 1),
        spec_excerpt=(
            "On EL withdrawal request:\n"
            "  state.pending_partial_withdrawals.append(PendingPartialWithdrawal(\n"
            "      validator_index=..., amount=...,\n"
            "      withdrawable_epoch=Epoch(get_current_epoch(state) + MIN_VALIDATOR_WITHDRAWABILITY_DELAY)\n"
            "  ))\n"
            "→ entry is ineligible for the sweep until epoch ≥ withdrawable_epoch."
        ),
        substituted=(
            f"new request submitted at epoch {ctx['current_epoch']:,}\n"
            f"withdrawable_epoch = {ctx['current_epoch']:,} + {spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY} = {eligible_epoch:,}\n"
            f"≈ {spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY * spec.SECONDS_PER_EPOCH / 3600:.2f}h after request"
        ),
        result={
            "withdrawable_epoch": eligible_epoch,
            "delay_epochs": spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY,
            "delay_seconds": spec.MIN_VALIDATOR_WITHDRAWABILITY_DELAY * spec.SECONDS_PER_EPOCH,
        },
        refreshed_at=trace.now_iso(),
        provenance="constant",
    )

    # Sweep position: appended to the tail of the current queue.
    new_position = len(partials)
    sweep_offset_epochs = new_position // rate_per_epoch if rate_per_epoch else 0
    process_epoch = eligible_epoch + sweep_offset_epochs
    wait_step = trace.TraceStep(
        id="new-partial-wait",
        function="eligible_epoch + sweep position",
        spec_file="(derived)",
        spec_lines=(0, 0),
        spec_excerpt=(
            "sweep_offset_epochs = floor(queue_position / rate_per_epoch)\n"
            "process_epoch = withdrawable_epoch + sweep_offset_epochs"
        ),
        substituted=(
            f"queue position             = {new_position:,}\n"
            f"sweep_offset_epochs        = {new_position:,} ÷ {rate_per_epoch} = {sweep_offset_epochs:,}\n"
            f"process_epoch              = {eligible_epoch:,} + {sweep_offset_epochs:,} = {process_epoch:,}\n"
            f"wait from now              ≈ {(process_epoch - ctx['current_epoch']) * spec.SECONDS_PER_EPOCH / 3600:,.2f}h"
        ),
        result={
            "process_epoch": process_epoch,
            "wait_epochs": process_epoch - ctx["current_epoch"],
            "wait_seconds": (process_epoch - ctx["current_epoch"]) * spec.SECONDS_PER_EPOCH,
            "rate_per_epoch": rate_per_epoch,
        },
        notes=[
            f"Amount ({amount_eth} ETH) doesn't affect wait — sweep position is FIFO by request order."
        ],
        refreshed_at=trace.now_iso(),
        provenance="computed",
    )

    steps = [
        _trace_state_inputs(ctx),
        queue_step,
        delay_step,
        wait_step,
    ]
    return {
        "kind": "partial-withdrawal",
        "input": {"amount_eth": amount_eth},
        "trace": [s.to_dict() for s in steps],
        "summary": wait_step.result,
        "refreshed_at": trace.now_iso(),
    }


# Mount the static dashboard at "/" — FastAPI dispatches declared routes first,
# so the API endpoints above still win for their paths.
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
