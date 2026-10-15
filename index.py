import io
import re

import pandas as pd
import pdfplumber

from database import fetch_item_records, fetch_item_details_from_mssql


# =========================================================
# Helpers
# =========================================================

def _clean_text(value):
    if value is None:
        return ""
    return " ".join(
        part.strip()
        for part in str(value).replace("\r", "\n").split("\n")
        if part and part.strip()
    )


def _clean_ship_to_address(value):
    """Keep the Ship-To name/address, excluding contact details."""
    text = _clean_text(value)
    # Contact labels can appear on the same line as the postal address.
    # Stop at the first label rather than removing only its value.
    text = re.split(
        r"(?i)(?<![A-Za-z])(?:tel(?:ephone)?|phone|fax|e[\s-]*mail|email)\b\s*[:：.]?\s*",
        text,
        maxsplit=1,
    )[0]
    return text.strip(" ,;:-")


def _normalize_code(value):
    """Normalize SKU/item code for matching."""
    if value is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _extract_numeric(value):
    """Extract the first number found in a PDF table cell."""
    if value is None or str(value).strip() == "":
        return None

    match = re.search(r"[-+]?\d[\d,]*\.?\d*", str(value))
    if not match:
        return None

    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _as_float(value, default=None):
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _looks_like_address(text):
    """
    Prevent SHIP-TO / vendor/address text from being treated as an item SKU.
    """
    upper = str(text or "").upper()

    address_words = [
        "SDN BHD",
        "BERHAD",
        "JALAN",
        "KAWASAN",
        "PERINDUSTRIAN",
        "MUKIM",
        "LOT ",
        "PLOT ",
        "MELAKA",
        "JOHOR",
        "SELANGOR",
        "KUALA LUMPUR",
        "POSTCODE",
        "SHIP-TO",
        "SHIP TO",
        "LOCATION",
    ]

    return any(word in upper for word in address_words)


# =========================================================
# PDF extraction
# =========================================================

def extract_po_from_pdf(file_bytes, agency="econsave"):
    """Dispatch PDF extraction to the template selected on the Home page."""
    agency_key = str(agency or "econsave").strip().lower()
    if agency_key == "tf":
        return extract_tf_po_from_pdf(file_bytes)
    if agency_key == "econsave":
        return extract_econsave_po_from_pdf(file_bytes)
    raise ValueError(f"Unsupported agency/template: {agency}")
    
