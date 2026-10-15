import logging
import os

import mysql.connector
import pyodbc
from werkzeug.security import generate_password_hash


logger = logging.getLogger(__name__)


# =========================================================
# CONFIGURATION
# =========================================================

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "P@ssw0rd131!")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "PO")

MSSQL_SERVER = os.getenv("MSSQL_SERVER", "dysys")
MSSQL_DATABASE = os.getenv("MSSQL_DATABASE", "MERAPP")
MSSQL_USER = os.getenv("MSSQL_USER", "readme")
MSSQL_PASSWORD = os.getenv("MSSQL_PASSWORD", "P@ssw0rd414735!")

DRIVER_CANDIDATES = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
]


# =========================================================
# CONNECTIONS
# =========================================================

def get_db_connection():
    """Return a connection to the local MySQL PO database."""
    try:
        return mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
        )
    except Exception as exc:
        logger.error("MySQL connection error: %s", exc)
        return None


def get_mssql_connection():
    """Return a SQL-authenticated connection to MERAPP."""
    last_error = None

    for driver in DRIVER_CANDIDATES:
        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={MSSQL_SERVER};"
            f"DATABASE={MSSQL_DATABASE};"
            f"UID={MSSQL_USER};"
            f"PWD={MSSQL_PASSWORD};"
            "TrustServerCertificate=yes;"
        )

        try:
            return pyodbc.connect(conn_str, timeout=5)
        except Exception as exc:
            last_error = exc

    logger.error("MS SQL Server connection error: %s", last_error)
    return None


# =========================================================
# CUSTOMER MAPPING - MYSQL
# =========================================================

def fetch_customer_records():
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT no, cust_code, sl_cust_code, seller, order_type, update_by FROM customer ORDER BY no"
        )
        return cursor.fetchall()
    except Exception as exc:
        logger.error("Error fetching customer records: %s", exc)
        return []
    finally:
        conn.close()

def fetch_seller_by_customer_id(cust_id):
    """Look up Seller ID in MSSQL Customer using SL Customer Code."""
    cust_id = str(cust_id or "").strip()
    if not cust_id:
        return None

    conn = get_mssql_connection()
    if not conn:
        return None

    query = """
        SELECT TOP 1 cust.SlsperId
        FROM Customer cust
        WHERE cust.CustId = ?
    """
    try:
        cursor = conn.cursor()
        cursor.execute(query, (cust_id,))
        row = cursor.fetchone()
        return str(row[0]).strip() or None if row and row[0] is not None else None
    except Exception as exc:
        logger.error("Error fetching Seller ID for CustId %s: %s", cust_id, exc)
        return None
    finally:
        conn.close()


def save_customer_record(cust_code, sl_cust_code, seller, order_type, update_by):
    """Save a customer mapping to MySQL."""
    conn = get_db_connection()
    if not conn:
        return False, "Database connection failed."

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT no FROM customer WHERE cust_code = %s LIMIT 1", (cust_code,))
        if cursor.fetchone():
            return False, f"Customer Code {cust_code} already exists."

        cursor.execute("SELECT no FROM customer WHERE sl_cust_code = %s LIMIT 1", (sl_cust_code,))
        if cursor.fetchone():
            return False, f"SL Customer Code {sl_cust_code} already exists."

        cursor.execute("""
            INSERT INTO customer (cust_code, sl_cust_code, seller, order_type, update_by)
            VALUES (%s, %s, %s, %s, %s)
        """, (cust_code, sl_cust_code, seller, order_type, update_by))
        conn.commit()
        return True, ""
    except Exception as exc:
        logger.error("Error saving customer record: %s", exc)
        conn.rollback()
        return False, "Unable to save customer."
    finally:
        conn.close()


