import base64
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Protocol, Set

from google.auth.transport.requests import Request  # type: ignore[reportMissingImports]
from google.oauth2.credentials import Credentials  # type: ignore[reportMissingImports]
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[reportMissingImports]
from googleapiclient.discovery import build  # type: ignore[reportMissingImports]
from pypdf import PdfReader  # type: ignore[reportMissingImports]

from billpay.models import Customer, Invoice

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Expected: Invoice_<InvoiceNumber>_<CustomerName> with optional ``.pdf``
# (Gmail sometimes omits the extension; we also accept ``application/pdf`` by MIME.)
# InvoiceNumber must not contain underscores; CustomerName may use underscores.
INVOICE_PDF_FILENAME_RE = re.compile(
    r"^Invoice_(?P<invoice_number>[^_]+)_(?P<customer_name>.+?)(?:\.pdf)?$",
    re.IGNORECASE,
)


def normalize_customer_name_for_filename_match(name: str) -> str:
    """Normalize for comparing ``Customer.name`` to the filename ``CustomerName`` segment."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_invoice_pdf_filename(filename: str) -> Optional[tuple[str, str]]:
    """Return (invoice_number, customer_name_from_filename) if name matches convention."""
    base = Path(filename).name.strip()
    match = INVOICE_PDF_FILENAME_RE.match(base)
    if not match:
        return None
    return match.group("invoice_number"), match.group("customer_name")


def customer_name_in_filename_matches(customer: Customer, customer_name_on_file: str) -> bool:
    """
    Match filename segment to ``Customer.name``:
    - full normalized equality (e.g. Acme_Ads_LLC vs ``Acme Ads LLC``)
    - or filename is one token that matches any word in the legal name (e.g. ``Jenna`` vs ``Jenna Wang``)
    - or matches ``Customer.invoice_name_aliases`` (e.g. filename ``Ahaah`` vs alias ``Ahaah``).
    """
    file_key = normalize_customer_name_for_filename_match(
        customer_name_on_file.replace("_", " ")
    )
    cust_key = normalize_customer_name_for_filename_match(customer.name)
    if not file_key:
        return False
    if file_key == cust_key:
        return True
    for token in re.split(r"[\s_]+", customer.name.strip()):
        if not token:
            continue
        if normalize_customer_name_for_filename_match(token) == file_key:
            return True
    for alias in customer.invoice_name_aliases:
        if normalize_customer_name_for_filename_match(alias) == file_key:
            return True
    return False


def extract_invoice_amount_from_text(text: str) -> Optional[float]:
    """Parse dollar amount from invoice body text (PDF extract or plain text)."""
    labeled_patterns = [
        r"(?:amount\s+due|total\s+due|invoice\s+amount)\s*[:\-]?\s*\$?\s*([0-9][0-9,]*\.?[0-9]{0,2})",
        r"AMOUNT\s*:\s*\$?\s*([0-9][0-9,]*\.?[0-9]{0,2})",
    ]
    for pattern in labeled_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return round(float(match.group(1).replace(",", "")), 2)

    patterns = [
        r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?)",
        r"\$?\s*([0-9]+\.[0-9]{1,2})\b",
        r"\b([0-9]{4,})\b",
    ]
    candidates: list[str] = []
    for pat in patterns:
        candidates.extend(re.findall(pat, text))
    if not candidates:
        return None
    values = [float(c.replace(",", "")) for c in candidates]
    values = [v for v in values if not (1990 <= v <= 2035)]
    if not values:
        return None
    return round(max(values), 2)


def infer_platform_from_invoice_text(text: str) -> str:
    lowered = text.lower()
    if "meta" in lowered or "facebook" in lowered:
        return "Meta"
    if "google" in lowered:
        return "Google Ads"
    return "Unknown"


class InvoiceProvider(Protocol):
    def fetch_latest_invoice(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
    ) -> Optional[Invoice]:
        ...

    def fetch_unpaid_invoices(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
        *,
        newest_first: bool = False,
    ) -> List[Invoice]:
        ...


class MockEmailInvoiceProvider:
    """
    Local mock (no Gmail). Reads invoice files from ``inbox_dir``.

    Preferred production-style name: ``Invoice_<InvoiceNumber>_<CustomerName>.pdf``
    (same as Gmail). By default any ``<CustomerName>`` segment is accepted; set
    ``require_customer_name_match=True`` to restrict to ``Customer.name`` / aliases.

    Legacy fallback: ``{customer_id}_invoice.txt`` or newest ``*_invoice.txt``.
    """

    def __init__(
        self,
        inbox_dir: str = "mock_inbox",
        require_customer_name_match: bool = False,
    ) -> None:
        self.inbox_dir = Path(inbox_dir)
        self.require_customer_name_match = require_customer_name_match

    def fetch_unpaid_invoices(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
        *,
        newest_first: bool = False,
    ) -> List[Invoice]:
        if not customer.has_email_access:
            raise PermissionError("Customer has not granted email invoice access.")

        skip_msg = skip_message_ids or set()
        skip_inv = skip_invoice_numbers or set()
        # When we have at least one paid invoice# in state, skip by invoice number
        # so a Gmail thread with multiple PDFs can still be re-opened for remaining invoices.
        invoice_level_skip = len(skip_inv) > 0

        out: List[Invoice] = []

        pdf_candidates = sorted(
            self.inbox_dir.glob("Invoice_*_*.pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=newest_first,
        )
        for pdf_path in pdf_candidates:
            mock_mid = f"mock:pdf:{pdf_path.resolve()}"
            if mock_mid in skip_msg and not invoice_level_skip:
                continue
            parsed = parse_invoice_pdf_filename(pdf_path.name)
            if not parsed:
                continue
            inv_no, name_on_file = parsed
            if inv_no in skip_inv:
                continue
            if self.require_customer_name_match and not customer_name_in_filename_matches(
                customer, name_on_file
            ):
                continue
            text = self._read_pdf_text(pdf_path)
            amount = extract_invoice_amount_from_text(text)
            if amount is None:
                continue
            platform = self._extract_field(text, "PLATFORM") or infer_platform_from_invoice_text(text)
            due_date_raw = self._extract_field(text, "DUE_DATE")
            due_date = (
                datetime.strptime(due_date_raw, "%Y-%m-%d") if due_date_raw else datetime.utcnow()
            )
            out.append(
                Invoice(
                    invoice_id=inv_no,
                    invoice_number=inv_no,
                    customer_name_on_invoice_file=name_on_file,
                    platform=platform,
                    amount=amount,
                    due_date=due_date,
                    source_email=customer.email,
                    pdf_path=str(pdf_path),
                    gmail_message_id=mock_mid,
                )
            )

        if out:
            return out

        # Legacy .txt mocks (single file, treated as one invoice)
        preferred = self.inbox_dir / f"{customer.customer_id}_invoice.txt"
        if preferred.exists():
            invoice_path = preferred
        else:
            candidates = sorted(
                self.inbox_dir.glob("*_invoice.txt"),
                key=lambda p: p.stat().st_mtime,
                reverse=newest_first,
            )
            invoice_path = candidates[0] if candidates else None
        if not invoice_path:
            return []

        mock_mid = f"mock:txt:{invoice_path.resolve()}"
        if mock_mid in skip_msg and not invoice_level_skip:
            return []

        text = invoice_path.read_text(encoding="utf-8")
        amount = self._extract_amount(text)
        platform = self._extract_field(text, "PLATFORM") or "Meta"
        invoice_id = self._extract_field(text, "INVOICE_ID") or "unknown"
        due_date_raw = self._extract_field(text, "DUE_DATE")
        due_date = datetime.strptime(due_date_raw, "%Y-%m-%d") if due_date_raw else datetime.utcnow()

        return [
            Invoice(
                invoice_id=invoice_id,
                invoice_number=invoice_id,
                platform=platform,
                amount=amount,
                due_date=due_date,
                source_email=customer.email,
                pdf_path=str(invoice_path),
                gmail_message_id=mock_mid,
            )
        ]

    def fetch_latest_invoice(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
    ) -> Optional[Invoice]:
        invs = self.fetch_unpaid_invoices(
            customer,
            skip_message_ids=skip_message_ids,
            skip_invoice_numbers=skip_invoice_numbers,
            newest_first=True,
        )
        return invs[0] if invs else None

    @staticmethod
    def _read_pdf_text(pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)

    @staticmethod
    def _extract_field(text: str, key: str) -> Optional[str]:
        pattern = rf"{key}\s*:\s*(.+)"
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _extract_amount(text: str) -> float:
        raw = MockEmailInvoiceProvider._extract_field(text, "AMOUNT")
        if not raw:
            raise ValueError("AMOUNT not found in invoice content.")
        sanitized = raw.replace("$", "").replace(",", "").strip()
        return round(float(sanitized), 2)


class GmailInvoiceProvider:
    """
    Real Gmail provider:
    - OAuth user consent (first run)
    - Searches for invoice emails with PDF attachments named
      ``Invoice_<InvoiceNumber>_<CustomerName>.pdf``
    - Downloads PDF, extracts text, parses amount

    By default ``<CustomerName>`` is not validated against ``Customer.name``;
    pass ``require_customer_name_match=True`` for stricter matching.
    """

    def __init__(
        self,
        credentials_path: str = "credentials.json",
        token_path: str = "token.json",
        attachments_dir: str = "downloads",
        require_customer_name_match: bool = False,
        max_messages_per_query: int = 100,
        max_list_pages_per_query: int = 5,
        lookback_days: Optional[int] = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.attachments_dir = Path(attachments_dir)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.require_customer_name_match = require_customer_name_match
        self.max_messages_per_query = max_messages_per_query
        self.max_list_pages_per_query = max_list_pages_per_query
        self.lookback_days = lookback_days

    def fetch_unpaid_invoices(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
        *,
        newest_first: bool = False,
    ) -> List[Invoice]:
        if not customer.has_email_access:
            raise PermissionError("Customer has not granted email invoice access.")

        skip_msg = skip_message_ids or set()
        skip_inv = skip_invoice_numbers or set()
        invoice_level_skip = len(skip_inv) > 0

        service = self._build_gmail_service()
        queries = self._build_queries(customer.email)
        message_ids = self._collect_message_ids(service, queries)
        if not message_ids:
            logging.warning(
                "Gmail: no messages matched any invoice search query for %s.",
                customer.email,
            )
            return []

        skipped_paid_only = 0
        metas: list[tuple[int, str]] = []
        for mid in message_ids:
            if mid in skip_msg and not invoice_level_skip:
                skipped_paid_only += 1
                continue
            meta = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="metadata")
                .execute()
            )
            metas.append((int(meta.get("internalDate", 0)), mid))
        metas.sort(key=lambda row: row[0], reverse=newest_first)

        if not metas and skipped_paid_only:
            logging.info(
                "Gmail: %d thread(s) matched search but all are already recorded as paid "
                "(message id in processed_invoices state). Nothing to do until new mail arrives.",
                skipped_paid_only,
            )
            return []

        out: List[Invoice] = []
        for _internal_date, mid in metas:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            payload = full.get("payload", {})
            parts = payload.get("parts", [])
            batch = self._extract_all_invoices_from_parts(
                service, mid, parts, customer, skip_inv
            )
            out.extend(batch)

        if not out and metas:
            order = "newest first" if newest_first else "oldest first"
            logging.warning(
                "Gmail: scanned %d messages (%s) but none produced a usable "
                "Invoice_<num>_<name> PDF with a parseable amount. "
                "Check: filename pattern, PDF text (Amount due / AMOUNT:), "
                "or whether invoice# dedupe is skipping (use default: dedupe by Gmail id only).",
                len(metas),
                order,
            )
        return out

    def fetch_latest_invoice(
        self,
        customer: Customer,
        skip_message_ids: Optional[Set[str]] = None,
        skip_invoice_numbers: Optional[Set[str]] = None,
    ) -> Optional[Invoice]:
        invs = self.fetch_unpaid_invoices(
            customer,
            skip_message_ids=skip_message_ids,
            skip_invoice_numbers=skip_invoice_numbers,
            newest_first=True,
        )
        return invs[0] if invs else None

    def _collect_message_ids(self, service, queries: list[str]) -> list[str]:
        """Union of message IDs from every search query (deduped, with pagination)."""
        seen: set[str] = set()
        out: list[str] = []
        page_size = min(100, max(1, self.max_messages_per_query))
        for query in queries:
            q = self._maybe_append_lookback(query)
            page_token: Optional[str] = None
            for _ in range(max(1, self.max_list_pages_per_query)):
                kwargs: dict = dict(userId="me", q=q, maxResults=page_size)
                if page_token:
                    kwargs["pageToken"] = page_token
                response = service.users().messages().list(**kwargs).execute()
                for msg in response.get("messages", []):
                    mid = msg["id"]
                    if mid not in seen:
                        seen.add(mid)
                        out.append(mid)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        logging.debug("Gmail: collected %d unique message ids across queries.", len(out))
        return out

    def _maybe_append_lookback(self, query: str) -> str:
        if self.lookback_days is None or self.lookback_days <= 0:
            return query
        return f"{query} newer_than:{int(self.lookback_days)}d"

    def _build_gmail_service(self):
        creds = None
        token_file = Path(self.token_path)
        creds_file = Path(self.credentials_path)

        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_file.exists():
                    raise FileNotFoundError(
                        f"Gmail OAuth credentials not found at {self.credentials_path}. "
                        "Download OAuth client JSON from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
                creds = flow.run_local_server(port=0)
            token_file.write_text(creds.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=creds)

    @staticmethod
    def _build_queries(customer_email: str) -> list[str]:
        """
        Try strict Meta/Google PDF search first, then PDFs named ``Invoice_*.pdf``.
        """
        mailbox = f"(to:{customer_email} OR deliveredto:{customer_email})"
        strict = (
            f"{mailbox} "
            "(from:meta OR from:googleads-noreply OR from:google) "
            "(invoice OR bill OR statement) has:attachment filename:pdf"
        )
        # Matches ``Invoice_<InvoiceNumber>_<CustomerName>.pdf`` (filename:Invoice).
        broad = (
            f"{mailbox} has:attachment filename:pdf "
            "(filename:Invoice OR invoice OR bill OR statement)"
        )
        # Attachments without ``.pdf`` in the filename still need to be found.
        no_ext_pdf = (
            f"{mailbox} has:attachment "
            "(filename:Invoice OR filename:invoice OR filename:INVOICE)"
        )
        return [strict, broad, no_ext_pdf]

    def _extract_invoice_from_parts(
        self,
        service,
        message_id: str,
        parts: list,
        customer: Customer,
        skip_invoice_numbers: Set[str],
    ) -> Optional[Invoice]:
        all_inv = self._extract_all_invoices_from_parts(
            service, message_id, parts, customer, skip_invoice_numbers
        )
        return all_inv[0] if all_inv else None

    def _extract_all_invoices_from_parts(
        self,
        service,
        message_id: str,
        parts: list,
        customer: Customer,
        skip_invoice_numbers: Set[str],
    ) -> List[Invoice]:
        """Return every usable ``Invoice_<num>_<name>.pdf`` attachment in this message."""
        results: List[Invoice] = []
        for part in parts:
            filename = (part.get("filename") or "").strip()
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")
            if not filename or not attachment_id:
                nested = part.get("parts", [])
                if nested:
                    results.extend(
                        self._extract_all_invoices_from_parts(
                            service, message_id, nested, customer, skip_invoice_numbers
                        )
                    )
                continue

            mime = (part.get("mimeType") or "").lower()
            parsed = parse_invoice_pdf_filename(filename)
            looks_like_invoice_name = parsed is not None
            is_pdf = (
                mime in ("application/pdf", "application/x-pdf")
                or filename.lower().endswith(".pdf")
                or (
                    mime == "application/octet-stream"
                    and looks_like_invoice_name
                )
            )
            if not is_pdf:
                continue

            if not parsed:
                continue
            inv_no, name_on_file = parsed
            if inv_no in skip_invoice_numbers:
                logging.debug(
                    "Skip %s in message %s: invoice# %s treated as already paid.",
                    filename,
                    message_id,
                    inv_no,
                )
                continue
            if self.require_customer_name_match and not customer_name_in_filename_matches(
                customer, name_on_file
            ):
                continue

            attachment = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )
            data = attachment.get("data", "")
            if not data:
                continue

            decoded = base64.urlsafe_b64decode(data.encode("utf-8"))
            saved_path = self.attachments_dir / f"{message_id}_{filename}"
            saved_path.write_bytes(decoded)

            text = self._read_pdf_text(saved_path)

            amount = extract_invoice_amount_from_text(text)
            if amount is None:
                continue

            results.append(
                Invoice(
                    invoice_id=inv_no,
                    invoice_number=inv_no,
                    customer_name_on_invoice_file=name_on_file,
                    platform=infer_platform_from_invoice_text(text),
                    amount=amount,
                    due_date=datetime.utcnow(),
                    source_email=customer.email,
                    pdf_path=str(saved_path),
                    gmail_message_id=message_id,
                )
            )
        return results

    @staticmethod
    def _read_pdf_text(pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