def extract_econsave_po_from_pdf(file_bytes):
    """
    Extract Econsave PO items from every page using PDF word coordinates.
    This template's item rows are not exposed as proper tables by pdfplumber,
    so parsing extract_tables() can return only headers and miss most items.
    """
    po_no = ""
    ship_to = ""
    extracted_items = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)

        # Purchase order number
        match = re.search(
            r"Purchase\s+Order\s+No\.?\s*:\s*([A-Z0-9\-/]+)",
            full_text,
            re.IGNORECASE,
        )
        if match:
            po_no = match.group(1).strip()

        # Ship-To address (read from the right-hand side of the first page)
        for page in pdf.pages:
            words = page.extract_words() or []
            ship_headers = [
                w for w in words
                if "SHIP-TO" in str(w.get("text", "")).upper()
                or str(w.get("text", "")).upper() == "SHIP"
            ]
            if not ship_headers:
                continue

            # Prefer the header on the right side of the page.
            header = next((w for w in ship_headers if w["x0"] >= page.width * 0.45), None)
            if header is None:
                continue

            header_top = header["top"]
            candidates = [
                w for w in words
                if w["x0"] >= page.width * 0.50
                and header_top < w["top"] < header_top + 85
            ]
            line_groups = {}
            for word in candidates:
                y = round(word["top"] / 3) * 3
                line_groups.setdefault(y, []).append(word)

            address_lines = []
            for y in sorted(line_groups):
                line = " ".join(
                    w["text"] for w in sorted(line_groups[y], key=lambda item: item["x0"])
                ).strip()
                upper = line.upper()
                if any(label in upper for label in (
                    "TEL", "TELEPHONE", "PHONE", "FAX", "E-MAIL", "EMAIL",
                    "ITEM", "DESCRIPTION", "BARCODE",
                )):
                    break
                if line and not any(label in upper for label in (
                    "SHIP-TO", "SHIP TO", "LOCATION",
                )):
                    address_lines.append(line)

            if address_lines:
                ship_to = _clean_ship_to_address(", ".join(address_lines))
                break

        # Use a safe fallback only if no address was detected.
        if not ship_to:
            ship_to = "10116 KANGKAR PULAI"

        # Item code anchors are 9-digit codes in the left-most item column.
        # Each item spans multiple visual lines: code/order pack, description/
        # total qty/unit price/amount, and barcode/capacity/free unit.
        for page in pdf.pages:
            words = page.extract_words() or []
            anchors = sorted(
                [
                    w for w in words
                    if 45 <= w["x0"] <= 105
                    and re.fullmatch(r"\d{9}", str(w.get("text", "")).strip())
                    and 245 < w["top"] < page.height - 70
                ],
                key=lambda w: w["top"],
            )

            for i, anchor in enumerate(anchors):
                top = anchor["top"]
                if i + 1 < len(anchors):
                    bottom = anchors[i + 1]["top"]
                else:
                    bottom = min(page.height - 45, top + 28)

                # Ignore implausibly tiny regions; a standard row includes
                # description and barcode lines below the item code.
                if bottom <= top + 5:
                    continue

                row_words = [
                    w for w in words
                    if top - 1 <= w["top"] < bottom
                ]

                def in_column(x0, x1):
                    return sorted(
                        [w for w in row_words if x0 <= w["x0"] < x1],
                        key=lambda w: (w["top"], w["x0"]),
                    )

                # Barcode is usually on the third visual line.
                barcode = next(
                    (
                        str(w["text"]).strip()
                        for w in in_column(45, 125)
                        if re.fullmatch(r"\d{12,14}", str(w["text"]).strip())
                    ),
                    "",
                )

                # Description can wrap onto another line, so collect all words
                # in its column while excluding item/barcode columns.
                description_words = [
                    w for w in in_column(120, 340)
                    if not re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?", str(w["text"]).strip())
                ]
                description_lines = {}
                for word in description_words:
                    y = round(word["top"] / 2) * 2
                    description_lines.setdefault(y, []).append(word)
                description = " ".join(
                    " ".join(w["text"] for w in sorted(description_lines[y], key=lambda z: z["x0"]))
                    for y in sorted(description_lines)
                ).strip()

                def first_number(x0, x1):
                    for word in in_column(x0, x1):
                        value = _extract_numeric(word["text"])
                        if value is not None:
                            return value
                    return None

                # Column positions for Econsave PDFs:
                # Total Qty x ≈ 390 to 470
                # Unit Price x ≈ 470 to 535
                # Amount / Total x ≈ 535 to 600
                quantity = first_number(390, 470)
                unit_price = first_number(470, 535)
                line_total = first_number(535, 600)

                # Fallback if coordinate columns shift
                if unit_price is None or line_total is None or unit_price > 1000:
                    valid_nums = [
                        _extract_numeric(w["text"])
                        for w in sorted(row_words, key=lambda item: item["x0"])
                        if _extract_numeric(w["text"]) is not None and _extract_numeric(w["text"]) > 0
                    ]
                    if len(valid_nums) >= 3:
                        quantity = valid_nums[-3]
                        unit_price = valid_nums[-2]
                        line_total = valid_nums[-1]
                    elif len(valid_nums) == 2:
                        unit_price = valid_nums[-2]
                        line_total = valid_nums[-1]

                # Don't let header/footer text become an item.
                if quantity is None or unit_price is None:
                    continue
                if quantity <= 0 or unit_price < 0:
                    continue

                if line_total is None:
                    line_total = round(quantity * unit_price, 2)

                # Keep both item code and barcode as mapping candidates.
                item_code = str(anchor["text"]).strip()
                sku_candidates = [item_code]
                if barcode and barcode != item_code:
                    sku_candidates.append(barcode)

                qty_value = int(quantity) if float(quantity).is_integer() else quantity
                entry = {
                    "PO NO": po_no,
                    "SHIP TO LOCATION": ship_to,
                    "DESCRIPTION": _clean_text(description),
                    "SKU": "\n".join(sku_candidates),
                    "UOM": "UNIT",
                    "QUANTITY": qty_value,
                    "PO PRICE (RM)": f"{unit_price:.2f}",
                    "PO TOTAL (RM)": f"{line_total:.2f}",
                }

                # Deduplicate in case PDF text positioning returns duplicate words.
                if not any(
                    old["SKU"] == entry["SKU"] and old["QUANTITY"] == entry["QUANTITY"]
                    for old in extracted_items
                ):
                    extracted_items.append(entry)

    columns = [
        "PO NO", "SHIP TO LOCATION", "DESCRIPTION", "SKU", "UOM",
        "QUANTITY", "PO PRICE (RM)", "PO TOTAL (RM)",
    ]
    return po_no, ship_to, pd.DataFrame(extracted_items, columns=columns)

