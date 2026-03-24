#!/usr/bin/env python3
"""
Stripe **test** helpers — **one Customer + one PaymentMethod**, then reuse.

**Never put your secret key in this file.** Use ``STRIPE_SECRET_KEY`` in env / ``.env``.

**Step 1 — run once** (creates Customer, ``pm_...`` via ``tok_visa``, attaches):

    export STRIPE_SECRET_KEY=sk_test_...
    python3 create_stripe.py onboard --email you@example.com

**Step 2 — save** (no spaces around ``=``):

    export STRIPE_CUSTOMER_ID=cus_...
    export STRIPE_PAYMENT_METHOD_ID=pm_...

**Step 3 — reuse** (same as ``main.py`` / ``charge`` subcommand):

    stripe.PaymentIntent.create(
        amount=...,
        currency="usd",
        customer=cus_id,
        payment_method=pm_id,
        confirm=True,
    )

**Fund platform balance (test Connect Transfers)** — card ``4000000000000077`` adds *available* balance
(needs raw card data API, or use ``BILLPAY_MOCK_CONNECT_TRANSFER=1`` in ``main.py`` instead):

    python3 create_stripe.py fund-balance --amount 50.00
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv


def _require_sk_test() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        print("Set STRIPE_SECRET_KEY (test: sk_test_...).", file=sys.stderr)
        sys.exit(1)
    if not key.startswith("sk_test_"):
        print("This script only runs with sk_test_... keys.", file=sys.stderr)
        sys.exit(1)
    return key


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _create_pm(stripe, use_raw_card: bool):
    """Default: tok_visa (works without raw card data API)."""
    if use_raw_card:
        return stripe.PaymentMethod.create(
            type="card",
            card={
                "number": "4242424242424242",
                "exp_month": 12,
                "exp_year": 2030,
                "cvc": "123",
            },
        )
    return stripe.PaymentMethod.create(
        type="card",
        card={"token": "tok_visa"},
    )


def _attach_pm(stripe, pm_id: str, customer_id: str) -> None:
    try:
        stripe.PaymentMethod.attach(pm_id, customer=customer_id)
    except stripe.error.InvalidRequestError as exc:
        msg = str(exc).lower()
        code = getattr(exc, "code", None) or ""
        if code == "resource_already_exists" or "already been attached" in msg:
            return
        raise


def cmd_onboard(use_raw_card: bool, email: str) -> int:
    import stripe

    stripe.api_key = _require_sk_test()
    customer = stripe.Customer.create(email=email)
    pm = _create_pm(stripe, use_raw_card)
    _attach_pm(stripe, pm.id, customer.id)
    print("CUSTOMER:", customer.id)
    print("PM:", pm.id)
    print("\nAdd to .env or export (no spaces around =):", flush=True)
    print(f'export STRIPE_CUSTOMER_ID="{customer.id}"', flush=True)
    print(f'export STRIPE_PAYMENT_METHOD_ID="{pm.id}"', flush=True)
    return 0


def cmd_create_pm(use_raw_card: bool) -> int:
    import stripe

    stripe.api_key = _require_sk_test()
    pm = _create_pm(stripe, use_raw_card)
    print("PaymentMethod ID:", pm.id)
    print("\nPrefer: python3 create_stripe.py onboard --email you@example.com", flush=True)
    print(f'export STRIPE_PAYMENT_METHOD_ID="{pm.id}"', flush=True)
    return 0


def cmd_charge(amount: float, currency: str) -> int:
    import stripe

    stripe.api_key = _require_sk_test()
    pm = (os.environ.get("STRIPE_PAYMENT_METHOD_ID") or "").strip()
    cus = (os.environ.get("STRIPE_CUSTOMER_ID") or "").strip()
    if not pm.startswith("pm_"):
        print("Set STRIPE_PAYMENT_METHOD_ID=pm_...", file=sys.stderr)
        sys.exit(1)
    if not cus.startswith("cus_"):
        print("Set STRIPE_CUSTOMER_ID=cus_... (run: python3 create_stripe.py onboard ...)", file=sys.stderr)
        sys.exit(1)

    _attach_pm(stripe, pm, cus)
    intent = stripe.PaymentIntent.create(
        amount=_cents(amount),
        currency=currency.lower(),
        customer=cus,
        payment_method=pm,
        payment_method_types=["card"],
        confirm=True,
    )
    print("Status:", intent.status)
    print("PaymentIntent ID:", intent.id)
    if intent.status != "succeeded":
        sys.exit(1)
    return 0


def cmd_fund_balance(amount: float, currency: str) -> int:
    """
    Test-mode: one-off PaymentIntent with 4000000000000077 to increase platform *available* balance
    (see Stripe testing docs). Requires sending raw card numbers to the API.
    """
    import stripe

    stripe.api_key = _require_sk_test()
    try:
        pm = stripe.PaymentMethod.create(
            type="card",
            card={
                "number": "4000000000000077",
                "exp_month": 12,
                "exp_year": 2034,
                "cvc": "123",
            },
        )
    except stripe.error.CardError as exc:
        print(str(exc.user_message or exc), file=sys.stderr)
        print(
            "\nRaw card API may be disabled. Use:\n"
            "  export BILLPAY_MOCK_CONNECT_TRANSFER=1\n"
            "  python3 main.py\n"
            "or enable raw card data APIs in Stripe Dashboard (test mode).",
            file=sys.stderr,
        )
        return 1
    intent = stripe.PaymentIntent.create(
        amount=_cents(amount),
        currency=currency.lower(),
        payment_method=pm.id,
        payment_method_types=["card"],
        confirm=True,
    )
    print("Status:", intent.status)
    print("PaymentIntent ID:", intent.id)
    if intent.status != "succeeded":
        return 1
    print("Platform available balance should increase (check Dashboard → Balances).")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Stripe: one Customer + PaymentMethod (tok_visa), attach once, reuse."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser("onboard", help="Customer + PM (tok_visa) + attach; print ids.")
    p_on.add_argument(
        "--email",
        default=os.environ.get("STRIPE_CUSTOMER_EMAIL", "test@example.com"),
        help="Customer email.",
    )
    p_on.add_argument(
        "--raw-card",
        action="store_true",
        help="Use 4242... (only if raw card data APIs are enabled on your Stripe account).",
    )

    p_pm = sub.add_parser("create-pm", help="PM only (default tok_visa). Prefer onboard.")
    p_pm.add_argument(
        "--raw-card",
        action="store_true",
        help="Use 4242 test number instead of tok_visa.",
    )

    p_ch = sub.add_parser("charge", help="Test PaymentIntent with env CUSTOMER + PM ids.")
    p_ch.add_argument("--amount", type=float, required=True, help="Dollars, e.g. 10.00")
    p_ch.add_argument("--currency", default="usd", help="ISO currency (default usd).")

    p_fb = sub.add_parser(
        "fund-balance",
        help="Test: charge 4000000000000077 once to add platform available balance (raw card API).",
    )
    p_fb.add_argument("--amount", type=float, default=50.0, help="Dollars (default 50).")
    p_fb.add_argument("--currency", default="usd", help="ISO currency (default usd).")

    args = parser.parse_args()
    if args.cmd == "onboard":
        return cmd_onboard(use_raw_card=args.raw_card, email=args.email)
    if args.cmd == "create-pm":
        return cmd_create_pm(use_raw_card=args.raw_card)
    if args.cmd == "charge":
        return cmd_charge(amount=args.amount, currency=args.currency)
    if args.cmd == "fund-balance":
        return cmd_fund_balance(amount=args.amount, currency=args.currency)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
