"""Send bills on WhatsApp via a click-to-chat link (free, no API key, no DLT).

The app builds the bill text and a https://wa.me/ link. The pharmacist clicks the
button, WhatsApp opens with the message already typed, and they tap Send.
"""
from __future__ import annotations

import re
import urllib.parse


def normalize_phone(raw: str | None) -> str | None:
    """Return a 12-digit Indian number (91XXXXXXXXXX) or None if invalid.

    Accepts: 9876543210, +91 98765 43210, 09876543210, 91-9876543210.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":      # Indian mobiles start with 6-9
        return "91" + digits
    return None


def bill_message(inv: dict, store_name: str, patient: str | None = None,
                 include_items: bool = True) -> str:
    """Plain-text bill for WhatsApp (WhatsApp supports *bold*)."""
    t = inv["totals"]
    date_txt = inv["ts"][:10]
    lines = [f"*{store_name}*", f"Bill: {inv['invoice_no']}  |  {date_txt}"]
    if patient:
        lines.append(f"Patient: {patient}")
    if include_items:
        lines.append("")
        for l in inv["lines"]:
            lines.append(f"• {l['product']} x{l['qty']} = Rs {l['amount']:.2f}")
    lines += [
        "",
        f"MRP total: Rs {t['gross']:.2f}",
        f"Discount: -Rs {t['disc']:.2f}",
        f"*Net paid: Rs {t['total']:.2f}* (incl. GST)",
        "",
        "Thank you! Get well soon.",
    ]
    return "\n".join(lines)


def wa_link(phone: str, message: str) -> str:
    """Click-to-chat link that opens WhatsApp with the message pre-filled."""
    return f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"


def udhaar_message(name: str, amount: float, store_name: str, upi_id: str | None = None,
                   last_bill: str | None = None) -> str:
    """Polite payment reminder for a credit (udhaar) balance."""
    first = (name or "").split()[0] if name else ""
    lines = [f"Namaste {first} ji,", "",
             f"This is a gentle reminder from *{store_name}*.",
             f"Pending amount on your account: *Rs {amount:,.2f}*"]
    if last_bill:
        lines.append(f"Last credit bill: {last_bill}")
    if upi_id:
        lines += ["", f"You can pay by UPI to: {upi_id}"]
    lines += ["", "Please ignore if already paid. Thank you!"]
    return "\n".join(lines)