def extract_tf_po_from_pdf(file_bytes):
    """Extract TF Value-Mart PO into the normalized MasterPO columns."""
    po_no = ""
    ship_to = ""
    extracted_items = []

    exclude_keywords = [
        "total amount",
        "vat amount",
        "grand total",
        "total qty",
        "subtotal",
        "tax @",
        "document summary",
    ]

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)

        # 1. Extract TF PO number (e.g. SDYPO26050886)
        match = re.search(r"\b([A-Z]{2,10}PO\d{6,})\b", full_text, re.IGNORECASE)
        if match:
            po_no = match.group(1).upper()
        else:
            m = re.search(r"PO\s+Refno\s*[\-\:]?\s*([A-Z0-9\-/]+)", full_text, re.I)
            if m:
                po_no = m.group(1).strip()

        # 2. Extract Delivery Location / Ship-To
        match = re.search(r"(?im)^\s*Deliver\s+To\s+(.+)$", full_text)
        if match:
            deliver_text = re.split(r"\s+Remark\b|\s+Term\b", match.group(1), maxsplit=1, flags=re.I)[0]
            ship_to = _clean_ship_to_address(deliver_text)

        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue

                headers = [_clean_text(c).lower() for c in table[0]]
                compact_headers = [re.sub(r"[^a-z0-9]", "", h) for h in headers]

                indexes = {
                    "sku": None,
                    "description": None,
                    "qty": None,
                    "unit_price": None,
                    "total_price": None,
                }

                for idx, header in enumerate(compact_headers):
                    if indexes["sku"] is None and any(k in header for k in ["itemcode", "articleno", "sku", "barcode"]):
                        indexes["sku"] = idx
                    elif indexes["description"] is None and any(k in header for k in ["description", "desc", "product"]):
                        indexes["description"] = idx
                    elif indexes["qty"] is None and any(k in header for k in ["quanlily", "quantity", "qty", "ictnqty"]):
                        indexes["qty"] = idx
                    elif indexes["unit_price"] is None and any(k in header for k in ["unitpriceincludetax", "unitpriceafterdisc", "unitprice"]):
                        indexes["unit_price"] = idx
                    elif indexes["total_price"] is None and any(k in header for k in ["totalamountincludetax", "totalamount", "amount"]):
                        indexes["total_price"] = idx

                # Fallback indices for TF template structure
                if indexes["sku"] is None:
                    indexes["sku"] = 1
                if indexes["description"] is None:
                    indexes["description"] = 1
                if indexes["qty"] is None:
                    indexes["qty"] = 9
                if indexes["unit_price"] is None:
                    indexes["unit_price"] = 8
                if indexes["total_price"] is None:
                    indexes["total_price"] = 11

                for row in table[1:]:
                    if not row or len(row) < 2:
                        continue

                    row_text = " ".join(str(cell).lower() for cell in row if cell is not None)
                    if any(kw in row_text for kw in exclude_keywords):
                        continue

                    raw_sku = str(row[indexes["sku"]] or "") if indexes["sku"] < len(row) else ""
                    sku_lines = [c.strip() for c in raw_sku.splitlines() if c.strip() and re.search(r"\d", c)]

                    if not sku_lines:
                        for cell in row[:3]:
                            for line in str(cell or "").splitlines():
                                line_str = line.strip()
                                if re.fullmatch(r"\d{6,14}", line_str):
                                    sku_lines.append(line_str)

                    if not sku_lines:
                        for cell in row:
                            for line in str(cell or "").splitlines():
                                if re.fullmatch(r"\d{6,14}", line.strip()):
                                    sku_lines.append(line.strip())

                    if not sku_lines:
                        continue

                    clean_sku = "\n".join(sku_lines)
                    description = _clean_text(row[indexes["description"]] if indexes["description"] < len(row) else "")

                    qty_value = _extract_numeric(row[indexes["qty"]] if indexes["qty"] < len(row) else "")
                    quantity = int(qty_value) if qty_value is not None and float(qty_value).is_integer() else (qty_value or 0)

                    price_value = _extract_numeric(row[indexes["unit_price"]] if indexes["unit_price"] < len(row) else None)
                    if price_value is None:
                        for cell in reversed(row):
                            val = _extract_numeric(cell)
                            if val is not None and val < 10000:
                                price_value = val
                                break

                    total_value = _extract_numeric(row[indexes["total_price"]] if indexes["total_price"] < len(row) else None)
                    if total_value is None and row:
                        total_value = _extract_numeric(row[-1])
                    if total_value is None and price_value is not None:
                        total_value = round(quantity * price_value, 2)

                    extracted_items.append({
                        "PO NO": po_no,
                        "SHIP TO LOCATION": ship_to,
                        "DESCRIPTION": description,
                        "SKU": clean_sku,
                        "QUANTITY": quantity,
                        "PO PRICE (RM)": f"{(price_value or 0.0):.2f}",
                        "PO TOTAL (RM)": f"{(total_value or 0.0):.2f}",
                    })

    columns = [
        "PO NO", "SHIP TO LOCATION", "DESCRIPTION", "SKU", "QUANTITY",
        "PO PRICE (RM)", "PO TOTAL (RM)",
    ]
    return po_no, ship_to, pd.DataFrame(extracted_items, columns=columns)


