"""
Stripe integrations:
- Charge the end customer via PaymentIntent with **reused** Customer + PaymentMethod
  (create + attach once; set ``STRIPE_CUSTOMER_ID`` + ``STRIPE_PAYMENT_METHOD_ID``).
- Move invoice principal to a Connect connected account.

Requires env:
  STRIPE_SECRET_KEY
  STRIPE_CUSTOMER_ID — cus_... (PaymentIntent.customer)
  STRIPE_PAYMENT_METHOD_ID — pm_... attached to that customer
  STRIPE_META_CONNECTED_ACCOUNT — acct_... for invoice principal Transfer

One-time setup: ``python3 create_stripe.py onboard --email you@example.com``
"""

from __future__ import annotations

import os
from typing import Optional

import stripe  # type: ignore[reportMissingImports]

from billpay.models import ChargeResult, PayoutResult


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _platform_usd_available_cents() -> int:
    """Sum of Stripe *platform* balance.available entries in USD (for Connect Transfers)."""
    bal = stripe.Balance.retrieve()
    total = 0
    for row in bal.available or []:
        if getattr(row, "currency", None) == "usd":
            total += int(row.amount or 0)
    return total


_CONNECT_BALANCE_HINT = (
    " In test mode: add platform *available* balance with test card 4000000000000077 "
    "(see https://stripe.com/docs/testing#available-balance), or set "
    "BILLPAY_MOCK_CONNECT_TRANSFER=1 / run main.py with --mock-connect-transfer to simulate Transfers."
)


def _attach_pm_if_needed(pm_id: str, customer_id: str) -> None:
    try:
        stripe.PaymentMethod.attach(pm_id, customer=customer_id)
    except stripe.error.InvalidRequestError as exc:
        msg = str(exc).lower()
        code = getattr(exc, "code", None) or ""
        if code == "resource_already_exists" or "already been attached" in msg:
            return
        raise


class StripeCardGateway:
    """Charge total (invoice + fee) with saved Customer + PaymentMethod (reuse)."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise ValueError("STRIPE_SECRET_KEY is not set.")
        stripe.api_key = key

    def charge(
        self,
        card_token: str,
        total_amount: float,
        fee_amount: float,
        *,
        stripe_customer_id: Optional[str] = None,
    ) -> ChargeResult:
        if not card_token.startswith("pm_"):
            return ChargeResult(
                success=False,
                amount=round(total_amount - fee_amount, 2),
                fee=round(fee_amount, 2),
                total_charged=round(total_amount, 2),
                reference="",
                error="card_token must be a Stripe PaymentMethod id (pm_...).",
            )

        cus = (stripe_customer_id or os.environ.get("STRIPE_CUSTOMER_ID") or "").strip()
        if not cus.startswith("cus_"):
            return ChargeResult(
                success=False,
                amount=round(total_amount - fee_amount, 2),
                fee=round(fee_amount, 2),
                total_charged=round(total_amount, 2),
                reference="",
                error="STRIPE_CUSTOMER_ID must be set to cus_... (run create_stripe.py onboard).",
            )

        try:
            _attach_pm_if_needed(card_token, cus)
            intent = stripe.PaymentIntent.create(
                amount=_cents(total_amount),
                currency="usd",
                customer=cus,
                payment_method=card_token,
                payment_method_types=["card"],
                confirm=True,
                metadata={
                    "fee_component_usd": str(round(fee_amount, 2)),
                    "invoice_component_usd": str(round(total_amount - fee_amount, 2)),
                },
            )
            if intent.status != "succeeded":
                return ChargeResult(
                    success=False,
                    amount=round(total_amount - fee_amount, 2),
                    fee=round(fee_amount, 2),
                    total_charged=round(total_amount, 2),
                    reference=intent.id,
                    error=f"PaymentIntent status: {intent.status}",
                )
            return ChargeResult(
                success=True,
                amount=round(total_amount - fee_amount, 2),
                fee=round(fee_amount, 2),
                total_charged=round(total_amount, 2),
                reference=intent.id,
            )
        except stripe.error.StripeError as exc:
            return ChargeResult(
                success=False,
                amount=round(total_amount - fee_amount, 2),
                fee=round(fee_amount, 2),
                total_charged=round(total_amount, 2),
                reference="",
                error=str(exc.user_message or exc),
            )


class StripeConnectPayoutGateway:
    """
    Send the *invoice principal* (not your fee) to a Connect account.

    This models routing funds toward ad-platform settlement; Meta does not give you
    a Stripe Connect account — you’d map this to your operational bank / ACH product.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        destination_account: Optional[str] = None,
    ) -> None:
        key = api_key or os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise ValueError("STRIPE_SECRET_KEY is not set.")
        stripe.api_key = key
        self.destination = (destination_account or "").strip() or (
            os.environ.get("STRIPE_META_CONNECTED_ACCOUNT") or ""
        ).strip()
        if not self.destination:
            raise ValueError(
                "Set STRIPE_META_CONNECTED_ACCOUNT to a Connect account id (acct_...), e.g.\n"
                "  export STRIPE_META_CONNECTED_ACCOUNT=acct_..."
            )

    def send_invoice_payment(self, destination: str, amount: float) -> PayoutResult:
        _ = destination  # platform label (e.g. Meta); funds go to configured Connect account
        if amount <= 0:
            return PayoutResult(
                success=False,
                amount=amount,
                reference="",
                error="Amount must be positive.",
            )
        need = _cents(amount)
        try:
            available = _platform_usd_available_cents()
            if available < need:
                return PayoutResult(
                    success=False,
                    amount=round(amount, 2),
                    reference="",
                    error=(
                        f"Skipping Stripe Transfer: platform available balance is "
                        f"${available / 100:.2f} USD but this payout needs ${amount:.2f} USD."
                        + _CONNECT_BALANCE_HINT
                    ),
                )
            transfer = stripe.Transfer.create(
                amount=need,
                currency="usd",
                destination=self.destination,
                metadata={"ad_platform": destination},
            )
            return PayoutResult(
                success=True,
                amount=round(amount, 2),
                reference=transfer.id,
            )
        except stripe.error.StripeError as exc:
            msg = str(exc.user_message or exc)
            if getattr(exc, "code", None) == "balance_insufficient":
                msg = msg.rstrip(".") + "." + _CONNECT_BALANCE_HINT
            return PayoutResult(
                success=False,
                amount=round(amount, 2),
                reference="",
                error=msg,
            )
