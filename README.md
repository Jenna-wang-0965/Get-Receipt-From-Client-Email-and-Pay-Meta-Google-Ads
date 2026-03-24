# BillPay for Ads (Workshop Prototype)

This is a runnable Python prototype of the flow:

1. Pull invoice emails from Gmail via API (OAuth consent).
2. Search for messages with PDF attachments (Meta/Google or `Invoice_*.pdf` style).
3. Download **only** PDFs named `Invoice_<InvoiceNumber>_<CustomerName>.pdf`, parse amount from PDF text.
4. Charge the customer with **Stripe** (`PaymentIntent`, card on file as `pm_...`).
5. Move the **invoice principal** (not your fee) to a **Stripe Connect** account (`Transfer`) — workshop stand-in for settling Meta ads (real Meta billing is usually bank/ACH outside Stripe).

### Invoice PDF filename (required)

Attachments must be PDFs named:

`Invoice_<InvoiceNumber>_<CustomerName>.pdf`

Examples:

- `Invoice_INV-2026-001_Acme_Ads_LLC.pdf`

Rules in code:

- `<InvoiceNumber>` must **not** contain `_` (use hyphens if needed, e.g. `INV-001`).
- `<CustomerName>` may use underscores — **any** value is accepted by default (only the pattern matters).
- Optional safety: `GmailInvoiceProvider(require_customer_name_match=True)` restricts files to names that match `Customer.name`, name tokens, or `Customer.invoice_name_aliases`.
- The ``.pdf`` extension may be missing in Gmail; PDF is still detected via MIME type (`application/pdf` or `application/octet-stream` with an `Invoice_*_*` name).

## Gmail API setup (required)

1. Create a Google Cloud project.
2. Enable **Gmail API**.
3. Configure OAuth consent screen.
4. Create OAuth Client ID (Desktop app).
5. Download client credentials JSON and save it as `credentials.json` in project root.

On first run, a browser opens for user permission. After approval, `token.json` is stored and reused.

## Stripe setup (optional — real charges / transfers)

If `STRIPE_SECRET_KEY` is set, `main.py` uses Stripe instead of mocks.

**Do not put your secret key in Python source or commit it to git.**

### One Customer + one PaymentMethod (reuse every invoice)

1. **Create & attach once** (uses test token `tok_visa` — no raw card API needed):

```bash
export STRIPE_SECRET_KEY=sk_test_...
python3 create_stripe.py onboard --email you@example.com
# or: python3 create_pm.py --email you@example.com
```

2. **Save** (no spaces around `=`):

```bash
export STRIPE_CUSTOMER_ID=cus_...
export STRIPE_PAYMENT_METHOD_ID=pm_...
export STRIPE_META_CONNECTED_ACCOUNT=acct_...
python3 main.py --once
```

3. **Reuse:** each run charges with `PaymentIntent.create(..., customer=..., payment_method=..., confirm=True)` — same ids every time.

### `.env` file (persists across sessions)

```bash
cp .env.example .env
# STRIPE_SECRET_KEY, STRIPE_CUSTOMER_ID, STRIPE_PAYMENT_METHOD_ID, STRIPE_META_CONNECTED_ACCOUNT
python3 main.py --once
```

`main.py` and `create_stripe.py` load `.env` via `python-dotenv`.

| Variable | Purpose |
|----------|---------|
| `STRIPE_SECRET_KEY` | `sk_test_...` — **never in source code** |
| `STRIPE_CUSTOMER_ID` | Stripe Customer `cus_...` |
| `STRIPE_PAYMENT_METHOD_ID` | `pm_...` **attached** to that customer |
| `STRIPE_META_CONNECTED_ACCOUNT` | Connect `acct_...` for invoice **Transfer** |