def extract_po_from_csv(file_bytes, agency="econsave"):
    """Dispatch PDF extraction to the template selected on the Home page."""
    agency_key = str(agency or "econsave").strip().lower()
    if agency_key == "tf":
        return extract_tf_po_from_csv(file_bytes)
    if agency_key == "econsave":
        return extract_econsave_po_from_csv(file_bytes)
    raise ValueError(f"Unsupported agency/template: {agency}")


def extract_econsave_po_from_csv(file_bytes):
    """Read either the customer's H/D PO CSV or a normal header-based PO CSV."""
    import csv
    from datetime import datetime

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin1")

    def normalize_date(value):
        value = str(value or "").strip()
        if not value:
            return ""
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(value[:10], fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return ""

    # ---------------------------------------------------------
    # Customer CSV format:
    # H,cust_code,location,po_no,order_date,...,delivery_date,...
    # D,line,item_code,uom,pack,barcode,description,capacity,order_units,qty,price,total
    # ---------------------------------------------------------
    csv_rows = list(csv.reader(io.StringIO(text)))
    first_nonempty = next((r for r in csv_rows if r and any(str(v).strip() for v in r)), [])

    if first_nonempty and str(first_nonempty[0]).strip().upper() == "H":
        header = first_nonempty
        po_no = str(header[3]).strip() if len(header) > 3 else ""
        cust_code = str(header[1]).strip() if len(header) > 1 else ""
        location = str(header[2]).strip() if len(header) > 2 else ""
        ship_to = " ".join(v for v in (cust_code, location) if v).strip()
        order_date = normalize_date(header[4] if len(header) > 4 else "")
        delivery_date = normalize_date(header[7] if len(header) > 7 else "")

        rows = []
        for r in csv_rows:
            if not r or str(r[0]).strip().upper() != "D":
                continue

            item_code = str(r[2]).strip() if len(r) > 2 else ""
            barcode = str(r[5]).strip() if len(r) > 5 else ""
            description = _clean_text(r[6] if len(r) > 6 else "")
            qty = _as_float(r[9] if len(r) > 9 else "", 0) or 0
            price = _as_float(r[10] if len(r) > 10 else "", 0.0) or 0.0
            total = _as_float(r[11] if len(r) > 11 else "", None)
            if total is None:
                total = round(price * qty, 2)

            if not item_code:
                continue

            qty_value = int(qty) if float(qty).is_integer() else qty
            # Keep barcode as a second candidate, matching the PDF extractor's
            # multi-code behavior. process_mapping() will use whichever exists
            # in Item Master.
            sku_candidates = [item_code]
            if barcode and barcode != item_code:
                sku_candidates.append(barcode)

            rows.append({
                "PO NO": po_no,
                "SHIP TO LOCATION": ship_to,
                "DESCRIPTION": description,
                "SKU": "\n".join(sku_candidates),
                "QUANTITY": qty_value,
                "PO PRICE (RM)": f"{price:.2f}",
                "PO TOTAL (RM)": f"{total:.2f}",
            })

        columns = ["PO NO", "SHIP TO LOCATION", "DESCRIPTION", "SKU", "QUANTITY", "PO PRICE (RM)", "PO TOTAL (RM)"]
        return po_no, ship_to, pd.DataFrame(rows, columns=columns), order_date, delivery_date

    # ---------------------------------------------------------
    # Generic CSV with a normal header row.
    # ---------------------------------------------------------
    df = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str).fillna("")
    if df.empty:
        return "", "", pd.DataFrame(), "", ""

    def key(value):
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    lookup = {key(col): col for col in df.columns}

    def find_col(*names):
        for name in names:
            k = key(name)
            if k in lookup:
                return lookup[k]
        return None

    po_col = find_col("PO NO", "PO#", "PO NUMBER", "PURCHASE ORDER NO")
    ship_col = find_col("SHIP TO LOCATION", "SHIP TO", "SHIP-TO", "LOCATION")
    desc_col = find_col("DESCRIPTION", "DESC", "ECONSAVE DESC", "ITEM DESCRIPTION")
    sku_col = find_col("SKU", "ITEM CODE", "ITEMCODE", "BARCODE", "PRODUCT CODE")
    qty_col = find_col("QUANTITY", "QTY", "ORDER QTY", "TOTAL QTY")
    price_col = find_col("PO PRICE (RM)", "PO PRICE", "UNIT PRICE", "CUSTOMER DMS PRICE", "PRICE")
    total_col = find_col("PO TOTAL (RM)", "PO TOTAL", "TOTAL", "AMOUNT", "LINE AMOUNT")
    order_date_col = find_col("ORDER DATE", "PO DATE", "PURCHASE ORDER DATE")
    delivery_date_col = find_col("DELIVERY DATE", "EXPECTED RECEIVE DATE", "REQUIRED DATE")

    required = {"SKU": sku_col, "QUANTITY": qty_col}
    missing = [name for name, col in required.items() if not col]
    if missing:
        raise ValueError("CSV missing required column(s): " + ", ".join(missing))

    def first_value(col):
        if not col:
            return ""
        values = [str(v).strip() for v in df[col].tolist() if str(v).strip()]
        return values[0] if values else ""

    po_no = first_value(po_col)
    ship_to = _clean_ship_to_address(first_value(ship_col))
    order_date = normalize_date(first_value(order_date_col))
    delivery_date = normalize_date(first_value(delivery_date_col))

    rows = []
    for _, row in df.iterrows():
        sku = str(row.get(sku_col, "") or "").strip()
        if not sku:
            continue
        qty = _as_float(row.get(qty_col), 0) or 0
        price = _as_float(row.get(price_col), 0.0) if price_col else 0.0
        total = _as_float(row.get(total_col), None) if total_col else None
        if total is None:
            total = round((price or 0.0) * qty, 2)
        qty_value = int(qty) if float(qty).is_integer() else qty
        rows.append({
            "PO NO": str(row.get(po_col, po_no) or po_no).strip() if po_col else po_no,
            "SHIP TO LOCATION": _clean_ship_to_address(row.get(ship_col, ship_to)) if ship_col else ship_to,
            "DESCRIPTION": _clean_text(row.get(desc_col, "")) if desc_col else "",
            "SKU": sku,
            "QUANTITY": qty_value,
            "PO PRICE (RM)": f"{(price or 0.0):.2f}",
            "PO TOTAL (RM)": f"{(total or 0.0):.2f}",
        })

    columns = ["PO NO", "SHIP TO LOCATION", "DESCRIPTION", "SKU", "QUANTITY", "PO PRICE (RM)", "PO TOTAL (RM)"]
    return po_no, ship_to, pd.DataFrame(rows, columns=columns), order_date, delivery_date


