from dataclasses import dataclass


@dataclass(frozen=True)
class PricingConfig:
    stripe_percent: float = 0.029
    fee_our_card: float = 0.03
    fee_external_card: float = 0.035


@dataclass(frozen=True)
class RiskConfig:
    max_invoice_amount: float = 5_000_000.00
