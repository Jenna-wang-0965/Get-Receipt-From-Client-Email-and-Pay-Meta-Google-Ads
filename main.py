import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from billpay.config import PricingConfig, RiskConfig
from billpay.demo.sample_data import build_demo_customer
from billpay.integrations.ach_gateway import MockAchGateway
from billpay.integrations.card_gateway import MockCardGateway
from billpay.integrations.email_provider import GmailInvoiceProvider
from billpay.persistence.invoice_state import ProcessedInvoiceStore
from billpay.services.billpay_service import BillPayBatchResult, BillPayService


def _validate_stripe_env_if_charging_with_stripe(use_mock: bool, mock_connect_transfer: bool) -> None:
    """
    Stripe needs cus_... + pm_... (attached once). Real Connect Transfers also need acct_...
    unless payouts are mocked (BILLPAY_MOCK_CONNECT_TRANSFER / --mock-connect-transfer).
    """
    if use_mock:
        return
    if not (os.environ.get("STRIPE_SECRET_KEY") or "").strip():
        return
    pm = (os.environ.get("STRIPE_PAYMENT_METHOD_ID") or "").strip()
    if not pm.startswith("pm_"):
        raise ValueError(
            "Stripe is on but STRIPE_PAYMENT_METHOD_ID must be pm_...\n"
            "Run once: python3 create_stripe.py onboard --email you@example.com\n"
            "Then set STRIPE_CUSTOMER_ID and STRIPE_PAYMENT_METHOD_ID in .env.\n"
            "Or use --mock-payments to skip Stripe."
        )
    cus = (os.environ.get("STRIPE_CUSTOMER_ID") or "").strip()
    if not cus.startswith("cus_"):
        raise ValueError(
            "Stripe is on but STRIPE_CUSTOMER_ID must be cus_...\n"
            "Run once: python3 create_stripe.py onboard --email you@example.com\n"
            "Or use --mock-payments to skip Stripe."
        )
    if mock_connect_transfer:
        return
    acct = (os.environ.get("STRIPE_META_CONNECTED_ACCOUNT") or "").strip()
    if not acct.startswith("acct_"):
        raise ValueError(
            "Stripe is on but STRIPE_META_CONNECTED_ACCOUNT must be set to a Connect account id (acct_...).\n"
            "Example: export STRIPE_META_CONNECTED_ACCOUNT=acct_...\n"
            "Or simulate Transfers in test: export BILLPAY_MOCK_CONNECT_TRANSFER=1 "
            "(or main.py --mock-connect-transfer).\n"
            "Or use --mock-payments to skip Stripe for testing."
        )


def _build_payment_gateways(use_mock: bool, mock_connect_transfer: bool):
    """Use Stripe when STRIPE_SECRET_KEY is set; otherwise mock (or --mock-payments)."""
    if use_mock:
        if os.environ.get("STRIPE_SECRET_KEY"):
            logging.info("--mock-payments: using mock card + payout gateways.")
        return MockCardGateway(), MockAchGateway()
    if not os.environ.get("STRIPE_SECRET_KEY"):
        logging.info("STRIPE_SECRET_KEY not set — using mock card + payout gateways.")
        return MockCardGateway(), MockAchGateway()
    from billpay.integrations.stripe_gateways import StripeCardGateway, StripeConnectPayoutGateway

    if mock_connect_transfer:
        logging.info(
            "Stripe PaymentIntent (charge) + mock Connect transfer "
            "(BILLPAY_MOCK_CONNECT_TRANSFER=1 or --mock-connect-transfer)."
        )
        return StripeCardGateway(), MockAchGateway()
    logging.info("Using Stripe PaymentIntent (charge) + Connect Transfer (Meta payout).")
    return StripeCardGateway(), StripeConnectPayoutGateway()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _run_once(
    service: BillPayService,
    store: ProcessedInvoiceStore,
    customer_id: str,
    dedupe_by_invoice_number: bool,
) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logging.info("=== BillPay tick %s ===", ts)
    paid_msg, paid_inv = store.load_paid_state(customer_id)
    # Default: only skip Gmail messages we already paid (new email = new work).
    # If you reuse the same Invoice_<num>_ name for every test, invoice-number
    # dedupe would hide new mail — enable with --dedupe-by-invoice-number or
    # BILLPAY_DEDUPE_INVOICE_NUMBER=1.
    skip_inv = paid_inv if dedupe_by_invoice_number else set()
    if not dedupe_by_invoice_number and paid_inv:
        logging.debug(
            "Not skipping by invoice number (%d in state); only paid Gmail threads are skipped. "
            "Use --dedupe-by-invoice-number to also skip known invoice# from filenames.",
            len(paid_inv),
        )
    customer = build_demo_customer()
    batch: BillPayBatchResult = service.process_all_unpaid_invoices(
        customer,
        skip_message_ids=paid_msg,
        skip_invoice_numbers=skip_inv,
    )

    if not batch.results:
        logging.info("No unpaid invoices to process.")
        return

    logging.info("Processing %d unpaid invoice(s).", len(batch.results))
    for invoice, outcome in batch.results:
        display_name = (invoice.customer_name_on_invoice_file or customer.name or "").strip()
        inv_label = invoice.invoice_number or invoice.invoice_id
        logging.info("%s", display_name)
        logging.info(
            "  Invoice %s | $%.2f | Outcome ok=%s | Message: %s",
            inv_label,
            invoice.amount,
            outcome.ok,
            outcome.message,
        )
        if outcome.ok:
            logging.info("  Invoice Amount: $%.2f | Fee: $%.2f | Total charged: $%.2f", invoice.amount, outcome.fee_amount, outcome.total_charged)
            logging.info("  Charge ref: %s | Payout ref: %s", outcome.charge_ref, outcome.ach_ref)
            if outcome.gmail_message_id:
                store.record_paid(customer_id, outcome.gmail_message_id, outcome.invoice_number)
                logging.info(
                    "  Recorded paid — message: %s, invoice #: %s",
                    outcome.gmail_message_id,
                    outcome.invoice_number,
                )


