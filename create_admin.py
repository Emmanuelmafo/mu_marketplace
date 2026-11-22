
from getpass import getpass
from werkzeug.security import generate_password_hash

from database import get_db_connection, init_db


def create_admin():

    init_db()

    print("\n===== MU MARKETPLACE ADMIN SETUP =====\n")

    full_name = input("Enter admin full name: ").strip()
    email = input("Enter admin email: ").strip().lower()
    phone = input("Enter admin phone number: ").strip()

    if not full_name or not email or not phone:
        print("\nAll fields are required.")
        return

    password = getpass("Create admin password: ")
    confirm_password = getpass("Confirm admin password: ")

    if len(password) < 8:
        print("\nPassword must contain at least 8 characters.")
        return

    if password != confirm_password:
        print("\nPasswords do not match.")
        return

    connection = get_db_connection()

    try:
        existing_admin = connection.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()

        if existing_admin:
            print("\nAn admin account already exists.")
            return

        password_hash = generate_password_hash(password)

        connection.execute(
            """
            INSERT INTO users (
                full_name,
                email,
                phone,
                password_hash,
                role
            )
            VALUES (?, ?, ?, ?, 'admin')
            """,
            (
                full_name,
                email,
                phone,
                password_hash
            )
        )

        connection.commit()

        print("\nAdmin account created successfully!")
        print("You can now log in using your admin email and password.")

    except Exception as error:
        connection.rollback()
        print("\nCould not create admin account:", error)

    finally:
        connection.close()


if __name__ == "__main__":
    create_admin()
