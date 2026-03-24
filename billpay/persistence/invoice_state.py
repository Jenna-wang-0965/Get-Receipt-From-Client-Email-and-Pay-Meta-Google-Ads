"""
Persist paid invoices so periodic polling only picks up **unpaid** work.

Tracks:
- Gmail message ids (email already processed)
- Invoice numbers from ``Invoice_<InvoiceNumber>_<Name>`` (paid end-to-end)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Set, Tuple


class ProcessedInvoiceStore:
    """
    JSON shape (per customer):

    .. code-block:: json

        {
          "cust_001": {
            "paid_message_ids": ["abc123"],
            "paid_invoice_numbers": ["20250120", "INV-1"]
          }
        }

    Legacy format (list of message ids only) is still loaded as ``paid_message_ids``.
    """

    def __init__(self, path: str = "processed_invoices.json") -> None:
        self.path = Path(path)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def load_paid_state(self, customer_id: str) -> Tuple[Set[str], Set[str]]:
        """Return (paid_message_ids, paid_invoice_numbers)."""
        data = self._read_all()
        entry = data.get(customer_id)
        if entry is None:
            return set(), set()
        if isinstance(entry, list):
            return {str(x) for x in entry}, set()
        if isinstance(entry, dict):
            raw_msg = entry.get("paid_message_ids", [])
            raw_inv = entry.get("paid_invoice_numbers", [])
            msg = {str(x) for x in raw_msg} if isinstance(raw_msg, list) else set()
            inv = {str(x) for x in raw_inv} if isinstance(raw_inv, list) else set()
            return msg, inv
        return set(), set()

    def record_paid(
        self,
        customer_id: str,
        gmail_message_id: str,
        invoice_number: Optional[str],
    ) -> None:
        data = self._read_all()
        entry = data.get(customer_id)
        paid_msg: list[str] = []
        paid_inv: list[str] = []

        if isinstance(entry, list):
            paid_msg = [str(x) for x in entry]
        elif isinstance(entry, dict):
            paid_msg = list(entry.get("paid_message_ids", []))
            paid_inv = list(entry.get("paid_invoice_numbers", []))
            if not isinstance(paid_msg, list):
                paid_msg = []
            if not isinstance(paid_inv, list):
                paid_inv = []
            paid_msg = [str(x) for x in paid_msg]
            paid_inv = [str(x) for x in paid_inv]

        if gmail_message_id and gmail_message_id not in paid_msg:
            paid_msg.append(gmail_message_id)
        if invoice_number and str(invoice_number) not in paid_inv:
            paid_inv.append(str(invoice_number))

        data[customer_id] = {
            "paid_message_ids": paid_msg,
            "paid_invoice_numbers": paid_inv,
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Backwards compatibility for older main.py call sites
    def load_processed_ids(self, customer_id: str) -> set[str]:
        msg, _inv = self.load_paid_state(customer_id)
        return msg

    def add_processed_id(self, customer_id: str, gmail_message_id: str) -> None:
        self.record_paid(customer_id, gmail_message_id, None)