def _poll_interval_seconds(cli_interval: int) -> int:
    """CLI --interval, overridden by BILLPAY_POLL_INTERVAL when set."""
    raw = os.environ.get("BILLPAY_POLL_INTERVAL", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logging.warning("BILLPAY_POLL_INTERVAL=%r is not an integer; using --interval.", raw)
    return max(1, cli_interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "BillPay for Ads — fetch invoices from Gmail and pay. "
            "By default runs continuously and rechecks Gmail on an interval."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single Gmail check then exit (default: keep running).",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help=argparse.SUPPRESS,  # backward compat; polling is now the default
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SEC",
        help="Seconds between Gmail checks when running continuously (default: 60). Override with BILLPAY_POLL_INTERVAL.",
    )
    parser.add_argument(
        "--state-file",
        default="processed_invoices.json",
        help="JSON file storing paid Gmail message ids + paid invoice numbers per customer.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Debug logging.",
    )
    parser.add_argument(
        "--mock-payments",
        action="store_true",
        help="Force mock card/payout even if STRIPE_SECRET_KEY is set.",
    )
    parser.add_argument(
        "--mock-connect-transfer",
        action="store_true",
        help=(
            "Use real Stripe charges but mock the Connect Transfer (avoids balance_insufficient "
            "in test when platform available balance is empty). Same as BILLPAY_MOCK_CONNECT_TRANSFER=1."
        ),
    )
    parser.add_argument(
        "--dedupe-by-invoice-number",
        action="store_true",
        help=(
            "Also skip attachments whose Invoice_<number>_ segment was already paid "
            "(default: only skip by Gmail message id, so new mail with same filename number is still processed)."
        ),
    )
    args = parser.parse_args()
    _setup_logging(args.verbose)

    interval_sec = _poll_interval_seconds(args.interval)
    run_continuously = not args.once

    customer_id = build_demo_customer().customer_id
    dedupe_inv = args.dedupe_by_invoice_number or _env_truthy("BILLPAY_DEDUPE_INVOICE_NUMBER")
    mock_connect = args.mock_connect_transfer or _env_truthy("BILLPAY_MOCK_CONNECT_TRANSFER")

    try:
        _validate_stripe_env_if_charging_with_stripe(args.mock_payments, mock_connect)
        card_gw, payout_gw = _build_payment_gateways(
            use_mock=args.mock_payments,
            mock_connect_transfer=mock_connect,
        )
        service = BillPayService(
            email_provider=GmailInvoiceProvider(
                credentials_path="credentials.json",
                token_path="token.json",
                attachments_dir="downloads",
            ),
            card_gateway=card_gw,
            ach_gateway=payout_gw,
            pricing_cfg=PricingConfig(),
            risk_cfg=RiskConfig(),
        )
        store = ProcessedInvoiceStore(path=args.state_file)

        if run_continuously:
            logging.info(
                "Continuous mode: checking Gmail every %s s for new invoices (Ctrl+C to stop). State: %s",
                interval_sec,
                args.state_file,
            )
            while True:
                try:
                    _run_once(service, store, customer_id, dedupe_inv)
                except Exception:
                    logging.exception("Tick failed; will retry after interval.")
                time.sleep(interval_sec)
        else:
            _run_once(service, store, customer_id, dedupe_inv)

    except FileNotFoundError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        print(
            "Place your Google OAuth desktop client file as "
            "'credentials.json' in the project root, then run again.",
            file=sys.stderr,
        )
        sys.exit(1)
    except PermissionError as exc:
        print(f"Permission error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Stripe configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    load_dotenv()
    main()