def extract_po_dates_from_pdf(file_bytes, agency=None):
    """Extract Order Date and Delivery Date from the uploaded PO as YYYY-MM-DD."""
    from datetime import datetime

    order_date = ''
    delivery_date = ''

    date_patterns = [
        '%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d',
        '%d-%m-%Y', '%m-%d-%Y', '%d.%m.%Y',
    ]

    def normalize_date(value):
        value = str(value or '').strip()
        for fmt in date_patterns:
            try:
                return datetime.strptime(value, fmt).strftime('%Y-%m-%d')
            except ValueError:
                pass
        return ''

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = '\n'.join((page.extract_text() or '') for page in pdf.pages)

    if str(agency or '').strip().lower() == 'tf':
        def normalize_tf_date(value):
            value = str(value or '').strip()
            for fmt in ('%d/%m/%y', '%d/%m/%Y'):
                try:
                    return datetime.strptime(value, fmt).strftime('%Y-%m-%d')
                except ValueError:
                    pass
            return ''

        order_match = re.search(r'PO\s+Date\s+(\d{1,2}/\d{1,2}/\d{2,4})', full_text, re.I)
        delivery_match = re.search(r'Deliver\s+on\s+(\d{1,2}/\d{1,2}/\d{2,4})', full_text, re.I)
        return (
            normalize_tf_date(order_match.group(1)) if order_match else '',
            normalize_tf_date(delivery_match.group(1)) if delivery_match else '',
        )

    lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    date_token = r'(\d{1,4}[\/\-.]\d{1,2}[\/\-.]\d{1,4})'

    for line in lines:
        upper = line.upper()

        if not order_date and any(label in upper for label in [
            'ORDER DATE', 'PO DATE', 'PURCHASE ORDER DATE'
        ]):
            match = re.search(date_token, line)
            if match:
                order_date = normalize_date(match.group(1))

        if not delivery_date and any(label in upper for label in [
            'EXPECTED RECEIVE DATE & TIME',
            'EXPECTED RECEIVE DATE',
            'DELIVERY DATE',
            'DELIVER DATE',
            'DELIVER ON',
            'REQUIRED DATE',
            'REQUESTED DELIVERY DATE'
        ]):
            match = re.search(date_token, line)
            if match:
                delivery_date = normalize_date(match.group(1))

        if order_date and delivery_date:
            break

    return order_date, delivery_date


