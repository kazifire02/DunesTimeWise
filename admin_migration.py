"""Apply the requested admin credentials once per database."""

import os
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
