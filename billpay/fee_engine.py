from billpay.config import PricingConfig
from billpay.models import CardPlan


def compute_credit_card_fee(invoice_amount: float, card_plan: CardPlan, cfg: PricingConfig) -> float:
    """
    Fee includes Stripe cost plus product margin:
    - our card:      3.0%
    - external card: 3.5%
    """
    if invoice_amount <= 0:
        raise ValueError("Invoice amount must be greater than 0.")

    if card_plan == CardPlan.OUR_CARD:
        total_percent = cfg.fee_our_card
    elif card_plan == CardPlan.EXTERNAL_CARD:
        total_percent = cfg.fee_external_card
    else:
        raise ValueError(f"Unsupported card plan: {card_plan}")

    return round(invoice_amount * total_percent, 2)