# =========================================================
# Mapping + total checking
# =========================================================

def process_mapping(po_no, extracted_df):
    """
    Mapping flow mapping SKU to Item Master, capturing Item Price,
    checking DB price against PO price, and generating remarks.
    """
    if extracted_df is None or extracted_df.empty:
        return pd.DataFrame()

    item_records = fetch_item_records() or []
    item_lookup = {}

    for item in item_records:
        sku_key = _normalize_code(item.get("item_code"))
        pg_code = str(item.get("pg_code") or "").strip()
        item_price = _as_float(item.get("price"), 0.0)

        if sku_key:
            item_lookup[sku_key] = {
                "pg_code": pg_code,
                "description": str(item.get("description") or "").strip(),
                "item_price": item_price,
            }

    price_cache = {}
    mapped_rows = []

    for _, row in extracted_df.iterrows():
        raw_sku = str(row.get("SKU", "") or "")
        sku_candidates = [
            code.strip()
            for code in raw_sku.splitlines()
            if code.strip()
        ]

        matched_record = None
        matched_sku = ""

        for candidate in sku_candidates:
            key = _normalize_code(candidate)
            if key in item_lookup:
                matched_record = item_lookup[key]
                matched_sku = candidate
                break

        quantity = int(_as_float(row.get("QUANTITY"), 0) or 0)
        po_price = _as_float(row.get("PO PRICE (RM)"), 0.0) or 0.0
        po_total = _as_float(row.get("PO TOTAL (RM)"), 0.0) or 0.0

        pg_code = ""
        item_price = matched_record.get("item_price", 0.0) if matched_record else 0.0
        db_price = None
        calculated_total = None
        remarks = []

        if not matched_record:
            remarks.append("SKU NOT LISTED")
        else:
            pg_code = matched_record["pg_code"]
            if not pg_code:
                remarks.append("PG CODE NOT FOUND")
            else:
                if pg_code not in price_cache:
                    result = fetch_item_details_from_mssql(pg_code) or {}
                    price_cache[pg_code] = _as_float(
                        result.get("price"),
                        None,
                    )
                db_price = price_cache.get(pg_code)
                if db_price is None or db_price <= 0:
                    remarks.append("DATABASE PRICE NOT FOUND")
                    db_price = None

        if db_price is not None:
            if abs(db_price - po_price) > 0.01:
                remarks.append("PRICE MISMATCH")
            calculated_total = round(db_price * quantity, 2)
        else:
            calculated_total = round(po_price * quantity, 2)

        mapped_rows.append(
            {
                "PO NO": po_no or row.get("PO NO", ""),
                "SHIP TO LOCATION": row.get("SHIP TO LOCATION", ""),
                "DESCRIPTION": (
                    matched_record["description"]
                    if matched_record and matched_record["description"]
                    else row.get("DESCRIPTION", "")
                ),
                "SKU": matched_sku or raw_sku.replace("\n", " / "),
                "PG CODE": pg_code or "-",
                "UOM": row.get("UOM", "UNIT"),
                "QUANTITY": quantity,
                "ITEM PRICE (RM)": f"{item_price:.2f}" if matched_record else "-",
                "PO PRICE (RM)": f"{po_price:.2f}",
                "DB PRICE (RM)": (
                    f"{db_price:.2f}"
                    if db_price is not None
                    else "-"
                ),
                "PO TOTAL (RM)": f"{po_total:.2f}",
                "CALCULATED TOTAL (RM)": (
                    f"{calculated_total:.2f}"
                    if calculated_total is not None
                    else "-"
                ),
                "REMARK": "; ".join(remarks) if remarks else "OK",
            }
        )

    columns = [
        "PO NO",
        "SHIP TO LOCATION",
        "DESCRIPTION",
        "SKU",
        "PG CODE",
        "UOM",
        "QUANTITY",
        "ITEM PRICE (RM)",
        "PO PRICE (RM)",
        "DB PRICE (RM)",
        "PO TOTAL (RM)",
        "CALCULATED TOTAL (RM)",
        "REMARK",
    ]

    return pd.DataFrame(mapped_rows, columns=columns)


