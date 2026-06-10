from datetime import datetime, timezone

GWEI = 1_000_000_000
SLOTS_PER_EPOCH = 32
SECONDS_PER_SLOT = 12
SECONDS_PER_EPOCH = SLOTS_PER_EPOCH * SECONDS_PER_SLOT  # 384 seconds = 6.4 min
GENESIS_TIME = 1606824023  # Mainnet beacon chain genesis

# Electra defaults (overridden by on-chain config when available)
MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA = 128 * GWEI  # 128 ETH in gwei
MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT = 256 * GWEI  # 256 ETH in gwei
CHURN_LIMIT_QUOTIENT = 65536
MIN_VALIDATOR_WITHDRAWABILITY_DELAY = 256  # epochs


def compute_churn_limit(total_active_balance: int, config: dict) -> int:
    min_churn = int(config.get(
        "MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA",
        MIN_PER_EPOCH_CHURN_LIMIT_ELECTRA,
    ))
    max_churn = int(config.get(
        "MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT",
        MAX_PER_EPOCH_ACTIVATION_EXIT_CHURN_LIMIT,
    ))
    quotient = int(config.get("CHURN_LIMIT_QUOTIENT", CHURN_LIMIT_QUOTIENT))
    return min(max_churn, max(min_churn, total_active_balance // quotient))


def slot_to_epoch(slot: int) -> int:
    return slot // SLOTS_PER_EPOCH


def epoch_to_timestamp(epoch: int) -> float:
    return GENESIS_TIME + epoch * SECONDS_PER_EPOCH


def epoch_to_datetime(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch_to_timestamp(epoch), tz=timezone.utc)


def epochs_to_hours(n_epochs: int) -> float:
    return round(n_epochs * SECONDS_PER_EPOCH / 3600, 2)


def build_exit_queue(
    exiting_validators: list[dict],
) -> dict[int, dict]:
    """Group exiting validators by exit_epoch, returning {epoch: {count, balance}}."""
    queue: dict[int, dict] = {}
    for v in exiting_validators:
        epoch = int(v["validator"]["exit_epoch"])
        bal = int(v["validator"]["effective_balance"])
        if epoch not in queue:
            queue[epoch] = {"count": 0, "balance": 0}
        queue[epoch]["count"] += 1
        queue[epoch]["balance"] += bal
    return dict(sorted(queue.items()))


def estimate_exit_epoch(
    queue: dict[int, dict],
    churn_limit: int,
    exit_balance: int,
    pending_partials: list[dict] | None = None,
    current_epoch: int = 0,
    is_partial: bool = False,
) -> int:
    """Walk the queue to find which epoch a new exit/withdrawal would land in.

    The churn limit is a per-epoch budget (in gwei). Each epoch can process up to
    churn_limit gwei worth of exits + partial withdrawals.

    For full exits: scan from current_epoch for first epoch with capacity.
    For partial withdrawals: scan from the last pending partial's epoch
    (new partials are appended to the FIFO queue, can't jump ahead).

    Returns an absolute epoch number.
    """
    # Build a map of churn consumed per epoch (from full exits)
    consumed: dict[int, int] = {}
    for epoch, info in queue.items():
        consumed[epoch] = consumed.get(epoch, 0) + info["balance"]

    # Track the latest pending partial epoch before adding to consumed map
    last_partial_epoch = 0
    if pending_partials:
        for pw in pending_partials:
            epoch = int(pw.get("withdrawable_epoch", 0))
            amount = int(pw.get("amount", 0))
            consumed[epoch] = consumed.get(epoch, 0) + amount
            if epoch > last_partial_epoch:
                last_partial_epoch = epoch

    if not consumed:
        return max(current_epoch, 1)

    # For partial withdrawals, start from the last pending partial's epoch
    # since new partials are queued after existing ones
    start_epoch = max(current_epoch, 1)
    if is_partial and last_partial_epoch > 0:
        start_epoch = max(start_epoch, last_partial_epoch)

    last_epoch = max(consumed.keys())
    epoch = start_epoch
    balance_left = exit_balance
    while epoch <= last_epoch:
        used = consumed.get(epoch, 0)
        available = churn_limit - used
        if available >= balance_left:
            return epoch
        if available > 0:
            balance_left -= available
        epoch += 1

    # Past all consumed epochs, full budget available each epoch
    while balance_left > churn_limit:
        balance_left -= churn_limit
        epoch += 1
    return epoch