def update_customer_record(record_no, cust_code, sl_cust_code, seller, order_type, update_by):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE customer
            SET cust_code = %s,
                sl_cust_code = %s,
                seller = %s,
                order_type = %s,
                update_by = %s
            WHERE no = %s
            """,
            (cust_code, sl_cust_code, seller, order_type, update_by, record_no),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error updating customer record: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_customer_record(record_no):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM customer WHERE no = %s", (record_no,))
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error deleting customer record: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def fetch_customer_mapping_for_po(po_number):
    
    po_text = str(po_number or "").strip().upper()
    if not po_text:
        return None

    po_prefix = po_text.split("-", 1)[0].strip()
    records = fetch_customer_records()
    starts_with_matches = []

    for record in records:
        cust_code = str(record.get("cust_code") or "").strip().upper()
        if not cust_code:
            continue

        if cust_code in {po_prefix, po_text}:
            return record

        if po_text.startswith(cust_code):
            starts_with_matches.append((len(cust_code), record))

    if starts_with_matches:
        starts_with_matches.sort(key=lambda item: item[0], reverse=True)
        return starts_with_matches[0][1]

    return None


def sync_customer_po_mappings():
    """Refresh saved PO customer fields using the current Customer Mapping table.

    Existing PO history remains saved even when no customer mapping matches.
    """
    if not ensure_customer_po_table():
        return False

    po_records = fetch_customer_po_records()
    if not po_records:
        return True

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        for po in po_records:
            po_number = str(po.get("po_number") or "").strip()
            if not po_number:
                continue
            mapping = fetch_customer_mapping_for_po(po_number)
            if not mapping:
                continue

            cursor.execute(
                """UPDATE customer_po
                   SET order_type = %s, cust_no = %s, seller_no = %s
                   WHERE po_number = %s""",
                (
                    mapping.get("order_type"),
                    mapping.get("sl_cust_code"),
                    mapping.get("seller"),
                    po_number,
                ),
            )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error syncing Customer PO mappings: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


# =========================================================
# ITEM MASTER - MYSQL
# =========================================================

def fetch_item_records():
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT no, item_code, pg_code, description, price, update_by FROM item ORDER BY no"
        )
        return cursor.fetchall()
    except Exception as exc:
        logger.error("Error fetching item records: %s", exc)
        return []
    finally:
        conn.close()


def save_item_record(item_code, pg_code, description, price, update_by):
    conn = get_db_connection()
    if not conn:
        return False, "Database connection failed."

    try:
        cursor = conn.cursor()

        # Check duplicate Item Code
        cursor.execute(
            "SELECT no FROM item WHERE item_code = %s LIMIT 1",
            (item_code,)
        )

        if cursor.fetchone():
            return False, f'Item Code {item_code} already exists.'

        # Check duplicate PG Code
        cursor.execute(
            "SELECT no FROM item WHERE pg_code = %s LIMIT 1",
            (pg_code,)
        )

        if cursor.fetchone():
            return False, f'PG Code {pg_code} already exists.'

        # No duplicate - save item
        cursor.execute(
            """
            INSERT INTO item
                (item_code, pg_code, description, price, update_by)
            VALUES
                (%s, %s, %s, %s, %s)
            """,
            (item_code, pg_code, description, price, update_by),
        )

        conn.commit()
        return True, ""

    except Exception as exc:
        logger.error("Error saving item record: %s", exc)
        conn.rollback()
        return False, "Unable to save item."

    finally:
        conn.close()


def update_item_record(record_no, item_code, pg_code, description, price, update_by):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE item
            SET item_code = %s,
                pg_code = %s,
                description = %s,
                price = %s,
                update_by = %s
            WHERE no = %s
            """,
            (item_code, pg_code, description, price, update_by, record_no),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error updating item record: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_item_record(record_no):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM item WHERE no = %s", (record_no,))
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error deleting item record: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


# =========================================================
# MICROSOFT SQL SERVER ITEM / PRICE LOOKUP
# =========================================================

def fetch_mapped_item_details(item_code, conn=None):
    """Look up an item against INVTID or IB_ITEMCODE1-4 in MERAPP."""
    owns_conn = conn is None

    if owns_conn:
        conn = get_mssql_connection()

    if not conn:
        return None

    query = """
        SELECT TOP 1
            inv.IB_ITEMCODE1,
            inv.IB_DESC2,
            uom.ToUnit,
            det.DiscPrice
        FROM xtcwt_INVENTORYX inv
        LEFT JOIN INUnit uom
            ON inv.INVTID = uom.UnitID
        LEFT JOIN SlsPrc prc
            ON inv.INVTID = prc.INVTID
        LEFT JOIN SlsPrcDet det
            ON prc.SlsPrcID = det.SlsPrcID
        WHERE inv.INVTID = ?
           OR inv.IB_ITEMCODE1 = ?
           OR inv.IB_ITEMCODE2 = ?
           OR inv.IB_ITEMCODE3 = ?
           OR inv.IB_ITEMCODE4 = ?
    """

    try:
        clean_code = str(item_code).strip()
        cursor = conn.cursor()
        cursor.execute(query, (clean_code,) * 5)
        return cursor.fetchone()
    except Exception as exc:
        logger.error("MS SQL item lookup error: %s", exc)
        return None
    finally:
        if owns_conn and conn:
            conn.close()


