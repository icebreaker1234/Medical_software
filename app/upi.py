"""UPI payment QR codes for bills.

Builds a standard UPI deep link (upi://pay?...) with the exact bill amount, and
renders it as a QR code. Any UPI app (GPay, PhonePe, Paytm, BHIM) can scan it.
Set the store's UPI ID with the STORE_UPI_ID environment variable / Streamlit secret.
"""
from __future__ import annotations

import base64
import io
import os
import re
import urllib.parse

import qrcode

STORE_UPI_ID = os.getenv("manthankhandelwal93-1@okicici", "manthankhandelwal93-1@okicici")   # demo value - replace
UPI_ID_PATTERN = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$")


def is_demo_upi(vpa: str = STORE_UPI_ID) -> bool:
    return vpa == "manthankhandelwal93-1@okicici"


def valid_upi_id(vpa: str) -> bool:
    return bool(UPI_ID_PATTERN.match(vpa or ""))


def upi_link(amount: float, note: str, vpa: str = STORE_UPI_ID, payee: str = "Medical Store") -> str:
    """Standard UPI intent link with a fixed amount (INR, 2 decimals)."""
    if not valid_upi_id(vpa):
        raise ValueError(f"Invalid UPI ID: {vpa}")
    if amount <= 0:
        raise ValueError("Amount must be positive")
    params = {"pa": vpa, "pn": payee, "am": f"{amount:.2f}", "cu": "INR", "tn": note[:50]}
    return "upi://pay?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def qr_png(data: str, box_size: int = 6) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(data: str, box_size: int = 4) -> str:
    """QR as a data: URI, for embedding in the HTML invoice."""
    return "data:image/png;base64," + base64.b64encode(qr_png(data, box_size)).decode()
