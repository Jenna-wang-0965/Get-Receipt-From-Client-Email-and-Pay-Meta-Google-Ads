from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple


class CardPlan(str, Enum):
    """Card pricing category for fee calculation."""

    OUR_CARD = "our_card"
    EXTERNAL_CARD = "external_card"


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    has_email_access: bool
    card_plan: CardPlan
    card_token: str
    # Stripe: STRIPE_CUSTOMER_ID (cus_...) for PaymentIntent.customer + attached pm_...
    stripe_customer_id: Optional[str] = None
    # Extra tokens allowed in ``Invoice_*_<CustomerName>.pdf`` (e.g. brand vs legal name).
    invoice_name_aliases: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Invoice:
    invoice_id: str
    platform: str
    amount: float
    due_date: datetime
    source_email: str
    pdf_path: Optional[str] = None
    # Parsed from attachment name: Invoice_<InvoiceNumber>_<CustomerName>.pdf
    invoice_number: Optional[str] = None
    customer_name_on_invoice_file: Optional[str] = None
    # Gmail message id when sourced from API (used to dedupe polling runs).
    gmail_message_id: Optional[str] = None


@dataclass
class ChargeResult:
    success: bool
    amount: float
    fee: float
    total_charged: float
    reference: str
    error: Optional[str] = None


@dataclass
class PayoutResult:
    success: bool
    amount: float
    reference: str
    error: Optional[str] = None
