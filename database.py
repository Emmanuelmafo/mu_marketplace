import os
import sqlite3

from config import Config


class _CompatRow(dict):
    """SQLite-style row supporting both row['column'] and row[0]."""

    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


class _CompatCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    def _convert(self, row):
        if row is None:
            return None
        description = self.description or ()
        columns = [item[0] for item in description]
        if isinstance(row, dict):
            return _CompatRow(columns or list(row.keys()), list(row.values()))
        return _CompatRow(columns, row)

    def fetchone(self):
        return self._convert(self._cursor.fetchone())

    def fetchall(self):
        return [self._convert(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [self._convert(row) for row in rows]

    def __iter__(self):
        for row in self._cursor:
            yield self._convert(row)


class _TursoConnection:
    """Small DB-API compatibility layer for the remote libSQL Python client."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        # Foreign-key enforcement is enabled by the hosted SQLite-compatible
        # database. Ignore connection-local toggles that are not meaningful
        # for the remote HTTP connection.
        normalized = " ".join(sql.strip().lower().split())
        if normalized in ("pragma foreign_keys = on", "pragma foreign_keys = off"):
            return _CompatCursor(self._connection.execute("SELECT 1"))
        return _CompatCursor(self._connection.execute(sql, parameters or ()))

    def executemany(self, sql, seq_of_parameters):
        return _CompatCursor(self._connection.executemany(sql, seq_of_parameters))

    def executescript(self, script):
        # libSQL's HTTP client does not expose sqlite3.executescript().
        # Execute complete SQLite statements individually instead.
        statement = ""
        for line in script.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                if sql:
                    self.execute(sql)
                statement = ""
        if statement.strip():
            self.execute(statement)
        return None

    def cursor(self):
        return _CompatCursor(self._connection.cursor())

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()


def get_db_connection():
    """Use Turso in production and the existing local SQLite file in development."""
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")

    if turso_url:
        if not turso_token:
            raise RuntimeError(
                "TURSO_AUTH_TOKEN is required when TURSO_DATABASE_URL is configured."
            )
        try:
            import libsql
        except ImportError as error:
            raise RuntimeError(
                "The libsql package is missing. Install the project requirements."
            ) from error

        connection = libsql.connect(
            database=turso_url,
            auth_token=turso_token,
        )
        return _TursoConnection(connection)

    if os.environ.get("VERCEL"):
        raise RuntimeError(
            "Turso is not configured. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN "
            "in the Vercel project environment variables."
        )

    os.makedirs(Config.INSTANCE_DIR, exist_ok=True)
    connection = sqlite3.connect(Config.DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate_users_table(connection):
    """
    Remove global email uniqueness and replace it
    with email + role uniqueness.
    Preserve existing user IDs and related records.
    """

    columns = connection.execute(
        "PRAGMA table_info(users)"
    ).fetchall()

    if not columns:
        return

    indexes = connection.execute(
        "PRAGMA index_list(users)"
    ).fetchall()

    email_only_unique = False

    for index in indexes:
        if index["unique"]:
            index_columns = connection.execute(
                f"PRAGMA index_info('{index['name']}')"
            ).fetchall()

            names = [col["name"] for col in index_columns]

            if names == ["email"]:
                email_only_unique = True

    if not email_only_unique:
        return

    duplicates = connection.execute("""
        SELECT email, role
        FROM users
        GROUP BY email, role
        HAVING COUNT(*) > 1
    """).fetchall()

    if duplicates:
        raise RuntimeError(
            "Duplicate email and role combinations must be "
            "resolved before migration."
        )

    connection.execute("PRAGMA foreign_keys = OFF")

    try:
        connection.execute("BEGIN")

        connection.execute("""
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                profile_picture TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'buyer'
                    CHECK(role IN ('buyer', 'seller', 'admin')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(email, role)
            )
        """)

        connection.execute("""
            INSERT INTO users_new (
                id, full_name, email, phone,
                password_hash, role, created_at
            )
            SELECT
                id, full_name, email, phone,
                password_hash, role, created_at
            FROM users
        """)

        connection.execute("DROP TABLE users")

        connection.execute("""
            ALTER TABLE users_new RENAME TO users
        """)

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def migrate_products_table(connection):
    """
    Add pickup_location to existing products tables
    without deleting or changing existing products.
    """

    columns = connection.execute(
        "PRAGMA table_info(products)"
    ).fetchall()

    column_names = [column["name"] for column in columns]

    if "pickup_location" not in column_names:
        connection.execute("""
            ALTER TABLE products
            ADD COLUMN pickup_location TEXT
        """)

        connection.commit()



def migrate_order_tracking_schema(connection):
    """Add order-item snapshots needed for private, reliable order tracking."""
    columns = connection.execute("PRAGMA table_info(order_items)").fetchall()
    names = [column["name"] for column in columns]
    additions = {
        "seller_id": "INTEGER",
        "pickup_location_snapshot": "TEXT",
        "product_name_snapshot": "TEXT"
    }
    for column_name, column_type in additions.items():
        if column_name not in names:
            connection.execute(
                f"ALTER TABLE order_items ADD COLUMN {column_name} {column_type}"
            )

    # Backfill snapshots for any older order rows, where the product still exists.
    connection.execute("""
        UPDATE order_items
        SET seller_id = (SELECT seller_id FROM products WHERE products.id = order_items.product_id)
        WHERE seller_id IS NULL
    """)
    connection.execute("""
        UPDATE order_items
        SET pickup_location_snapshot = COALESCE(
            (SELECT pickup_location FROM products WHERE products.id = order_items.product_id), ''
        )
        WHERE pickup_location_snapshot IS NULL
    """)
    connection.execute("""
        UPDATE order_items
        SET product_name_snapshot = (
            SELECT name FROM products WHERE products.id = order_items.product_id
        )
        WHERE product_name_snapshot IS NULL
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_notifications_user
        ON notifications(user_id, created_at)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_items_seller
        ON order_items(seller_id, order_id)
    """)




def migrate_commission_amounts(connection):
    """
    Ensure a commission never exceeds the value of the products sold.
    Existing commission statuses are preserved; only excessive amounts
    are reduced. Safe to run on every application startup.
    """
    import math

    rows = connection.execute("""
        SELECT
            c.id AS commission_id,
            c.amount AS current_amount,
            oi.quantity,
            oi.unit_price
        FROM commissions c
        JOIN order_items oi ON oi.order_id = c.order_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE c.seller_id = COALESCE(oi.seller_id, p.seller_id)
    """).fetchall()

    commission_caps = {}
    for row in rows:
        price = max(float(row["unit_price"] or 0), 0)
        quantity = max(int(row["quantity"] or 0), 0)

        if price < 50:
            tier_commission = 5.0
        elif price < 100:
            tier_commission = 10.0
        else:
            tier_commission = 10.0 + 5.0 * math.ceil(price / 100.0)

        capped_unit_commission = min(tier_commission, price)
        key = row["commission_id"]
        # K20-and-below products incur one commission per order line,
        # regardless of how many units were ordered.
        commission_quantity = 1 if price <= 20 else quantity
        commission_caps[key] = commission_caps.get(key, 0.0) + (
            capped_unit_commission * commission_quantity
        )

    for commission_id, maximum_amount in commission_caps.items():
        connection.execute("""
            UPDATE commissions
            SET amount = ?
            WHERE id = ? AND amount > ?
        """, (round(maximum_amount, 2), commission_id, round(maximum_amount, 2)))


def init_db():
    connection = get_db_connection()

    try:
        connection.execute("PRAGMA foreign_keys = OFF")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'buyer'
                    CHECK(role IN ('buyer', 'seller', 'admin')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(email, role)
            )
        """)

        migrate_users_table(connection)

        # Add seller profile-picture support to existing databases safely.
        user_columns = connection.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
        user_column_names = [column["name"] for column in user_columns]
        if "profile_picture" not in user_column_names:
            connection.execute(
                "ALTER TABLE users ADD COLUMN profile_picture TEXT"
            )
        if "is_deleted" not in user_column_names:
            connection.execute(
                "ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
            )

        connection.executescript("""

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            category_id INTEGER,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL CHECK(price >= 0),
            stock INTEGER DEFAULT 1,
            image TEXT,
            pickup_location TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (seller_id) REFERENCES users(id),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (product_id) REFERENCES products(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cart_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1
                CHECK(quantity > 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(buyer_id, product_id),

            FOREIGN KEY (buyer_id) REFERENCES users(id)
                ON DELETE CASCADE,

            FOREIGN KEY (product_id) REFERENCES products(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            order_number TEXT UNIQUE NOT NULL,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (buyer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,

            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            method TEXT NOT NULL
                CHECK(method IN ('cash', 'mobile_money')),
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            reference TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (order_id) REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            delivery_address TEXT NOT NULL,
            delivery_fee REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (order_id) REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            rate REAL NOT NULL,
            status TEXT DEFAULT 'pending',

            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (seller_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS seller_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            paid_at TIMESTAMP,

            FOREIGN KEY (seller_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS overseas_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            audience_role TEXT,
            order_id INTEGER,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            read_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS message_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(buyer_id, product_id),
            FOREIGN KEY (buyer_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (seller_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            sender_role TEXT NOT NULL CHECK(sender_role IN ('buyer', 'seller', 'admin')),
            sender_id INTEGER,
            recipient_role TEXT NOT NULL CHECK(recipient_role IN ('buyer', 'seller', 'admin')),
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES message_conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
        ON chat_messages(conversation_id, created_at, id);

        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(buyer_id, product_id),
            FOREIGN KEY (buyer_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_watchlist_buyer
        ON watchlist(buyer_id, created_at);

        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS product_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            review TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(product_id, buyer_id),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(buyer_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_role TEXT NOT NULL CHECK(user_role IN ('buyer','seller')),
            request_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            admin_response TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS admin_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(admin_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
        ON login_attempts(email, ip_address, attempted_at);

        CREATE INDEX IF NOT EXISTS idx_support_requests_status
        ON support_requests(status, created_at);

        CREATE INDEX IF NOT EXISTS idx_product_reviews_product
        ON product_reviews(product_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_users_email
        ON users(email);

        CREATE INDEX IF NOT EXISTS idx_password_resets_user
        ON password_resets(user_id);

        CREATE INDEX IF NOT EXISTS idx_product_images_product
        ON product_images(product_id);

        CREATE INDEX IF NOT EXISTS idx_cart_items_buyer
        ON cart_items(buyer_id);

        """)

        # Seller verification and product moderation. Existing sellers/products are
        # grandfathered as verified/approved; newly registered sellers and listings
        # must pass admin review.
        user_cols = {r["name"] for r in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "seller_verified" not in user_cols:
            connection.execute("ALTER TABLE users ADD COLUMN seller_verified INTEGER NOT NULL DEFAULT 0")
        if "seller_verification_status" not in user_cols:
            connection.execute("ALTER TABLE users ADD COLUMN seller_verification_status TEXT NOT NULL DEFAULT 'not_submitted'")
        connection.execute("UPDATE users SET seller_verified=1, seller_verification_status='verified' WHERE role='seller' AND seller_verification_status='not_submitted'")

        product_cols = {r["name"] for r in connection.execute("PRAGMA table_info(products)").fetchall()}
        if "approval_status" not in product_cols:
            connection.execute("ALTER TABLE products ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved'")
        if "approval_note" not in product_cols:
            connection.execute("ALTER TABLE products ADD COLUMN approval_note TEXT")

        order_cols = {r["name"] for r in connection.execute("PRAGMA table_info(orders)").fetchall()}
        if "discount_amount" not in order_cols:
            connection.execute("ALTER TABLE orders ADD COLUMN discount_amount REAL NOT NULL DEFAULT 0")
        if "promotion_code" not in order_cols:
            connection.execute("ALTER TABLE orders ADD COLUMN promotion_code TEXT")
        if "contact_phone" not in order_cols:
            connection.execute("ALTER TABLE orders ADD COLUMN contact_phone TEXT")

        connection.executescript("""
        CREATE TABLE IF NOT EXISTS promotion_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            discount_type TEXT NOT NULL CHECK(discount_type IN ('percent','fixed')),
            discount_value REAL NOT NULL CHECK(discount_value > 0),
            minimum_order REAL NOT NULL DEFAULT 0,
            starts_at TEXT,
            ends_at TEXT,
            usage_limit INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS refund_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            request_id INTEGER,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY(request_id) REFERENCES support_requests(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_products_approval ON products(approval_status, created_at);
        CREATE INDEX IF NOT EXISTS idx_promotions_active ON promotion_codes(active, code);
        """)

        # Global marketplace availability switch (closed by default only if explicitly set).
        connection.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("INSERT OR IGNORE INTO marketplace_settings(setting_key, setting_value) VALUES('is_open','1')")

        # Global marketplace availability switch (closed by default only if explicitly set).
        connection.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        connection.execute("INSERT OR IGNORE INTO marketplace_settings(setting_key, setting_value) VALUES('is_open','1')")

        # Add the optional order reference to support requests without losing existing requests.
        support_columns = [row["name"] for row in connection.execute("PRAGMA table_info(support_requests)").fetchall()]
        if "order_number" not in support_columns:
            connection.execute("ALTER TABLE support_requests ADD COLUMN order_number TEXT")

        # Track chat reads independently for each recipient role.
        chat_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(chat_messages)").fetchall()
        }
        if "read_at" not in chat_columns:
            connection.execute("ALTER TABLE chat_messages ADD COLUMN read_at TIMESTAMP DEFAULT NULL")
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_messages_unread
            ON chat_messages(recipient_role, read_at, conversation_id)
        """)

        # Safely update existing databases without deleting existing records.
        migrate_products_table(connection)
        migrate_order_tracking_schema(connection)
        migrate_commission_amounts(connection)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_conversations_buyer
            ON message_conversations(buyer_id, updated_at)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_conversations_seller
            ON message_conversations(seller_id, updated_at)
        """)

        connection.commit()

        print("Marketplace database initialized successfully!")

    finally:
        connection.close()
