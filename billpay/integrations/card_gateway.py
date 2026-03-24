import uuid

from typing import Optional

from billpay.models import ChargeResult


class MockCardGateway:
    """Simulates charging a card token via a PSP like Stripe."""

    def charge(
        self,
        card_token: str,
        total_amount: float,
        fee_amount: float,
        *,
        stripe_customer_id: Optional[str] = None,
    ) -> ChargeResult:
        if not card_token.startswith("card_"):
            return ChargeResult(
                success=False,
                amount=round(total_amount - fee_amount, 2),
                fee=round(fee_amount, 2),
                total_charged=round(total_amount, 2),
                reference="",
                error="Invalid card token format.",
            )

        reference = f"ch_{uuid.uuid4().hex[:12]}"
        return ChargeResult(
            success=True,
            amount=round(total_amount - fee_amount, 2),
            fee=round(fee_amount, 2),
            total_charged=round(total_amount, 2),
            reference=reference,
        )
