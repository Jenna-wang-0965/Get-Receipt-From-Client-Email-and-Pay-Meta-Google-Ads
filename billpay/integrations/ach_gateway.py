import uuid

from billpay.models import PayoutResult


class MockAchGateway:
    """Simulates paying invoice amount to ad platform via ACH."""

    def send_invoice_payment(self, destination: str, amount: float) -> PayoutResult:
        if amount <= 0:
            return PayoutResult(
                success=False,
                amount=amount,
                reference="",
                error="ACH amount must be positive.",
            )

        reference = f"ach_{destination.lower()}_{uuid.uuid4().hex[:10]}"
        return PayoutResult(
            success=True,
            amount=round(amount, 2),
            reference=reference,
        )
