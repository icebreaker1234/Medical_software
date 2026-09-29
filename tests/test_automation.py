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


# ---------------------------------------------------------------- blurry-photo enhancement
from PIL import ImageFilter  # noqa: E402

from app.ocr import _interpret_numbers, deskew, image_quality, merge_bills, parse_bill as _pb  # noqa: E402

_ITEMS = [dict(name="Paracetamol 650mg Tab", batch="PCM2609A", expiry="08/28", qty=50, free=5,
               mrp=33.0, rate=24.1, gst=5),
          dict(name="Montelukast + Levocetirizine Tab", batch="MLK7731", expiry="03/28", qty=20, free=0,
               mrp=180.0, rate=131.5, gst=5),
          dict(name="Hand Sanitizer 500ml", batch="HS7788", expiry="12/27", qty=10, free=0,
               mrp=250.0, rate=160.0, gst=18)]


def test_blur_is_detected():
    clean = sample_invoice(_ITEMS, "Shree Balaji Pharma Distributors", "SB/26/1", date(2026, 9, 28))
    blurry = clean.filter(ImageFilter.GaussianBlur(3))
    assert image_quality(clean)["verdict"] == "good"
    assert image_quality(blurry)["verdict"] in ("blurry", "very blurry")
    assert image_quality(blurry)["blur_score"] < image_quality(clean)["blur_score"] / 5


def test_deskew_recovers_tilt():
    import numpy as np
    img = sample_invoice(_ITEMS, "X", "SB/26/1", date(2026, 9, 28)).convert("L").rotate(
        3, expand=True, fillcolor=255)
    _, angle = deskew(np.asarray(img).astype("float32"))
    assert abs(abs(angle) - 3) <= 0.5


def test_lost_decimal_points_are_recovered_by_amount_check():
    # blurry OCR: "131.50" read as "13150"; qty 20 x 131.50 x 1.05 = 2761.50 proves the fix
    f = _interpret_numbers([(20, False), (0, False), (180.0, True), (13150, False), (5, False), (2761.5, True)])
    assert f["ok"] and f["rate"] == 131.5 and f["mrp"] == 180.0 and f["qty"] == 20


def test_broken_expiry_slash_and_letter_noise():
    pl = parse_line("5 Hand Sanitizer 500ml HS0925 12727 1O O 250.00 160.00 18 1888.00", CATALOGUE)
    assert pl.expiry == "2027-12-31" and pl.qty == 10 and pl.amount_check


def test_merge_keeps_verified_reading():
    a = _pb("1 Paracetamol 650mg Tab PCM2609A 08/28 50 5 3300 2410 5 1265.25", CATALOGUE)
    b = _pb("1 Paracetamol 650mg Tab PCM2609A 08/28 50 5 33.00 24.10 5 1265.25", CATALOGUE)
    merged = merge_bills([a, b])
    assert len(merged.lines) == 1 and merged.lines[0].amount_check and merged.lines[0].rate == 24.1


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_enhancement_reads_blurry_phone_photo():
    from app.ocr import ocr_image, simulate_phone_photo
    clean = sample_invoice(_ITEMS, "Shree Balaji Pharma Distributors", "SB/26/04812", date(2026, 9, 28))
    photo = simulate_phone_photo(clean, blur=1.2, seed=3)
    basic = _pb(ocr_image(photo, "basic", CATALOGUE).text, CATALOGUE)
    auto = ocr_image(photo, "auto", CATALOGUE).bill
    good = lambda b: sum(l.amount_check and l.medicine is not None for l in b.lines)  # noqa: E731
    assert good(auto) >= max(2, good(basic))
