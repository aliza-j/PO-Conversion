import io
import json
import re
import os
from datetime import datetime

import pandas as pd
import pdfplumber
from werkzeug.security import check_password_hash
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from index import (
    export_to_csv_bytes,
    export_to_dta_bytes,
    export_to_excel_bytes,
    extract_po_from_pdf,
    extract_po_from_csv,
    extract_po_dates_from_pdf,
    process_mapping,
)

from database import (
    fetch_customer_records,
    save_customer_record,
    fetch_seller_by_customer_id,
    update_customer_record,
    delete_customer_record,
    fetch_item_records,
    save_item_record,
    update_item_record,
    delete_item_record,
    fetch_item_details_from_mssql,
    fetch_customer_mapping_for_po,
    sync_customer_po_mappings,
    save_customer_po_record,
    fetch_customer_po_records,
    fetch_customer_po_by_number,
    update_customer_po_status,
    delete_customer_po_record,
    ensure_customer_po_table,
    ensure_user_table,
    fetch_user_accounts,
    fetch_user_by_username,
    create_user_account,
    update_user_account,
    delete_user_account,
)

app = Flask(__name__)
app.secret_key = os.environ.get("MASTERPO_SECRET_KEY", "po_converter_secret_key")

# Seed credentials used only when the users table is empty.
# Change these environment variables before the first run in production.
DEFAULT_ADMIN_USERNAME = os.environ.get("MASTERPO_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("MASTERPO_ADMIN_PASSWORD", "MasterPO123")

def _safe_next_url(value):
    """Allow only local redirects such as /customer-po."""
    value = str(value or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("home")


ADMIN_ONLY_ENDPOINTS = {
    "user_management",
    "save_user_account",
    "update_user_account_route",
    "delete_user_account_route",
}


@app.before_request
def require_login():
    """Require login for MasterPO and allow User Management only for Admin."""
    if request.endpoint in {"login", "static"}:
        return None

    if not session.get("logged_in"):
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url))

    if (
        request.endpoint in ADMIN_ONLY_ENDPOINTS
        and str(session.get("role") or "").lower() != "admin"
    ):
        return redirect(url_for("home"))

    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_user_table(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)

    if session.get("logged_in"):
        return redirect(url_for("home"))

    error = ""

    if request.method == "POST":
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        next_url = _safe_next_url(request.form.get("next"))

        user = fetch_user_by_username(username)

        if (
            user
            and int(user.get("is_active") or 0) == 1
            and check_password_hash(str(user.get("password_hash") or ""), password)
        ):
            session.clear()
            session["logged_in"] = True
            session["user_id"] = user.get("no")
            session["username"] = user.get("username")
            session["role"] = str(user.get("role") or "user").lower()
            return redirect(next_url)

        error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error,
        next_url=_safe_next_url(request.args.get("next")),
    )


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Admin User Account Management ---
@app.route("/user-management", methods=["GET"])
def user_management():
    if session.get("role") != "admin":
        return redirect(url_for("home"))

    ensure_user_table(
        DEFAULT_ADMIN_USERNAME,
        DEFAULT_ADMIN_PASSWORD
    )

    users = fetch_user_accounts() or []

    # Get search keyword
    search = request.args.get('search','').strip().lower()

    filtered = []

    for user in users:

        if search:
            searchable_values = [
                user.get('username'),
                user.get('role'),
                "Active" if user.get('is_active') else 'Inactive',
            ]

            haystack = ' '.join(
                str(value or '').strip()
                for value in searchable_values
            ).lower()

            if search not in haystack:
                continue

        filtered.append(user)

    return render_template(
        "user_management.html", users=filtered, search=search, message=request.args.get("message", ""), error=request.args.get("error", ""),
    )