def fetch_item_details_from_mssql(pg_code):
    """
    Return the PCS DiscPrice for price level 2-2-2.

    PG Code -> SlsPrc.InvtID -> SelectFld2 2-2-2
            -> SlsPrcDet.SlsUnit PCS -> DiscPrice
    """
    if not pg_code:
        return {"price": "0.00"}

    conn = get_mssql_connection()
    if not conn:
        return {"price": "0.00"}

    query = """
        SELECT TOP 1 det.DiscPrice
        FROM SlsPrc prc
        INNER JOIN SlsPrcDet det
            ON prc.SlsPrcID = det.SlsPrcID
        WHERE RTRIM(LTRIM(prc.InvtID)) = ?
          AND RTRIM(LTRIM(prc.SelectFld2)) = '2-2-2'
          AND RTRIM(LTRIM(det.SlsUnit)) = 'PCS'
          AND det.DiscPrice IS NOT NULL
    """

    try:
        clean_pg = str(pg_code).strip()
        cursor = conn.cursor()
        cursor.execute(query, (clean_pg,))
        row = cursor.fetchone()

        if row and row[0] is not None:
            return {"price": f"{float(row[0]):.2f}"}

        logger.warning("No PCS price found for PG Code: %s", clean_pg)
        return {"price": "0.00"}
    except Exception as exc:
        logger.error("Error fetching price for PG Code %s: %s", pg_code, exc)
        return {"price": "0.00"}
    finally:
        conn.close()




# =========================================================
# USER ACCOUNTS - MYSQL
# =========================================================

def ensure_user_table(default_admin_username="admin", default_admin_password="MasterPO123"):
    """
    Create the users table when missing and seed one Admin account
    only when the table has no users yet.
    """
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                no INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'user',
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute("SELECT COUNT(*) AS total FROM users")
        row = cursor.fetchone() or {}
        total = int(row.get("total") or 0)

        if total == 0:
            cursor.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active)
                VALUES (%s, %s, 'admin', 1)
                """,
                (
                    str(default_admin_username).strip(),
                    generate_password_hash(str(default_admin_password)),
                ),
            )

        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error ensuring users table: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def fetch_user_accounts():
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT no, username, role, is_active, created_at, updated_at
            FROM users
            ORDER BY
                CASE WHEN role = 'admin' THEN 0 ELSE 1 END,
                username
            """
        )
        return cursor.fetchall()
    except Exception as exc:
        logger.error("Error fetching user accounts: %s", exc)
        return []
    finally:
        conn.close()


def fetch_user_by_username(username):
    username = str(username or "").strip()
    if not username:
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT no, username, password_hash, role, is_active
            FROM users
            WHERE username = %s
            LIMIT 1
            """,
            (username,),
        )
        return cursor.fetchone()
    except Exception as exc:
        logger.error("Error fetching user %s: %s", username, exc)
        return None
    finally:
        conn.close()


def create_user_account(username, password, role="user", is_active=True):
    username = str(username or "").strip()
    password = str(password or "")
    role = str(role or "user").strip().lower()

    if not username or not password:
        return False, "Username and password are required."

    if role not in {"admin", "user"}:
        role = "user"

    conn = get_db_connection()
    if not conn:
        return False, "Database connection failed."

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, role, is_active)
            VALUES (%s, %s, %s, %s)
            """,
            (
                username,
                generate_password_hash(password),
                role,
                1 if is_active else 0,
            ),
        )
        conn.commit()
        return True, ""
    except mysql.connector.IntegrityError:
        conn.rollback()
        return False, "Username already exists."
    except Exception as exc:
        logger.error("Error creating user %s: %s", username, exc)
        conn.rollback()
        return False, "Unable to create user account."
    finally:
        conn.close()


