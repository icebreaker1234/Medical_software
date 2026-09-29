import shutil
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from app.ocr import parse_bill, parse_line, sample_invoice
from app.upi import qr_png, upi_link, valid_upi_id
from app.whatsapp import udhaar_message

CATALOGUE = ["Paracetamol 650mg Tab", "Azithromycin 500mg Tab", "Hand Sanitizer 500ml",
             "Montelukast + Levocetirizine Tab"]


def test_upi_link_has_exact_amount():
    link = upi_link(1234.5, "Bill INV/2627/000001", vpa="store@okaxis", payee="Test Store")
    q = parse_qs(urlparse(link).query)
    assert link.startswith("upi://pay?")
    assert q["pa"] == ["store@okaxis"] and q["am"] == ["1234.50"] and q["cu"] == ["INR"]
    assert qr_png(link)[:8] == b"\x89PNG\r\n\x1a\n"


def test_upi_validation():
    assert valid_upi_id("name.shop@okhdfcbank")
    assert not valid_upi_id("not-a-upi-id")
    with pytest.raises(ValueError):
        upi_link(0, "x", vpa="store@okaxis")


def test_udhaar_message():
    msg = udhaar_message("Ramesh Sharma", 1520.0, "Test Store", "store@okaxis", "INV/2627/000045")
    assert "Ramesh ji" in msg and "Rs 1,520.00" in msg and "store@okaxis" in msg


def test_parse_line_extracts_fields():
    pl = parse_line("1 Paracetamol 650mg Tab PCM2609A 08/28 50 5 33.00 24.10 5 1265.25", CATALOGUE)
    assert pl.medicine == "Paracetamol 650mg Tab"
    assert (pl.batch_no, pl.expiry, pl.qty, pl.free_qty) == ("PCM2609A", "2028-08-31", 50, 5)
    assert (pl.mrp, pl.rate, pl.gst_rate) == (33.0, 24.1, 5.0)


def test_parse_line_handles_ocr_confusions():
    pl = parse_line("2 Montelukast + Levocetirizine Tab MLK7731 03/28 20 O 180.00 131.50 5 2761.50", CATALOGUE)
    assert pl.free_qty == 0 and pl.rate == 131.5 and pl.mrp == 180.0


def test_header_date_is_not_an_item():
    bill = parse_bill("Invoice No: SB/26/04812   Date: 28/09/2026\n", CATALOGUE)
    assert bill.invoice_no == "SB/26/04812" and bill.bill_date == "2026-09-28" and bill.lines == []


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_ocr_end_to_end_on_sample_bill():
    from app.ocr import run_ocr
    items = [dict(name="Paracetamol 650mg Tab", batch="PCM2609A", expiry="08/28", qty=50, free=5,
                  mrp=33.0, rate=24.1, gst=5),
             dict(name="Hand Sanitizer 500ml", batch="HS7788", expiry="12/27", qty=10, free=0,
                  mrp=250.0, rate=160.0, gst=18)]
    img = sample_invoice(items, "Shree Balaji Pharma Distributors", "SB/26/04812", date(2026, 9, 28))
    bill = parse_bill(run_ocr(img), CATALOGUE, ["Shree Balaji Pharma Distributors"])
    assert bill.supplier == "Shree Balaji Pharma Distributors"
    assert [l.medicine for l in bill.lines] == ["Paracetamol 650mg Tab", "Hand Sanitizer 500ml"]
    assert bill.lines[1].gst_rate == 18.0 and bill.lines[0].qty == 50
