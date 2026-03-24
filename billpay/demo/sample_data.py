import os

from billpay.models import CardPlan, Customer


def build_demo_customer() -> Customer:
    # MockCardGateway: token must start with "card_".
    # Stripe: STRIPE_CUSTOMER_ID + STRIPE_PAYMENT_METHOD_ID (see main.py + create_stripe.py onboard).
    default_pm = os.environ.get("STRIPE_PAYMENT_METHOD_ID", "card_demo_token_123")
    cus = (os.environ.get("STRIPE_CUSTOMER_ID") or "").strip() or None
    return Customer(
        customer_id="cust_001",
        name="Jenna",
        email="jennawang24680@gmail.com",
        has_email_access=True,
        card_plan=CardPlan.EXTERNAL_CARD,
        card_token=default_pm,
        stripe_customer_id=cus,
    )