@app.route("/user-management/save", methods=["POST"])
def save_user_account():
    if session.get("role") != "admin":
        return redirect(url_for("home"))

    username = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")
    role = str(request.form.get("role") or "user").strip().lower()
    is_active = request.form.get("is_active") == "1"

    ok, error = create_user_account(
        username=username,
        password=password,
        role=role,
        is_active=is_active,
    )

    if ok:
        return redirect(url_for("user_management", message="User account created."))

    return redirect(url_for("user_management", error=error))


@app.route("/user-management/update", methods=["POST"])
def update_user_account_route():
    if session.get("role") != "admin":
        return redirect(url_for("home"))

    record_no = request.form.get("record_no")
    username = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")
    role = str(request.form.get("role") or "user").strip().lower()
    is_active = request.form.get("is_active") == "1"

    if str(record_no) == str(session.get("user_id")):
        # The signed-in admin must remain an active admin.
        role = "admin"
        is_active = True

    ok, error = update_user_account(
        record_no=record_no,
        username=username,
        role=role,
        is_active=is_active,
        password=password,
    )

    if ok:
        # Keep the displayed username synchronized if Admin edited their own account.
        if str(record_no) == str(session.get("user_id")):
            session["username"] = username
        return redirect(url_for("user_management", message="User account updated."))

    return redirect(url_for("user_management", error=error))


@app.route("/user-management/delete", methods=["POST"])
def delete_user_account_route():
    if session.get("role") != "admin":
        return redirect(url_for("home"))

    record_no = request.form.get("record_no")

    if str(record_no) == str(session.get("user_id")):
        return redirect(
            url_for("user_management", error="You cannot delete your own Admin account.")
        )

    if delete_user_account(record_no):
        return redirect(url_for("user_management", message="User account deleted."))

    return redirect(url_for("user_management", error="Unable to delete user account."))


def _safe_float(value, default=0.0):
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return default




def _mapped_result_is_ok(df_mapped):
    """A PO is valid only when every mapped line has REMARK = OK."""
    if df_mapped is None or df_mapped.empty or 'REMARK' not in df_mapped.columns:
        return False

    remarks = df_mapped['REMARK'].fillna('').astype(str).str.strip().str.upper()
    return bool(len(remarks)) and remarks.eq('OK').all()


def _po_remark_from_rows(rows):
    """Summarize mapping errors for the Customer PO Remark column."""
    if isinstance(rows, str):
        try:
            rows = json.loads(rows or '[]')
        except (TypeError, ValueError):
            rows = []

    if not isinstance(rows, list) or not rows:
        return 'SKU NOT LISTED'

    remarks = []
    all_rows_ok = True
    valid_rows_seen = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        valid_rows_seen += 1
        raw = str(row.get('REMARK') or '').strip()
        normalized = raw.upper()
        if normalized == 'OK':
            continue

        all_rows_ok = False

        if 'PRICE MISMATCH' in normalized or 'PRICE NOT SAME' in normalized:
            label = 'PRICE MISMATCH'
        elif 'SKU NOT LISTED' in normalized or 'SKU NOT MAPPED' in normalized or 'ITEM NOT LISTED' in normalized:
            label = 'SKU NOT LISTED'
        else:
            label = re.sub(r'\s*\([^)]*\)', '', raw).strip() or 'Other'
        if label not in remarks:
            remarks.append(label)

    if valid_rows_seen and all_rows_ok:
        return ''
    return ', '.join(remarks) or 'Mapping error'


def _po_total_from_rows(df_mapped):
    if df_mapped is None or df_mapped.empty:
        return 0.0
    return round(sum(_safe_float(v) for v in df_mapped.get('PO TOTAL (RM)', [])), 2)


def _customer_mapping_is_ok(customer):
    """Customer mapping must contain the values required for export/generation."""
    if not customer:
        return False

    required = ('order_type', 'sl_cust_code', 'seller')
    return all(str(customer.get(field) or '').strip() for field in required)


def _dta_date(value):
    """Convert stored YYYY-MM-DD date to MM/DD/YYYY for the legacy DTA text."""
    if not value:
        return ''
    text = str(value)
    try:
        return datetime.strptime(text[:10], '%Y-%m-%d').strftime('%m/%d/%Y')
    except ValueError:
        return text


