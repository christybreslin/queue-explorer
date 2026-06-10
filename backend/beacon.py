import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

BEACON_URL = os.environ.get("BEACON_URL", "https://hoodi-user-cl.attestant.io")
BEACON_TOKEN = os.environ.get("BEACON_TOKEN", "")
TIMEOUT = 60.0

# ---------------------------------------------------------------------------
# Simple async TTL cache
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}


def _get_cached(key: str, ttl: float) -> Any | None:
    """Return cached value if it exists and hasn't expired, else None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > ttl:
        del _cache[key]
        return None
    return value


def _set_cached(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# TTLs in seconds
TTL_HEAD = 6          # new slot every 12s
TTL_CONFIG = 3600     # chain config almost never changes
TTL_VALIDATORS = 120  # validator sets shift per epoch (~6.4 min)
TTL_WITHDRAWALS = 120


def _headers() -> dict[str, str]:
    if BEACON_TOKEN:
        return {"Authorization": f"Bearer {BEACON_TOKEN}"}
    return {}


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=BEACON_URL, timeout=TIMEOUT) as client:
        resp = await client.get(path, params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_active_exiting_validators(state_id: str = "head") -> list[dict]:
    key = f"active_exiting:{state_id}"
    cached = _get_cached(key, TTL_VALIDATORS)
    if cached is not None:
        return cached
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/validators",
        params={"status": "active_exiting"},
    )
    result = data.get("data", [])
    if state_id == "head":
        _set_cached(key, result)
    return result



async def get_active_validator_summary(state_id: str = "head") -> dict:
    """Aggregate stats over all active validators (active_ongoing + exiting + slashed).

    Returns {count, total_balance_gwei, compounding_count}. Powers /network/stats
    and is the single source for get_total_active_balance.
    """
    key = f"active_summary:{state_id}"
    cached = _get_cached(key, TTL_VALIDATORS)
    if cached is not None:
        return cached
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/validators",
        params={"status": "active_ongoing,active_exiting,active_slashed"},
    )
    validators = data.get("data", [])
    total_balance = 0
    compounding_count = 0
    for v in validators:
        vd = v["validator"]
        total_balance += int(vd["effective_balance"])
        creds = vd.get("withdrawal_credentials", "")
        if creds.lower().startswith("0x02"):
            compounding_count += 1
    result = {
        "count": len(validators),
        "total_balance_gwei": total_balance,
        "compounding_count": compounding_count,
    }
    if state_id == "head":
        _set_cached(key, result)
    return result


async def get_total_active_balance(state_id: str = "head") -> int:
    """Sum effective_balance over all active validators.

    Per the consensus spec, is_active_validator iff
        activation_epoch <= current_epoch < exit_epoch
    which means active_ongoing + active_exiting + active_slashed all qualify.
    Thin wrapper around get_active_validator_summary to share the cached fetch.
    """
    summary = await get_active_validator_summary(state_id)
    return summary["total_balance_gwei"]


async def get_validator(state_id: str, validator_id: str) -> dict:
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/validators/{validator_id}"
    )
    return data.get("data", {})



async def get_config() -> dict:
    key = "config"
    cached = _get_cached(key, TTL_CONFIG)
    if cached is not None:
        return cached
    data = await _get("/eth/v1/config/spec")
    result = data.get("data", {})
    _set_cached(key, result)
    return result


async def get_head_slot() -> int:
    key = "head_slot"
    cached = _get_cached(key, TTL_HEAD)
    if cached is not None:
        return cached
    data = await _get("/eth/v1/beacon/headers/head")
    result = int(data["data"]["header"]["message"]["slot"])
    _set_cached(key, result)
    return result


async def get_pending_consolidations(state_id: str = "head") -> list[dict]:
    key = f"pending_consolidations:{state_id}"
    cached = _get_cached(key, TTL_WITHDRAWALS)
    if cached is not None:
        return cached
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/pending_consolidations"
    )
    result = data.get("data", [])
    if state_id == "head":
        _set_cached(key, result)
    return result


async def get_pending_partial_withdrawals(state_id: str = "head") -> list[dict]:
    key = f"pending_withdrawals:{state_id}"
    cached = _get_cached(key, TTL_WITHDRAWALS)
    if cached is not None:
        return cached
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/pending_partial_withdrawals"
    )
    result = data.get("data", [])
    if state_id == "head":
        _set_cached(key, result)
    return result


# ---------------------------------------------------------------------------
# Methodology helpers
# ---------------------------------------------------------------------------

GWEI = 1_000_000_000


async def get_pending_deposits(state_id: str = "head") -> list[dict]:
    """Full pending_deposits list. WARNING: ~14 MB on mainnet. Cached 60 s."""
    key = f"pending_deposits_full:{state_id}"
    cached = _get_cached(key, TTL_WITHDRAWALS)
    if cached is not None:
        return cached
    data = await _get(f"/eth/v1/beacon/states/{state_id}/pending_deposits")
    result = data.get("data", [])
    if state_id == "head":
        _set_cached(key, result)
    return result


async def find_pending_deposit_by_pubkey(pubkey: str, state_id: str = "head") -> dict | None:
    """Search state.pending_deposits for a deposit matching pubkey.

    Returns {deposit, position, queue_total, ahead_gwei} or None.

    `ahead_gwei` is the sum of `amount` over every deposit strictly ahead of
    this one — the actual ETH the churn budget needs to clear before reaching
    this deposit. Avoids the wildly-wrong "position × 32 ETH" approximation.

    Note: hits the full pending_deposits list (~14 MB on mainnet). Cached 60 s.
    """
    target = pubkey.strip().lower()
    if target.startswith("0x"):
        target_no_prefix = target[2:]
    else:
        target_no_prefix = target
        target = "0x" + target
    deposits = await get_pending_deposits(state_id)
    ahead_gwei = 0
    for i, d in enumerate(deposits):
        pk = (d.get("pubkey") or "").lower()
        pk_no_prefix = pk[2:] if pk.startswith("0x") else pk
        if pk == target or pk_no_prefix == target_no_prefix:
            return {
                "deposit": d,
                "position": i + 1,
                "queue_total": len(deposits),
                "ahead_gwei": ahead_gwei,
            }
        try:
            ahead_gwei += int(d.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    return None


async def get_pending_deposits_aggregate(state_id: str = "head") -> dict:
    """Server-side fold of pending_deposits into a small summary.

    Never let the 14 MB list cross the wire to the browser; this is what the
    methodology endpoint returns instead.
    """
    key = f"pending_deposits_agg:{state_id}"
    cached = _get_cached(key, TTL_WITHDRAWALS)
    if cached is not None:
        return cached

    deposits = await get_pending_deposits(state_id)
    finalized_epoch = await get_finalized_checkpoint_epoch(state_id)
    finalized_slot = finalized_epoch * 32

    total_gwei = 0
    finalized_count = 0
    finalized_gwei = 0
    buckets = {"<32": 0, "=32": 0, "33-99": 0, "100-499": 0, "500-2047": 0, ">=2048": 0}
    bucket_eth = {k: 0 for k in buckets}
    for d in deposits:
        amount = int(d["amount"])
        total_gwei += amount
        eth = amount // GWEI
        if eth < 32:
            buckets["<32"] += 1; bucket_eth["<32"] += amount
        elif eth == 32:
            buckets["=32"] += 1; bucket_eth["=32"] += amount
        elif eth < 100:
            buckets["33-99"] += 1; bucket_eth["33-99"] += amount
        elif eth < 500:
            buckets["100-499"] += 1; bucket_eth["100-499"] += amount
        elif eth < 2048:
            buckets["500-2047"] += 1; bucket_eth["500-2047"] += amount
        else:
            buckets[">=2048"] += 1; bucket_eth[">=2048"] += amount
        if int(d["slot"]) <= finalized_slot:
            finalized_count += 1
            finalized_gwei += amount

    result = {
        "count": len(deposits),
        "total_gwei": total_gwei,
        "total_eth": round(total_gwei / GWEI, 4),
        "finalized_count": finalized_count,
        "finalized_gwei": finalized_gwei,
        "finalized_eth": round(finalized_gwei / GWEI, 4),
        "unfinalized_count": len(deposits) - finalized_count,
        "buckets": {k: {"count": v, "eth": round(bucket_eth[k] / GWEI, 2)} for k, v in buckets.items()},
        "first_10": [
            {
                "pubkey": d["pubkey"][:14] + "…",
                "amount_eth": round(int(d["amount"]) / GWEI, 4),
                "slot": int(d["slot"]),
                "finalized": int(d["slot"]) <= finalized_slot,
            }
            for d in deposits[:10]
        ],
        "finalized_slot": finalized_slot,
    }
    if state_id == "head":
        _set_cached(key, result)
    return result


async def get_finalized_checkpoint_epoch(state_id: str = "head") -> int:
    key = f"finalized:{state_id}"
    cached = _get_cached(key, TTL_HEAD)
    if cached is not None:
        return cached
    data = await _get(f"/eth/v1/beacon/states/{state_id}/finality_checkpoints")
    result = int(data["data"]["finalized"]["epoch"])
    if state_id == "head":
        _set_cached(key, result)
    return result


async def get_fork(state_id: str = "head") -> dict:
    key = f"fork:{state_id}"
    cached = _get_cached(key, TTL_CONFIG)
    if cached is not None:
        return cached
    data = await _get(f"/eth/v1/beacon/states/{state_id}/fork")
    result = data["data"]
    if state_id == "head":
        _set_cached(key, result)
    return result


async def get_validators_by_pubkey(pubkeys: list[str], state_id: str = "head") -> dict[str, dict]:
    """Fetch a batch of validators and index by pubkey. Used by the deposit
    simulator to look up target validators for the free-pass branches."""
    if not pubkeys:
        return {}
    # POST endpoint takes a list of ids
    data = await _get(
        f"/eth/v1/beacon/states/{state_id}/validators",
        params={"id": ",".join(pubkeys)},
    )
    result = {}
    for v in data.get("data", []):
        pk = v["validator"]["pubkey"]
        result[pk] = v["validator"]
    return result