def update_user_account(record_no, username, role, is_active, password=""):
    username = str(username or "").strip()
    role = str(role or "user").strip().lower()
    password = str(password or "")

    if not username:
        return False, "Username is required."

    if role not in {"admin", "user"}:
        role = "user"

    conn = get_db_connection()
    if not conn:
        return False, "Database connection failed."

    try:
        cursor = conn.cursor()

        if password:
            cursor.execute(
                """
                UPDATE users
                SET username=%s,
                    role=%s,
                    is_active=%s,
                    password_hash=%s
                WHERE no=%s
                """,
                (
                    username,
                    role,
                    1 if is_active else 0,
                    generate_password_hash(password),
                    record_no,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE users
                SET username=%s,
                    role=%s,
                    is_active=%s
                WHERE no=%s
                """,
                (
                    username,
                    role,
                    1 if is_active else 0,
                    record_no,
                ),
            )

        conn.commit()
        return True, ""
    except mysql.connector.IntegrityError:
        conn.rollback()
        return False, "Username already exists."
    except Exception as exc:
        logger.error("Error updating user %s: %s", record_no, exc)
        conn.rollback()
        return False, "Unable to update user account."
    finally:
        conn.close()


def delete_user_account(record_no):
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE no=%s", (record_no,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        logger.error("Error deleting user %s: %s", record_no, exc)
        conn.rollback()
        return False
    finally:
        conn.close()

# =========================================================
# CUSTOMER PO HISTORY - MYSQL
# =========================================================

def ensure_customer_po_table():
    """
    Ensure the Customer PO table is compatible with the application.

    The user-created table is kept. This function only creates it if missing
    and adds mapped_data / ship_to when those re-export fields are absent.
    """
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_po (
                no INT AUTO_INCREMENT PRIMARY KEY,
                po_number VARCHAR(100) NOT NULL UNIQUE,
                total DECIMAL(12,2) DEFAULT 0.00,
                order_type VARCHAR(50),
                cust_no VARCHAR(50),
                seller_no VARCHAR(50),
                order_date DATE,
                delivery_date DATE,
                exported VARCHAR(20) DEFAULT 'Pending',
                generate VARCHAR(20) DEFAULT 'Pending',
                mapped_data LONGTEXT NULL,
                ship_to TEXT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute("SHOW COLUMNS FROM customer_po")
        existing_columns = {row["Field"] for row in cursor.fetchall()}

        if "mapped_data" not in existing_columns:
            cursor.execute("ALTER TABLE customer_po ADD COLUMN mapped_data LONGTEXT NULL")

        if "ship_to" not in existing_columns:
            cursor.execute("ALTER TABLE customer_po ADD COLUMN ship_to TEXT NULL")

        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error checking customer_po table: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def save_customer_po_record(
    po_number,
    total,
    order_type,
    cust_no,
    seller_no,
    order_date,
    delivery_date,
    mapped_data,
    ship_to="",
):
    """Insert a new PO or refresh its stored details without resetting statuses."""
    if not ensure_customer_po_table():
        return False

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO customer_po (
                po_number,
                total,
                order_type,
                cust_no,
                seller_no,
                order_date,
                delivery_date,
                mapped_data,
                ship_to
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total = VALUES(total),
                order_type = VALUES(order_type),
                cust_no = VALUES(cust_no),
                seller_no = VALUES(seller_no),
                order_date = VALUES(order_date),
                delivery_date = VALUES(delivery_date),
                mapped_data = VALUES(mapped_data),
                ship_to = VALUES(ship_to)
            """,
            (
                po_number,
                total,
                order_type,
                cust_no,
                seller_no,
                order_date or None,
                delivery_date or None,
                mapped_data,
                ship_to,
            ),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error saving customer PO record: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def fetch_customer_po_records():
    if not ensure_customer_po_table():
        return []

    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                no,
                po_number,
                total,
                order_type,
                cust_no,
                seller_no,
                order_date,
                delivery_date,
                exported,
                generate,
                mapped_data,
                ship_to,
                updated_at
            FROM customer_po
            ORDER BY updated_at DESC, no DESC
            """
        )
        return cursor.fetchall()
    except Exception as exc:
        logger.error("Error fetching customer PO records: %s", exc)
        return []
    finally:
        conn.close()



def delete_customer_po_record(po_number):
    """Delete one Customer PO history record by PO number."""
    po_number = str(po_number or "").strip()
    if not po_number:
        return False

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM customer_po WHERE po_number = %s",
            (po_number,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        logger.error("Error deleting Customer PO %s: %s", po_number, exc)
        conn.rollback()
        return False
    finally:
        conn.close()


def fetch_customer_po_by_number(po_number):
    if not ensure_customer_po_table():
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM customer_po WHERE po_number = %s LIMIT 1",
            (po_number,),
        )
        return cursor.fetchone()
    except Exception as exc:
        logger.error("Error fetching customer PO %s: %s", po_number, exc)
        return None
    finally:
        conn.close()


def update_customer_po_status(po_number, status_type):
    """Mark a PO as Exported (CSV) or Generated (DTA)."""
    status_values = {
        "exported": "Exported",
        "generate": "Generated",
    }

    if status_type not in status_values:
        return False

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        sql = f"UPDATE customer_po SET {status_type} = %s WHERE po_number = %s"
        cursor.execute(sql, (status_values[status_type], po_number))
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Error updating customer PO status: %s", exc)
        conn.rollback()
        return False
    finally:
        conn.close()
