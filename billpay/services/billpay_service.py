from dataclasses import dataclass
from typing import List, Optional, Protocol, Set, Tuple

from billpay.config import PricingConfig, RiskConfig
from billpay.fee_engine import compute_credit_card_fee
from billpay.integrations.email_provider import InvoiceProvider
from billpay.models import ChargeResult, Customer, Invoice, PayoutResult


class CardGateway(Protocol):
    def charge(
        self,
        card_token: str,
        total_amount: float,
        fee_amount: float,
        *,
        stripe_customer_id: Optional[str] = None,
    ) -> ChargeResult: ...


class PayoutGateway(Protocol):
    def send_invoice_payment(self, destination: str, amount: float) -> PayoutResult: ...


@dataclass
class BillPayOutcome:
    ok: bool
    message: str
    invoice_amount: float = 0.0
    fee_amount: float = 0.0
    total_charged: float = 0.0
    charge_ref: str = ""
    ach_ref: str = ""
    gmail_message_id: Optional[str] = None
    invoice_number: Optional[str] = None


@dataclass
class BillPayBatchResult:
    """One entry per unpaid invoice found in this run (oldest first from the provider)."""

    results: List[Tuple[Invoice, BillPayOutcome]]


class BillPayService:
    def __init__(
        self,
        email_provider: InvoiceProvider,
        card_gateway: CardGateway,
        ach_gateway: PayoutGateway,
        pricing_cfg: PricingConfig,
        risk_cfg: RiskConfig,
    ) -> None:
        self.email_provider = email_provider
        self.card_gateway = card_gateway
        self.ach_gateway = ach_gateway
        self.pricing_cfg = pricing_cfg
        self.risk_cfg = risk_cfg

    def process_all_unpaid_invoices(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
    ) -> BillPayBatchResult:
        """
        Fetch **all** unpaid invoices (typically oldest first), charge + payout each.
        """
        invoices = self.email_provider.fetch_unpaid_invoices(
            customer,
            skip_message_ids=skip_message_ids,
            skip_invoice_numbers=skip_invoice_numbers,
            newest_first=False,
        )
        results: List[Tuple[Invoice, BillPayOutcome]] = []
        for inv in invoices:
            results.append((inv, self._process_invoice(customer, inv)))
        return BillPayBatchResult(results=results)

    def run_for_customer(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
    ) -> BillPayOutcome:
        """Process a single invoice: the **newest** unpaid one (backward compatible)."""
        invoice = self.email_provider.fetch_latest_invoice(
            customer,
            skip_message_ids=skip_message_ids,
            skip_invoice_numbers=skip_invoice_numbers,
        )
        if not invoice:
            return BillPayOutcome(
                ok=False,
                message="No unpaid invoice found (or nothing matches Invoice_<num>_<name>).",
            )
        return self._process_invoice(customer, invoice)

    def _process_invoice(self, customer: Customer, invoice: Invoice) -> BillPayOutcome:
        inv_no = invoice.invoice_number or invoice.invoice_id

        if invoice.amount > self.risk_cfg.max_invoice_amount:
            return BillPayOutcome(
                ok=False,
                message=f"Invoice amount exceeds risk limit ({self.risk_cfg.max_invoice_amount:,.2f}).",
                gmail_message_id=invoice.gmail_message_id,
                invoice_number=inv_no,
            )

        fee = compute_credit_card_fee(invoice.amount, customer.card_plan, self.pricing_cfg)
        total_to_charge = round(invoice.amount + fee, 2)
        charge_result = self.card_gateway.charge(
            customer.card_token,
            total_to_charge,
            fee,
            stripe_customer_id=customer.stripe_customer_id,
        )
        if not charge_result.success:
            return BillPayOutcome(
                ok=False,
                message=f"Charge failed: {charge_result.error}",
                gmail_message_id=invoice.gmail_message_id,
                invoice_number=inv_no,
            )

        ach_result = self.ach_gateway.send_invoice_payment(invoice.platform, invoice.amount)
        if not ach_result.success:
            return BillPayOutcome(
                ok=False,
                message=f"Meta / vendor payout failed: {ach_result.error}",
                gmail_message_id=invoice.gmail_message_id,
                invoice_number=inv_no,
            )

        return BillPayOutcome(
            ok=True,
            message="BillPay flow completed successfully.",
            invoice_amount=invoice.amount,
            fee_amount=fee,
            total_charged=total_to_charge,
            charge_ref=charge_result.reference,
            ach_ref=ach_result.reference,
            gmail_message_id=invoice.gmail_message_id,
            invoice_number=inv_no,
        )
