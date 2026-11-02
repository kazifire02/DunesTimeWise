"""Apply the requested admin credentials once per database."""

import os
import re
from decimal import Decimal
from pathlib import Path
from threading import Lock

from werkzeug.security import generate_password_hash


ADMIN_USERNAME = "Dunes_TimeWise"
MIGRATION_ID = "20261102_dunes_timewise_admin_login_v1"

_migration_complete = False
_migration_lock = Lock()


def migrate_admin_login(get_connection):
    """Keep the admin's staff ID and record the credential update atomically.

    Prefer the requested username, then the legacy ``admin`` account, then
    the sole admin. Otherwise create the requested admin account. Leave
    existing logins unchanged until the operator supplies the password.
    """
    password = os.environ.get("TIMEWISE_ADMIN_PASSWORD")
    if not password:
        return False

    con = get_connection()
    cursor = None
    try:
        cursor = con.cursor(dictionary=True)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS timewise_migrations (
                migration_id VARCHAR(100) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB
        """)
        con.start_transaction()
        # A competing worker waits on this key until we commit or roll back.
        # Keep this marker in the same transaction as the credential change.
        cursor.execute(
            "INSERT IGNORE INTO timewise_migrations (migration_id) VALUES (%s)",
            (MIGRATION_ID,)
        )
        if cursor.rowcount == 0:
            con.commit()
            return False

        cursor.execute("""
            SELECT staff_id, username, role
            FROM staff_users
            WHERE LOWER(role) = 'admin' OR LOWER(username) = LOWER(%s)
            ORDER BY staff_id
            FOR UPDATE
        """, (ADMIN_USERNAME,))
        accounts = cursor.fetchall()
        target = next((account for account in accounts
                       if account["username"].casefold() == ADMIN_USERNAME.casefold()), None)
        admins = [account for account in accounts
                  if (account["role"] or "").lower() == "admin"]

        if target and (target["role"] or "").lower() != "admin":
            raise RuntimeError(
                "Admin migration stopped: Dunes_TimeWise belongs to a non-admin account."
            )
        if target is None:
            target = next((account for account in admins
                           if account["username"].casefold() == "admin"), None)
        if target is None and len(admins) == 1:
            target = admins[0]
        password_hash = generate_password_hash(password)
        if target:
            cursor.execute("""
                UPDATE staff_users
                SET username = %s, password_hash = %s
                WHERE staff_id = %s
            """, (ADMIN_USERNAME, password_hash, target["staff_id"]))
        else:
            cursor.execute("""
                INSERT INTO staff_users (username, password_hash, role)
                VALUES (%s, %s, %s)
            """, (ADMIN_USERNAME, password_hash, "admin"))

        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        con.close()


def ensure_admin_login(get_connection):
    """Check the persisted migration marker once per app process."""
    global _migration_complete
    if not os.environ.get("TIMEWISE_ADMIN_PASSWORD"):
        return
    if _migration_complete:
        return
    with _migration_lock:
        if not _migration_complete:
            migrate_admin_login(get_connection)
            _migration_complete = True


# The export stays outside source control. Only these application tables are read.
_IMPORT_COLUMNS = {
    "late_arrivals": ("arrival_id", "student_name", "grade", "section", "arrival_time", "recorded_by", "created_at"),
    "staff_users": ("staff_id", "username", "password_hash", "role", "created_at"),
    "student_roster": ("student_no", "student_name", "grade", "section", "is_active"),
    "timewise_migrations": ("migration_id", "applied_at"),
}
_IMPORT_SCHEMA = {
    "late_arrivals": """CREATE TABLE IF NOT EXISTS late_arrivals (
        arrival_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        student_name VARCHAR(100) NOT NULL, grade VARCHAR(10) NOT NULL,
        section VARCHAR(10) NOT NULL, arrival_time DATETIME NOT NULL,
        recorded_by VARCHAR(100), created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "staff_users": """CREATE TABLE IF NOT EXISTS staff_users (
        staff_id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(20) NOT NULL DEFAULT 'staff',
        created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "student_roster": """CREATE TABLE IF NOT EXISTS student_roster (
        student_no VARCHAR(30) NOT NULL PRIMARY KEY,
        student_name VARCHAR(150) NOT NULL, grade VARCHAR(10) NOT NULL,
        section VARCHAR(10) NOT NULL, is_active TINYINT(1) NOT NULL DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "timewise_migrations": """CREATE TABLE IF NOT EXISTS timewise_migrations (
        migration_id VARCHAR(100) NOT NULL PRIMARY KEY,
        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
}


def _dump_statements(text):
    """Split a mysqldump export without treating quoted semicolons as SQL."""
    statement = []
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            statement.append(char)
            if char == "\\" and quote != "`" and index + 1 < len(text):
                index += 1
                statement.append(text[index])
            elif char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 1
                    statement.append(text[index])
                else:
                    quote = None
        elif char in "'\"`":
            quote = char
            statement.append(char)
        elif text.startswith("--", index) and (index + 2 == len(text) or text[index + 2].isspace()) or char == "#":
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
            statement.append(" ")
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unterminated SQL comment")
            index = end + 1
            statement.append(" ")
        elif char == ";":
            if "".join(statement).strip():
                yield "".join(statement).strip()
            statement = []
        else:
            statement.append(char)
        index += 1
    if quote:
        raise ValueError("Unterminated SQL string")
    if "".join(statement).strip():
        raise ValueError("Incomplete SQL statement")


def _dump_values(text):
    """Parse literals into bound parameters; never execute dump SQL."""
    index = 0
    escapes = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a"}
    def skip_space():
        nonlocal index
        while index < len(text) and text[index].isspace():
            index += 1
    while index < len(text):
        skip_space()
        if index >= len(text) or text[index] != "(":
            raise ValueError("Expected a row of SQL literals")
        index += 1
        row = []
        while True:
            skip_space()
            if index < len(text) and text[index] == "'":
                index += 1
                value = []
                while index < len(text):
                    char = text[index]
                    index += 1
                    if char == "\\":
                        if index >= len(text):
                            raise ValueError("Incomplete SQL escape")
                        escaped = text[index]
                        value.append(escapes.get(escaped, escaped))
                        index += 1
                    elif char == "'":
                        if index < len(text) and text[index] == "'":
                            value.append("'")
                            index += 1
                        else:
                            break
                    else:
                        value.append(char)
                else:
                    raise ValueError("Unterminated SQL literal")
                row.append("".join(value))
            else:
                end = index
                while end < len(text) and text[end] not in ",)":
                    end += 1
                token = text[index:end].strip()
                if token.upper() == "NULL":
                    row.append(None)
                elif re.fullmatch(r"[+-]?\d+", token):
                    row.append(int(token))
                elif re.fullmatch(r"[+-]?\d+\.\d+", token):
                    row.append(Decimal(token))
                else:
                    raise ValueError("Only SQL literals are allowed in imported rows")
                index = end
            skip_space()
            if index >= len(text):
                raise ValueError("Incomplete SQL row")
            separator = text[index]
            index += 1
            if separator == ")":
                break
            if separator != ",":
                raise ValueError("Invalid SQL value separator")
        yield tuple(row)
        skip_space()
        if index == len(text):
            return
        if text[index] != ",":
            raise ValueError("Invalid SQL row separator")
        index += 1
        skip_space()
        if index == len(text):
            raise ValueError("Incomplete SQL row list")


def read_database_export(sql_file):
    """Read supported data rows while ignoring DROP, LOCK, SET and dump DDL."""
    rows = {table: [] for table in _IMPORT_COLUMNS}
    declared_tables = set()
    for statement in _dump_statements(Path(sql_file).read_text(encoding="utf-8-sig")):
        create = re.match(r"CREATE TABLE `([a-z_]+)`", statement, re.IGNORECASE)
        if create:
            declared_tables.add(create.group(1))
        if not re.match(r"INSERT\b", statement, re.IGNORECASE):
            continue
        insert = re.fullmatch(r"INSERT INTO `([a-z_]+)` VALUES\s*(.*)", statement, re.IGNORECASE | re.DOTALL)
        if not insert or insert.group(1) not in rows:
            raise ValueError("Unsupported INSERT statement in export")
        table = insert.group(1)
        for row in _dump_values(insert.group(2)):
            if len(row) != len(_IMPORT_COLUMNS[table]):
                raise ValueError(f"Unexpected column count for {table}")
            rows[table].append(row)
    if not {"late_arrivals", "staff_users", "student_roster"}.issubset(declared_tables):
        raise ValueError("The file is not a complete latetrack export")
    return rows


def import_database_export(con, database, rows):
    """Create missing tables and import missing rows without overwriting data."""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", database):
        raise ValueError("Use letters, numbers and underscores in the database name")
    cursor = con.cursor()
    try:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4")
        con.database = database
        for schema in _IMPORT_SCHEMA.values():
            cursor.execute(schema)
        con.start_transaction()
        counts = {}
        for table, columns in _IMPORT_COLUMNS.items():
            primary_key = columns[0]
            column_sql = ", ".join(f"`{column}`" for column in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            # The self-assignment skips primary/unique-key conflicts without
            # hiding conversion errors or replacing existing accounts/records.
            query = (f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders}) "
                     f"ON DUPLICATE KEY UPDATE `{primary_key}` = `{table}`.`{primary_key}`")
            inserted = 0
            for row in rows[table]:
                cursor.execute(query, row)
                inserted += cursor.rowcount == 1
            counts[table] = {"inserted": inserted, "skipped": len(rows[table]) - inserted}
        con.commit()
        return counts
    except Exception:
        con.rollback()
        raise
    finally:
        cursor.close()