def _csv_filename(ship_to):
    customer_name = ''
    if ship_to:
        first_word = str(ship_to).strip().split()[0]
        customer_name = re.sub(r'[^A-Za-z0-9_-]', '', first_word).upper()
    if not customer_name:
        customer_name = 'CUSTOMER'
    return f"{datetime.now().strftime('%d%m%Y')}-{customer_name}.csv"


@app.route('/', methods=['GET'])
def home():
    return render_template(
        'index.html',
        extracted_data=session.get('extracted_data'),
        mapped_data=session.get('mapped_data'),
        po_no=session.get('po_no', ''),
        ship_to=session.get('ship_to', ''),
        status=session.get('status', ''),
        agency=session.get('agency', 'econsave'),
    )

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('po_file')

    if not file or not file.filename:
        return redirect(url_for('home'))
    filename = file.filename.lower()
    if not (filename.endswith('.pdf') or filename.endswith('.csv')):
        return redirect(url_for('home'))

    # Capture and remember selected agency in session
    agency = str(request.form.get('agency') or session.get('agency') or 'econsave').strip().lower()
    session['agency'] = agency

    file_bytes = file.read()

    # --- Strict Agency Validation for PDFs ---
    if filename.endswith('.pdf'):
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            full_text = "\n".join((page.extract_text() or "") for page in pdf.pages).upper()
            
            is_tf = "TF VALUE-MART" in full_text
            is_econsave = "ECONSAVE" in full_text

            if agency == 'econsave' and is_tf:
                flash("Error: You selected Econsave, but uploaded a TF Value-Mart PO.", "error")
                return redirect(url_for('home'))
            if agency == 'tf' and is_econsave:
                flash("Error: You selected TF Value-Mart, but uploaded an Econsave PO.", "error")
                return redirect(url_for('home'))
    # ----------------------------------------

    po_no = ''
    ship_to = ''
    order_date = ''
    delivery_date = ''

    try:
        if filename.endswith('.csv'):
            po_no, ship_to, df_extracted, order_date, delivery_date = extract_po_from_csv(file_bytes, agency=agency)
        else:
            po_no, ship_to, df_extracted = extract_po_from_pdf(file_bytes, agency=agency)
            order_date, delivery_date = extract_po_dates_from_pdf(file_bytes, agency=agency)

        po_no = str(po_no or '').strip()
        ship_to = str(ship_to or '').strip()
        session['po_no'] = po_no
        session['ship_to'] = ship_to
        session['order_date'] = order_date or ''
        session['delivery_date'] = delivery_date or ''

        extracted_records = df_extracted.to_dict(orient='records') if df_extracted is not None and not df_extracted.empty else []
        session['extracted_data'] = extracted_records or None

        customer = fetch_customer_mapping_for_po(po_no) or {}
        order_type = str(customer.get('order_type') or '').strip()
        sl_cust_code = str(customer.get('sl_cust_code') or '').strip()
        seller = str(customer.get('seller') or '').strip()
        session['order_type'] = order_type
        session['customer_no'] = sl_cust_code
        session['seller_no'] = seller

        if extracted_records:
            df_mapped = process_mapping(po_no, df_extracted)
            mapped_records = df_mapped.to_dict(orient='records') if df_mapped is not None and not df_mapped.empty else []
            session['mapped_data'] = mapped_records or None
            mapping_ok = _mapped_result_is_ok(df_mapped)
            session['status'] = 'mapped' if mapping_ok else 'error'
        else:
            mapped_records = []
            df_mapped = pd.DataFrame()
            session['mapped_data'] = None
            session['status'] = 'error'

        # Save every identifiable PO, including mapping failures. The stored
        # rows/remarks allow Customer PO to show the issue and block export.
        if po_no:
            save_customer_po_record(
                po_number=po_no,
                total=_po_total_from_rows(df_mapped),
                order_type=order_type,
                cust_no=sl_cust_code,
                seller_no=seller,
                order_date=order_date,
                delivery_date=delivery_date,
                mapped_data=json.dumps(mapped_records, ensure_ascii=False),
                ship_to=ship_to,
            )

    except Exception as e:
        app.logger.exception('PO upload/extraction/mapping failed: %s', e)
        session['mapped_data'] = None
        session['status'] = 'error'
        # If extraction identified the PO before a later failure, keep a
        # history row rather than losing the uploaded PO completely.
        if po_no:
            try:
                customer = fetch_customer_mapping_for_po(po_no) or {}
                save_customer_po_record(
                    po_number=po_no,
                    total=0,
                    order_type=str(customer.get('order_type') or '').strip(),
                    cust_no=str(customer.get('sl_cust_code') or '').strip(),
                    seller_no=str(customer.get('seller') or '').strip(),
                    order_date=order_date,
                    delivery_date=delivery_date,
                    mapped_data='[]',
                    ship_to=ship_to,
                )
            except Exception:
                app.logger.exception('Unable to save failed PO upload %s', po_no)

    return redirect(url_for('home'))


