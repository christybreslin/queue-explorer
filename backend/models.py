from pydantic import BaseModel


class EpochBreakdown(BaseModel):
    epochs_from_now: int
    validator_count: int
    total_balance_gwei: int
    total_balance_eth: float


class ExitQueueResponse(BaseModel):
    current_epoch: int
    churn_limit_gwei: int
    total_exiting_validators: int
    total_exiting_balance_gwei: int
    total_exiting_balance_eth: float
    queue_depth_epochs: int
    estimated_wait_hours: float
    per_epoch: list[EpochBreakdown]


class ExitQueueHistoryEntry(BaseModel):
    epoch: int
    exiting_validators: int
    total_balance_gwei: int


class ExitQueueHistoryResponse(BaseModel):
    current_epoch: int
    entries: list[ExitQueueHistoryEntry]


class ValidatorStatusResponse(BaseModel):
    index: str | None = None       # null for pending-deposit entries (no validator yet)
    pubkey: str
    status: str
    balance_gwei: int
    balance_eth: float
    effective_balance_gwei: int
    credential_type: str            # "compounding", "execution", or "bls"
    exit_epoch: str | None = None
    withdrawable_epoch: str | None = None
    estimated_exit_time: str | None = None
    estimated_wait_hours: float | None = None
    # Pending-deposit fallback — populated when the pubkey is in pending_deposits
    # but no Validator object exists yet. Frontend renders a pre-validator row.
    is_pending_deposit: bool = False
    pending_deposit_position: int | None = None
    pending_deposit_queue_total: int | None = None
    pending_deposit_amount_eth: float | None = None
    pending_deposit_ahead_eth: float | None = None
    pending_deposit_eta_seconds: int | None = None
    pending_deposit_slot: int | None = None


class ExitPredictionResponse(BaseModel):
    balance_gwei: int
    balance_eth: float
    predicted_exit_epoch: int
    estimated_wait_epochs: int
    estimated_wait_hours: float
    withdrawable_epoch: int
    estimated_withdrawable_hours: float
    current_epoch: int
    churn_limit_gwei: int


class PartialWithdrawalPredictionResponse(BaseModel):
    amount_gwei: int
    amount_eth: float
    predicted_epoch: int
    estimated_wait_epochs: int
    estimated_wait_hours: float
    withdrawable_epoch: int
    estimated_withdrawable_hours: float
    current_epoch: int
    churn_limit_gwei: int


class PendingPartialWithdrawal(BaseModel):
    validator_index: str
    amount_gwei: int
    amount_eth: float
    withdrawable_epoch: int
    withdrawable_time: str


class PendingPartialWithdrawalsResponse(BaseModel):
    count: int
    total_amount_gwei: int
    total_amount_eth: float
    withdrawals: list[PendingPartialWithdrawal]


class ConsolidationSource(BaseModel):
    index: str
    balance_eth: float
    effective_balance_eth: float
    status: str


class ConsolidationTarget(BaseModel):
    target_index: str
    target_pubkey: str
    target_status: str
    target_balance_eth: float
    target_effective_balance_eth: float
    source_count: int
    sources: list[ConsolidationSource]
    total_incoming_eth: float


class ConsolidationsResponse(BaseModel):
    count: int
    target_count: int
    total_eth: float
    targets: list[ConsolidationTarget]


class BatchValidatorsRequest(BaseModel):
    validators: list[str]


class BatchValidatorsResponse(BaseModel):
    validators: list[ValidatorStatusResponse]
    errors: list[str]


class ChurnResponse(BaseModel):
    churn_limit_gwei: int
    churn_limit_eth: float
    total_active_balance_gwei: int
    total_active_balance_eth: float
    min_per_epoch_churn_limit: int
    max_per_epoch_churn_limit: int
    churn_limit_quotient: int
    explanation: str