**Roll your key** in [Dashboard → API keys](https://dashboard.stripe.com/apikeys) if it was ever pasted into chat or committed.

- One **PaymentIntent** per invoice: `invoice + fee`, same saved customer + PM.
- **Transfer** sends the invoice principal to the connected account.

Use `python3 main.py --mock-payments` to skip Stripe.

### Connect `Transfer` and `balance_insufficient` (test mode)

Charges go to your platform balance, but **Connect Transfers** need enough **available** USD on the platform. Normal test cards often leave you short — Stripe suggests test card **`4000000000000077`** to fund *available* balance ([testing: available balance](https://stripe.com/docs/testing#available-balance)).

**Easiest for local dev:** simulate the Transfer only (still real `PaymentIntent` charges):

```bash
export BILLPAY_MOCK_CONNECT_TRANSFER=1
python3 main.py --once
# or: python3 main.py --mock-connect-transfer --once
```

With this flag you **do not** need `STRIPE_META_CONNECTED_ACCOUNT` in `.env`.

**Or** fund test balance (requires [raw card data APIs](https://support.stripe.com/questions/enabling-access-to-raw-card-data-apis) on your account):

```bash
python3 create_stripe.py fund-balance --amount 100.00
```

**Troubleshooting:** With Stripe on and **real** Transfers, set `STRIPE_CUSTOMER_ID`, `STRIPE_PAYMENT_METHOD_ID`, and `STRIPE_META_CONNECTED_ACCOUNT`. Use `BILLPAY_MOCK_CONNECT_TRANSFER=1` if Transfers fail with insufficient balance. Use `--mock-payments` to skip Stripe entirely.

**Note:** Meta Ads does not give you a Stripe Connect account. This pattern models *your* treasury: you still align real-world Meta ACH with your bank ops; Connect here is a code-friendly simulation.

## Install

```bash
python3 -m pip install -r requirements.txt
```

## Run

**Default: continuous mode** — the process keeps running and rechecks Gmail on a timer. When a **new** invoice email arrives, it is picked up on the **next** tick (all unpaid invoices in that tick are processed in batch).

```bash
python3 main.py
```

- Default interval: **60 seconds** between checks.
- Change interval: `python3 main.py --interval 30`
- Or set **`BILLPAY_POLL_INTERVAL`** (seconds; overrides `--interval` default when set).

**Single run** (one Gmail check, then exit):

```bash
python3 main.py --once
```

**Unpaid vs paid:** After a **successful** payment, the app records the Gmail **message id** and (for your records) the filename **invoice number**.

- **Default:** only **Gmail message ids** are used to skip work. A **new email** (new message id) is treated as **unpaid**, even if the attachment is still named `Invoice_20250120_….pdf` from testing.
- **Optional stricter dedupe:** `--dedupe-by-invoice-number` or `BILLPAY_DEDUPE_INVOICE_NUMBER=1` also skips any attachment whose `Invoice_<number>_` segment was already paid (stops paying the same invoice number twice across different emails).

Failed payments are **not** recorded, so the next tick retries.

State file: `processed_invoices.json` (override with `--state-file`). Use `Ctrl+C` to stop.

If you see *“No unpaid invoice found”* but mail is in Gmail: run with `-v` and check logs — often the PDF amount could not be parsed, or the message was outside the search window.

### Search behavior

- Several Gmail searches run (Meta/Google strict, `filename:Invoice`, attachments without `.pdf` in the name, etc.). Message IDs from **all** searches are merged (deduped).
- Each query may fetch **multiple pages** (up to 5 × up to 100 ids per page by default) so newer mail is less likely to be cut off.
- Candidates are sorted by **`internalDate` (newest first)**. The **first** message that contains a valid `Invoice_*` PDF for this customer wins — so a batch of new emails is not skipped in favor of an older hit from an earlier search.
- Optional: `GmailInvoiceProvider(lookback_days=90)` adds `newer_than:90d` to every query (limits how far back Gmail searches). Default is no time filter.
- `max_messages_per_query` (default `100`) is the page size for Gmail list; `max_list_pages_per_query` (default `5`) controls pagination depth per query.

Downloaded attachments are stored in `downloads/`.

## Fee model

- Stripe fee: 2.9%
- Product fee:
  - 3.0% when using your card
  - 3.5% when using external card

Total charged = invoice amount + (invoice * (stripe fee + product fee))

## Project layout

Run from the project root (`python main.py`) so the `billpay` package resolves.

| Area | Path |
|------|------|
| **Entry** | `main.py` |
| **Domain** (entities, pricing config) | `billpay/models.py`, `billpay/config.py` |
| **Pricing** | `billpay/fee_engine.py` |
| **Orchestration** | `billpay/services/billpay_service.py` |
| **Integrations** (Gmail, mocks, Stripe) | `billpay/integrations/` — `email_provider.py`, `card_gateway.py`, `ach_gateway.py`, `stripe_gateways.py` |
| **Persistence** | `billpay/persistence/invoice_state.py` |
| **Demo data** | `billpay/demo/sample_data.py` |

Use `from billpay....` imports in any new code (no duplicate modules at the repo root).