@app.route('/map', methods=['POST'])
def map_data():
    extracted_data = session.get('extracted_data')
    po_no = str(session.get('po_no') or '').strip()

    if extracted_data and po_no:
        df_extracted = pd.DataFrame(extracted_data)
        df_mapped = process_mapping(po_no, df_extracted)
        mapped_records = df_mapped.to_dict(orient='records') if df_mapped is not None and not df_mapped.empty else []
        session['mapped_data'] = mapped_records or None

        # Refresh customer mapping in case it was corrected before re-mapping.
        customer = fetch_customer_mapping_for_po(po_no) or {}
        order_type = str(customer.get('order_type') or '').strip()
        sl_cust_code = str(customer.get('sl_cust_code') or '').strip()
        seller = str(customer.get('seller') or '').strip()
        session['order_type'] = order_type
        session['customer_no'] = sl_cust_code
        session['seller_no'] = seller

        mapping_ok = _mapped_result_is_ok(df_mapped)
        session['status'] = 'mapped' if mapping_ok else 'error'
        save_customer_po_record(
            po_number=po_no,
            total=_po_total_from_rows(df_mapped),
            order_type=order_type,
            cust_no=sl_cust_code,
            seller_no=seller,
            order_date=session.get('order_date', ''),
            delivery_date=session.get('delivery_date', ''),
            mapped_data=json.dumps(mapped_records, ensure_ascii=False),
            ship_to=session.get('ship_to', ''),
        )
    else:
        session['mapped_data'] = None
        session['status'] = 'error'

    return redirect(url_for('home'))


@app.route('/reset', methods=['GET'])
def reset():
    # Clear current PO work only. Keep session['agency'] persistent.
    po_session_keys = [
        'po_no', 'ship_to', 'order_date', 'delivery_date',
        'extracted_data', 'mapped_data', 'status',
        'order_type', 'customer_no', 'seller_no',
    ]
    for key in po_session_keys:
        session.pop(key, None)

    return redirect(url_for('home'))


def _export_dataframe(df, file_type, po_no, ship_to, order_type='', cust_no='', seller_no='', order_date=''):
    export_columns = [
        'PO NO', 'SHIP TO LOCATION', 'DESCRIPTION', 'SKU', 'PG CODE',
        'QUANTITY', 'PO PRICE (RM)', 'DB PRICE (RM)', 'PO TOTAL (RM)',
        'CALCULATED TOTAL (RM)', 'REMARK',
    ]
    df = df.reindex(columns=export_columns).fillna('')

    if file_type == 'csv':
        return export_to_csv_bytes(df), 'text/csv', _csv_filename(ship_to)

    if file_type == 'dta':
        content = export_to_dta_bytes(
            df,
            po_no=po_no,
            order_type=order_type,
            customer_no=cust_no,
            seller_no=seller_no,
            order_date=_dta_date(order_date),
            document_ref='',
            default_uom='UNIT',
        )
        return content, 'application/octet-stream', 'Order.dta'

    return None, None, None

