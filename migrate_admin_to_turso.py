"""
One-time migration: copy only the existing admin account from local SQLite to Turso.

Run this from the project folder after creating the Turso database and setting:
    TURSO_DATABASE_URL
    TURSO_AUTH_TOKEN

The script initializes the empty Turso schema, then copies the admin row from
instance/marketplace.db. Buyer/seller accounts and all test marketplace data
are intentionally not copied.
"""
import os
import sqlite3
import sys

from database import get_db_connection, init_db


def main():
    source_path = os.path.join(os.path.dirname(__file__), "instance", "marketplace.db")
    if not os.path.isfile(source_path):
        raise SystemExit(f"Local SQLite database not found: {source_path}")

    if not os.environ.get("TURSO_DATABASE_URL") or not os.environ.get("TURSO_AUTH_TOKEN"):
        raise SystemExit(
            "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN before running this script."
        )

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    target = None

    try:
        admin = source.execute(
            "SELECT * FROM users WHERE role = 'admin' AND COALESCE(is_deleted, 0) = 0 "
            "ORDER BY id LIMIT 1"
        ).fetchone()

        if admin is None:
            raise SystemExit("No active admin account was found in the local database.")

        init_db()
        target = get_db_connection()

        existing = target.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        if existing:
            raise SystemExit(
                "Turso already contains an admin account. No records were changed."
            )

        target_columns = {
            row["name"]
            for row in target.execute("PRAGMA table_info(users)").fetchall()
        }
        source_columns = set(admin.keys())
        shared_columns = [
            name for name in admin.keys() if name in target_columns
        ]

        required = {"id", "full_name", "email", "password_hash", "role"}
        if not required.issubset(shared_columns):
            raise RuntimeError(
                "The local and Turso users tables do not share all required admin fields."
            )

        placeholders = ", ".join("?" for _ in shared_columns)
        column_sql = ", ".join(f'"{name}"' for name in shared_columns)
        values = tuple(admin[name] for name in shared_columns)

        target.execute(
            f"INSERT INTO users ({column_sql}) VALUES ({placeholders})",
            values,
        )
        target.commit()

        copied = target.execute(
            "SELECT id, full_name, email, role FROM users WHERE id = ?",
            (admin["id"],),
        ).fetchone()
        if not copied or copied["role"] != "admin":
            raise RuntimeError("Admin verification failed after copying.")

        print("SUCCESS: The existing admin account was copied to Turso.")
        print(f"Admin name: {copied['full_name']}")
        print(f"Admin email: {copied['email']}")
        print("Password hash preserved. No buyer/seller/test records were copied.")

    finally:
        source.close()
        if target is not None:
            target.close()


if __name__ == "__main__":
    main()
