#!/usr/bin/env python3
"""
Shim: runs ``python3 create_stripe.py onboard`` (Customer + tok_visa PM + attach).

  export STRIPE_SECRET_KEY=sk_test_...
  python3 create_pm.py --email you@example.com
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    script = root / "create_stripe.py"
    cmd = [sys.executable, str(script), "onboard", *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