@app.route('/download/report', methods=['GET'])
def download_report():
    mapped_data = session.get('mapped_data')
    extracted_data = session.get('extracted_data')
    po_no = session.get('po_no', 'Unknown')

    if mapped_data:
        report_df = pd.DataFrame(mapped_data)
    elif extracted_data:
        report_df = pd.DataFrame(extracted_data)
    else:
        return redirect(url_for('home'))

    content = export_to_excel_bytes(report_df)
    
    # Format: report date - po_no.xlsx
    report_date = datetime.now().strftime('%d%m%Y')
    filename = f"{report_date} - {po_no}.xlsx"
    
    return Response(
        content,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )

@app.route('/download/<file_type>', methods=['GET'])
def download(file_type):
    mapped_data = session.get('mapped_data')
    po_no = session.get('po_no', '')

    if not mapped_data:
        return redirect(url_for('home'))

    if file_type == 'dta':
        customer = fetch_customer_mapping_for_po(po_no)
        if customer:
            session['order_type'] = str(customer.get('order_type') or '').strip()
            session['customer_no'] = str(customer.get('sl_cust_code') or '').strip()
            session['seller_no'] = str(customer.get('seller') or '').strip()
        if not all(session.get(k) for k in ('order_type', 'customer_no', 'seller_no')):
            return redirect(url_for('home'))

    content, mimetype, filename = _export_dataframe(
        pd.DataFrame(mapped_data),
        file_type,
        po_no,
        session.get('ship_to', ''),
        session.get('order_type', ''),
        session.get('customer_no', ''),
        session.get('seller_no', ''),
        session.get('order_date', ''),
    )
    if content is None:
        return redirect(url_for('home'))

    if file_type == 'csv':
        update_customer_po_status(po_no, 'exported')
    elif file_type == 'dta':
        update_customer_po_status(po_no, 'generate')

    return Response(
        content,
        mimetype=mimetype,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# --- Customer Mapping Routes ---
@app.route('/customer-mapping', methods=['GET'])
def customer_mapping():
    return render_template('customer_mapping.html', mappings=fetch_customer_records() or [])

@app.route('/customer-mapping/save', methods=['POST'])
def save_customer_mapping():

    cust_code = str(request.form.get('cust_code') or '').strip()
    sl_cust_code = str(request.form.get('sl_cust_code') or '').strip()
    seller = request.form.get('seller')
    order_type = str(request.form.get('order_type') or '').strip()
    update_by = session.get('username', '')

    if not cust_code or not sl_cust_code:
        return render_template(
            'customer_mapping.html',
            mappings=fetch_customer_records() or [],
            error='Cust Code and SL Code are required.'
        )

    ok, error = save_customer_record(cust_code, sl_cust_code, seller, order_type, update_by)
    
    if not ok:
        return render_template(
            'customer_mapping.html',
            mappings=fetch_customer_records() or [],
            error=error
        )
        
    sync_customer_po_mappings()
    return redirect(url_for('customer_mapping'))


@app.route('/customer-mapping/update', methods=['POST'])
def update_customer_mapping():
    record_no = request.form.get('record_no')
    update_by = session.get('username', '')
    if record_no:
        update_customer_record(
            record_no,
            request.form.get('cust_code'),
            request.form.get('sl_cust_code'),
            request.form.get('seller'),
            request.form.get('order_type'),
            update_by
        )
        sync_customer_po_mappings()
    return redirect(url_for('customer_mapping'))


@app.route('/customer-mapping/delete-from-form', methods=['POST'])
def delete_from_form_customer_mapping():
    record_no = request.form.get('record_no')
    if record_no:
        delete_customer_record(record_no)
    return redirect(url_for('customer_mapping'))


@app.route('/api/get-customer-seller', methods=['GET'])
def get_customer_seller():
    cust_id = request.args.get('cust_id', '').strip()
    seller = fetch_seller_by_customer_id(cust_id)
    return jsonify({'seller': seller or ''})
    
# --- Item Master Routes ---
@app.route('/item-master', methods=['GET'])
def item_master():
    return render_template('item_master.html', items=fetch_item_records() or [])


@app.route('/item-master/save', methods=['POST'])
def save_item_master():
    item_code = str(request.form.get('item_code') or '').strip()
    pg_code = str(request.form.get('pg_code') or '').strip()
    description = str(request.form.get('description') or '').strip()
    price = request.form.get('price')
    update_by = session.get('username', '')

    if not item_code or not pg_code:
        return render_template(
            'item_master.html',
            items=fetch_item_records() or [],
            error='Item Code and PG Code are required.'
        )

    ok, error = save_item_record(item_code, pg_code, description, price, update_by)

    if not ok:
        return render_template(
            'item_master.html',
            items=fetch_item_records() or [],
            error=error
        )

    return redirect(url_for('item_master'))


@app.route('/item-master/update', methods=['POST'])
def update_item_master():
    record_no = request.form.get('record_no')
    update_by = session.get('username', '')
    if record_no:
        update_item_record(
            record_no,
            request.form.get('item_code'),
            request.form.get('pg_code'),
            request.form.get('description'),
            request.form.get('price'),
            update_by
        )
    return redirect(url_for('item_master'))


@app.route('/item-master/delete-from-form', methods=['POST'])
def delete_from_form_item_master():
    record_no = request.form.get('record_no')
    if record_no:
        delete_item_record(record_no)
    return redirect(url_for('item_master'))


@app.route('/api/get-item-details', methods=['GET'])
def get_item_details():
    pg_code = request.args.get('pg_code', '').strip()
    return jsonify(fetch_item_details_from_mssql(pg_code))


# --- Customer PO History / Re-export ---
@app.route('/customer-po', methods=['GET'])
def customer_po():
    ensure_customer_po_table()
    sync_customer_po_mappings()  # Also backfill POs uploaded before this feature was added.
    po_list = fetch_customer_po_records() or []

    status = request.args.get('status', 'ALL').strip().upper()
    search = request.args.get('search', '').strip().lower()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    filtered = []

    for po in po_list:
        exported_status = str(po.get('exported') or 'Pending').strip().upper()
        generated_status = str(po.get('generate') or po.get('generated') or 'Pending').strip().upper()
        po['remark'] = _po_remark_from_rows(po.get('mapped_data') or '[]')

        delivery_raw = po.get('delivery_date')
        delivery = ''

        if delivery_raw:
            try:
                delivery = delivery_raw.strftime('%Y-%m-%d')
            except AttributeError:
                delivery = str(delivery_raw)[:10]

        # STATUS FILTER
        if status == 'EXPORTED' and exported_status != 'EXPORTED':
            continue

        if status == 'GENERATED' and generated_status != 'GENERATED':
            continue

        if status == 'PENDING':
            if exported_status != 'PENDING' and generated_status != 'PENDING':
                continue

        # DELIVERY DATE FILTER
        # If a date filter is selected, records without a delivery date
        # must not be shown.
        if date_from or date_to:
            if not delivery:
                continue

            if date_from and delivery < date_from:
                continue

            if date_to and delivery > date_to:
                continue

        # SEARCH FILTER
        if search:
            searchable_values = [
                po.get('po_number'),
                po.get('order_type'),
                po.get('cust_no'),
                po.get('seller_no'),
                po.get('total'),
                po.get('order_date'),
                po.get('delivery_date'),
                po.get('remark'),
            ]

            haystack = ' '.join(
                str(value or '').strip()
                for value in searchable_values
            ).lower()

            if search not in haystack:
                continue

        filtered.append(po)

    return render_template('customer_po.html', po_list=filtered)

@app.route('/customer-po/batch-action', methods=['POST'])
def customer_po_batch_action():
    action = request.form.get('action', '').strip().lower()
    po_numbers = [
        str(po).strip()
        for po in request.form.getlist('po_numbers')
        if str(po).strip()
    ]

    if not po_numbers:
        return redirect(url_for('customer_po'))

    # Export selected PO history rows as a detailed Excel report matching the reference format.
    if action == 'excel':
        all_item_rows = []
        for po_number in dict.fromkeys(po_numbers):
            record = fetch_customer_po_by_number(po_number)
            if not record:
                continue
            try:
                mapped_data = json.loads(record.get('mapped_data') or '[]')
            except (TypeError, json.JSONDecodeError):
                mapped_data = []

            if not mapped_data:
                # Fallback if no item rows stored, create a placeholder summary row
                all_item_rows.append({
                    'PO NO': po_number,
                    'SKU': '',
                    'DESCRIPTION': '',
                    'QUANTITY': 0,
                    'PO PRICE (RM)': '0.00',
                    'DB PRICE (RM)': '0.00',
                    'REMARK': _po_remark_from_rows(record.get('mapped_data') or '[]'),
                })
            else:
                for row in mapped_data:
                    row['PO NO'] = po_number
                    all_item_rows.append(row)

        if not all_item_rows:
            return Response('No selected PO records were found.', status=400, mimetype='text/plain')

        report_df = pd.DataFrame(all_item_rows)
        content = export_to_excel_bytes(report_df)
        
        report_date = datetime.now().strftime('%d%m%Y')
        po_label = po_numbers[0] if len(po_numbers) == 1 else f"BATCH-{len(po_numbers)}"
        filename = f"{po_label} - {report_date}.xlsx"
        
        return Response(
            content,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    # Delete all selected POs.
    if action == 'delete':
        for po_number in po_numbers:
            delete_customer_po_record(po_number)
        return redirect(url_for('customer_po'))

    # Multiple DTA generation: concatenate complete Header/Lines blocks into ONE Order.dta.
    if action == 'dta':
        sync_customer_po_mappings()
        sections = []
        generated = []
        skipped = []
        for po_number in dict.fromkeys(po_numbers):
            record = fetch_customer_po_by_number(po_number)
            if not record:
                skipped.append(po_number)
                continue
            try:
                mapped_data = json.loads(record.get('mapped_data') or '[]')
            except (TypeError, ValueError):
                skipped.append(po_number)
                continue
            if not mapped_data:
                skipped.append(po_number)
                continue
            history_df = pd.DataFrame(mapped_data)
            
            # REMOVED error check so DTA generates even with errors
            
            if not all(str(record.get(key) or '').strip()
                       for key in ('order_type', 'cust_no', 'seller_no')):
                skipped.append(po_number)
                continue
            content, _, _ = _export_dataframe(
                history_df, 'dta', record.get('po_number', ''),
                record.get('ship_to', ''), record.get('order_type', ''),
                record.get('cust_no', ''), record.get('seller_no', ''),
                record.get('order_date', ''),
            )
            if content is None:
                skipped.append(po_number)
                continue
            sections.append(content.rstrip(b'\r\n') + b'\n')
            generated.append(po_number)

        if skipped:
            return Response(
                'DTA not generated. These selected POs need customer mapping details: '
                + ', '.join(skipped), status=400, mimetype='text/plain'
            )
        if not generated:
            return Response('No selected POs are ready for DTA generation.',
                            status=400, mimetype='text/plain')

        content = b''.join(sections)
        for po_number in generated:
            if not update_customer_po_status(po_number, 'generate'):
                app.logger.error('DTA created but failed to update Generate status for %s', po_number)
        return Response(
            content, mimetype='application/octet-stream',
            headers={'Content-Disposition': 'attachment; filename="Order.dta"'},
        )

    # Multiple CSV export:
    # combine every selected PO's item rows into ONE CSV table.
    if action == 'csv':
        combined_frames = []
        first_ship_to = ''
        invalid = []

        for po_number in dict.fromkeys(po_numbers):
            record = fetch_customer_po_by_number(po_number)
            if not record:
                continue

            try:
                mapped_data = json.loads(record.get('mapped_data') or '[]')
            except (TypeError, json.JSONDecodeError):
                mapped_data = []

            if not mapped_data:
                invalid.append(po_number)
                continue

            history_df = pd.DataFrame(mapped_data)

            # Reject the whole selection if any selected PO has mapping errors.
            if not _mapped_result_is_ok(history_df):
                invalid.append(po_number)
                continue

            # Make sure each row keeps the correct selected PO number.
            history_df['PO NO'] = po_number
            combined_frames.append(history_df)

            if not first_ship_to:
                first_ship_to = str(record.get('ship_to') or '').strip()

        if invalid:
            return Response(
                'CSV export blocked. These selected POs have mapping errors: ' + ', '.join(invalid),
                status=400, mimetype='text/plain'
            )

        for po_number in dict.fromkeys(po_numbers):
            update_customer_po_status(po_number, 'exported')

        if not combined_frames:
            return Response('No selected POs are ready for CSV export.', status=400, mimetype='text/plain')

        combined_df = pd.concat(combined_frames, ignore_index=True)

        export_columns = [
            'PO NO', 'SHIP TO LOCATION', 'DESCRIPTION', 'SKU', 'PG CODE',
            'QUANTITY', 'PO PRICE (RM)', 'DB PRICE (RM)', 'PO TOTAL (RM)',
            'CALCULATED TOTAL (RM)', 'REMARK',
        ]
        combined_df = combined_df.reindex(columns=export_columns).fillna('')

        # export_to_csv_bytes already creates the ECONSAVE reference layout.
        # Because all selected item rows are in one DataFrame, the exported
        # table contains multiple PO numbers in the same PO# column.
        content = export_to_csv_bytes(combined_df)
        filename = _csv_filename(first_ship_to)

        return Response(
            content,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    return redirect(url_for('customer_po'))


@app.route('/customer-po/delete', methods=['POST'])
def delete_customer_po():
    po_number = request.form.get('po_number', '').strip()

    if po_number:
        delete_customer_po_record(po_number)

    return redirect(url_for('customer_po'))


@app.route('/customer-po/re-export/<file_type>', methods=['GET'])
def customer_po_re_export(file_type):
    po_number = request.args.get('po_number', '').strip()
    record = fetch_customer_po_by_number(po_number)
    if not record:
        return redirect(url_for('customer_po'))

    try:
        mapped_data = json.loads(record.get('mapped_data') or '[]')
    except (TypeError, json.JSONDecodeError):
        mapped_data = []

    if not mapped_data:
        return redirect(url_for('customer_po'))

    history_df = pd.DataFrame(mapped_data)

    if file_type == 'dta':
        sync_customer_po_mappings()
        record = fetch_customer_po_by_number(po_number) or record
        if not all(str(record.get(k) or '').strip() for k in ('order_type', 'cust_no', 'seller_no')):
            return redirect(url_for('customer_po'))

    content, mimetype, filename = _export_dataframe(
        history_df,
        file_type,
        record.get('po_number', ''),
        record.get('ship_to', ''),
        record.get('order_type', ''),
        record.get('cust_no', ''),
        record.get('seller_no', ''),
        record.get('order_date', ''),
    )
    if content is None:
        return redirect(url_for('customer_po'))

    if file_type == 'csv':
        update_customer_po_status(po_number, 'exported')
    elif file_type == 'dta':
        update_customer_po_status(po_number, 'generate')

    return Response(
        content,
        mimetype=mimetype,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


if __name__ == '__main__':
    ensure_customer_po_table()
    ensure_user_table(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
    app.run(debug=True, port=5000)
