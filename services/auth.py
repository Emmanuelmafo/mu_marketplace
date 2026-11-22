
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from database import get_db_connection


def register_user(full_name, email, phone, password, role="buyer"):

    # Public registration must never create admin accounts.
    if role not in ("buyer", "seller"):
        return False, "Invalid account type."

    phone = (phone or "").strip()
    if not full_name or not email or not phone or not password:
        return False, "Full name, email, phone number and password are required."

    if len(phone) < 7:
        return False, "Please enter a valid phone number."

    if len(password) < 8:
        return False, "Password must contain at least 8 characters."

    email = email.lower().strip()

    connection = get_db_connection()

    try:
        password_hash = generate_password_hash(password)

        connection.execute("""
            INSERT INTO users
            (full_name, email, phone, password_hash, role)
            VALUES (?, ?, ?, ?, ?)
        """, (
            full_name.strip(),
            email,
            phone,
            password_hash,
            role
        ))

        connection.commit()

        return True, "Account created successfully!"

    except Exception as error:

        if "UNIQUE constraint failed" in str(error):
            return False, "This email already has an account of this type."

        return False, "Registration failed."

    finally:
        connection.close()


def login_user(email, password, role):

    # Admin login is allowed, but admin registration is not.
    if role not in ("buyer", "seller", "admin"):
        return None

    connection = get_db_connection()

    try:
        user = connection.execute("""
            SELECT *
            FROM users
            WHERE email = ? AND role = ? AND COALESCE(is_deleted, 0) = 0
        """, (
            email.lower().strip(),
            role
        )).fetchone()

        if user and check_password_hash(
            user["password_hash"], password
        ):
            return dict(user)

        return None

    finally:
        connection.close()


# Get account information
def get_user_account(user_id):

    connection = get_db_connection()

    try:
        user = connection.execute("""
            SELECT
                id,
                full_name,
                email,
                phone,
                profile_picture,
                role,
                created_at
            FROM users
            WHERE id = ?
        """, (user_id,)).fetchone()

        return dict(user) if user else None

    finally:
        connection.close()


# Update account information
def update_user_account(user_id, full_name, email, phone):

    if not full_name or not email:
        return False, "Full name and email are required."

    full_name = full_name.strip()
    email = email.lower().strip()
    phone = phone.strip() if phone else ""

    connection = get_db_connection()

    try:
        user = connection.execute("""
            SELECT role
            FROM users
            WHERE id = ?
        """, (user_id,)).fetchone()

        if not user:
            return False, "Account not found."

        connection.execute("""
            UPDATE users
            SET full_name = ?,
                email = ?,
                phone = ?
            WHERE id = ?
        """, (
            full_name,
            email,
            phone,
            user_id
        ))

        connection.commit()

        return True, "Account information updated successfully!"

    except Exception as error:

        if "UNIQUE constraint failed" in str(error):
            return False, "This email is already used by another account of this type."

        return False, "Unable to update account information."

    finally:
        connection.close()


# Change account password
def change_user_password(user_id, current_password, new_password):

    if not current_password or not new_password:
        return False, "Please complete all password fields."

    if len(new_password) < 8:
        return False, "New password must contain at least 8 characters."

    connection = get_db_connection()

    try:
        user = connection.execute("""
            SELECT password_hash
            FROM users
            WHERE id = ?
        """, (user_id,)).fetchone()

        if not user:
            return False, "Account not found."

        if not check_password_hash(
            user["password_hash"],
            current_password
        ):
            return False, "Your current password is incorrect."

        password_hash = generate_password_hash(new_password)

        connection.execute("""
            UPDATE users
            SET password_hash = ?
            WHERE id = ?
        """, (
            password_hash,
            user_id
        ))

        connection.commit()

        return True, "Password changed successfully!"

    finally:
        connection.close()


def create_password_reset(email, role):

    if role not in ("buyer", "seller"):
        return None

    connection = get_db_connection()

    try:
        user = connection.execute("""
            SELECT id
            FROM users
            WHERE email = ? AND role = ?
        """, (
            email.lower().strip(),
            role
        )).fetchone()

        if not user:
            return None

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(
            token.encode()
        ).hexdigest()

        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat()

        connection.execute("""
            UPDATE password_resets
            SET used_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND used_at IS NULL
        """, (user["id"],))

        connection.execute("""
            INSERT INTO password_resets
            (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
        """, (
            user["id"],
            token_hash,
            expires_at
        ))

        connection.commit()

        return token

    finally:
        connection.close()


def reset_password(token, new_password):

    if not token or len(new_password) < 4:
        return False, "Password must contain at least 8 characters."

    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    connection = get_db_connection()

    try:
        reset = connection.execute("""
            SELECT *
            FROM password_resets
            WHERE token_hash = ?
              AND used_at IS NULL
        """, (token_hash,)).fetchone()

        if not reset:
            return False, "Invalid or already used reset link."

        expiry = datetime.fromisoformat(
            reset["expires_at"]
        )

        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        if datetime.now(timezone.utc) >= expiry:
            return False, "Password reset link has expired."

        password_hash = generate_password_hash(new_password)

        connection.execute("""
            UPDATE users
            SET password_hash = ?
            WHERE id = ?
        """, (
            password_hash,
            reset["user_id"]
        ))

        connection.execute("""
            UPDATE password_resets
            SET used_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (reset["id"],))

        connection.commit()

        return True, "Password updated successfully!"

    finally:
        connection.close()