# =========================================================
# Export
# =========================================================

def export_to_csv_bytes(df):
    """
    Export CSV in the customer H/D format
    """
    import csv
    from datetime import datetime

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")

    # Retrieve stored order and delivery dates from session or use defaults
    order_date = datetime.now().strftime("%m/%d/%Y")
    delivery_date = (datetime.now()).strftime("%m/%d/%Y")

    po_no = ""
    ship_to = ""
    if not df.empty and "PO NO" in df.columns:
        po_no = str(df.iloc[0].get("PO NO", "")).strip()
    if not df.empty and "SHIP TO LOCATION" in df.columns:
        ship_to = str(df.iloc[0].get("SHIP TO LOCATION", "")).strip()

    # Split ship_to into customer code and location if formatted as such, or use defaults
    cust_code = ship_to.split()[0] if ship_to else "10012"
    location = " ".join(ship_to.split()[1:]) if len(ship_to.split()) > 1 else ship_to

    # 1. Header Row (H)
    header_row = [
        "H",
        cust_code,
        location,
        po_no,
        order_date,
        "PGMA_ALL_MERI19",
        "MERISON MARKETING SDN BHD",
        delivery_date,
        "",
        "",
        "",
        "",
    ]
    writer.writerow(header_row)

    # 2. Detail Rows (D)
    for no, (_, row) in enumerate(df.iterrows(), start=1):
        item_code = str(row.get("SKU", "") or "").strip()
        # If SKU has multiple lines (barcode/item code), take the first or second appropriately
        sku_lines = item_code.splitlines()
        primary_sku = sku_lines[-1] if sku_lines else ""
        barcode = sku_lines[0] if len(sku_lines) > 1 else primary_sku

        description = str(row.get("DESCRIPTION", "") or "").strip()
        uom = str(row.get("UOM", "UNIT")).strip()
        
        qty = row.get("QUANTITY", 0)
        try:
            qty_num = float(str(qty).replace(",", "").strip())
            qty_value = int(qty_num) if qty_num.is_integer() else qty_num
        except Exception:
            qty_value = qty

        po_price = row.get("DB PRICE (RM)") if row.get("DB PRICE (RM)") and row.get("DB PRICE (RM)") != "-" else row.get("PO PRICE (RM", 0.0)
        try:
            price_num = float(str(po_price).replace(",", "").strip())
        except Exception:
            price_num = 0.0

        total_val = round(price_num * float(qty_value), 2)

        detail_row = [
            "D",
            no,
            primary_sku,
            uom,
            "1UNITx1",
            barcode,
            description,
            "1",
            qty_value,
            qty_value,
            f"{price_num:.2f}",
            f"{total_val:.2f}",
        ]
        writer.writerow(detail_row)

    return output.getvalue().encode("utf-8-sig")


def _dta_quote(value):
    """Quote one field for the legacy Order.dta text format."""
    if value is None:
        value = ""

    value = str(value)
    value = value.replace('"', '""')
    value = value.replace(chr(13), " ")
    value = value.replace(chr(10), " ")

    return f'"{value}"'


