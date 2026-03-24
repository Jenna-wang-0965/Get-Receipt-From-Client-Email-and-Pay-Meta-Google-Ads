#!/usr/bin/env python3
"""
Create a Stripe **test** PaymentMethod (pm_...) via ``tok_visa``.

For **BillPay**, prefer one-shot Customer + attach:

  python3 create_stripe.py onboard --email you@example.com

This script is a minimal PM-only helper (never commit your secret key):

  export STRIPE_SECRET_KEY=sk_test_...
  python3 scripts/create_test_payment_method.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        print("Set STRIPE_SECRET_KEY (test secret key) first.", file=sys.stderr)
        return 1
    if not key.startswith("sk_test_"):
        print("Refusing: use a sk_test_... key only with this script.", file=sys.stderr)
        return 1

    import stripe

    stripe.api_key = key

    pm = stripe.PaymentMethod.create(
        type="card",
        card={"token": "tok_visa"},
    )
    print(pm.id)
    print("\nAdd to your environment:", flush=True)
    print(f'export STRIPE_PAYMENT_METHOD_ID="{pm.id}"', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