def export_to_dta_bytes(
    df,
    po_no="",
    order_type="",
    customer_no="",
    seller_no="",
    order_date="",
    document_ref="",
    default_uom="PCS",
):
    """
    Export the legacy text-based Order.dta structure, restricting export to rows where REMARK is 'OK'.
    """
    from datetime import datetime

    # Filter to only export rows where REMARK is OK
    if df is not None and not df.empty and "REMARK" in df.columns:
        df = df[df["REMARK"].fillna("").astype(str).str.strip().str.upper() == "OK"]

    if df is None or df.empty:
        return b""

    if not order_date:
        order_date = datetime.now().strftime("%m/%d/%Y")

    if not document_ref:
        compact_po = re.sub(r"[^0-9A-Za-z]", "", str(po_no))
        suffix = compact_po[-10:] if compact_po else "ORDER"
        document_ref = f"{datetime.now().strftime('%Y%m%d')}_{suffix}"

    output_lines = []

    header_fields = [
        "Header",
        "",
        order_type,
        document_ref,
        customer_no,
        po_no,
        order_date,
        seller_no,
        "0",
        "N",
        "",
    ]

    output_lines.append(
        ",".join(_dta_quote(value) for value in header_fields)
    )

    for _, row in df.iterrows():
        pg_code = str(
            row.get("PG CODE", "")
            or row.get("pg_code", "")
            or ""
        ).strip()

        qty = row.get("QUANTITY", "")

        try:
            qty_num = float(str(qty).replace(",", "").strip())
            qty = str(int(qty_num)) if qty_num.is_integer() else str(qty_num)
        except Exception:
            qty = str(qty or "").strip()

        # --- Dynamic UOM Conversion Logic ---
        description = str(row.get("DESCRIPTION", "")).upper()
        
        if any(k in description for k in ["CARTON", "CTN", "CS"]):
            prefix = "CS"
            match = re.search(r"(?:X|\b)(\d+)", description)
            multiplier = match.group(1) if match else "6"
            uom = f"{prefix}{multiplier}"
        else:
            uom = "PCS"

        line_fields = [
            "Lines",
            pg_code,
            uom,
            qty,
            order_date,
            order_date,
            "1",
            "",
            "",
            "",
            "",
        ]

        output_lines.append(
            ",".join(_dta_quote(value) for value in line_fields) + ","
        )

    return ("\n".join(output_lines) + "\n").encode("utf-8")

def export_to_excel_bytes(df):
    """Export DataFrame to Excel bytes matching the reference report format."""
    output = io.BytesIO()
    
    report_rows = []
    for no, (_, row) in enumerate(df.iterrows(), start=1):
        po_no = str(row.get("PO NO") or row.get("PO Number") or row.get("PO#") or "").strip()
        pg_code = str(row.get("PG CODE") or row.get("Pg Code") or "").strip()
        if pg_code == "-":
            pg_code = ""
            
        item_code = str(row.get("SKU") or row.get("Item Code") or "").strip()
        sku_lines = [l.strip() for l in item_code.splitlines() if l.strip()]
        primary_item_code = sku_lines[-1] if sku_lines else item_code

        description = str(row.get("DESCRIPTION") or row.get("Description") or "").strip()
        
        qty = row.get("QUANTITY") or row.get("Qty") or 0
        try:
            qty_num = float(str(qty).replace(",", "").strip())
            qty_value = int(qty_num) if qty_num.is_integer() else qty_num
        except Exception:
            qty_value = qty

        uom = str(row.get("UOM") or "UNIT").strip()

        po_price = row.get("PO PRICE (RM)") or row.get("Customer Price") or 0.0
        try:
            po_price_num = float(str(po_price).replace(",", "").strip())
        except Exception:
            po_price_num = 0.0

        db_price = row.get("DB PRICE (RM)") or row.get("Selling Price") or 0.0
        try:
            db_price_num = float(str(db_price).replace(",", "").strip())
        except Exception:
            db_price_num = 0.0

        # If selling price is missing/not found, set to 0.00
        selling_price_display = f"{db_price_num:.2f}" if db_price_num > 0 else "0.00"
        sell_factor = qty_value
        
        # Variance = Customer Price - Selling Price (defaults to 0.00 if selling price is 0.00)
        remark = str(row.get("REMARK") or row.get("Remark") or "OK").strip()
        if db_price_num > 0:
            variance = round(po_price_num - db_price_num, 2)
        else:
            variance = round(po_price_num - 0.00, 2)

        report_rows.append({
            "No": no,
            "PO#": po_no,
            "Pg Code": pg_code,
            "Item Code": primary_item_code,
            "Description": description,
            "Quantity": qty_value,
            "UOM": uom,
            "Customer Price": f"{po_price_num:.2f}",
            "Selling Price": selling_price_display,
            "Sell Factor": sell_factor,
            "Variance": f"{variance:.2f}",
            "Remark": remark,
        })

    report_df = pd.DataFrame(report_rows)

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        report_df.to_excel(writer, index=False, sheet_name='Sheet1')
        worksheet = writer.sheets['Sheet1']
        worksheet.freeze_panes = 'A2'
        worksheet.auto_filter.ref = worksheet.dimensions
        for column_cells in worksheet.columns:
            max_length = max(len(str(cell.value or '')) for cell in column_cells)
            worksheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 40)
            
    output.seek(0)
    return output.getvalue()