from database import get_db_connection


def get_watchlist(buyer_id):
    connection = get_db_connection()
    try:
        return connection.execute("""
            SELECT p.id, p.name, p.description, p.price, p.stock, p.image,
                   c.name AS category_name, w.created_at
            FROM watchlist w
            JOIN products p ON p.id = w.product_id
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE w.buyer_id = ?
              AND LOWER(COALESCE(p.status, 'active')) = 'active'
            ORDER BY w.created_at DESC
        """, (buyer_id,)).fetchall()
    finally:
        connection.close()


def add_to_watchlist(buyer_id, product_id):
    connection = get_db_connection()
    try:
        product = connection.execute("""
            SELECT id FROM products
            WHERE id = ? AND LOWER(COALESCE(status, 'active')) = 'active'
        """, (product_id,)).fetchone()
        if not product:
            return False, "This product is no longer available."
        connection.execute("""
            INSERT OR IGNORE INTO watchlist (buyer_id, product_id)
            VALUES (?, ?)
        """, (buyer_id, product_id))
        connection.commit()
        return True, "Product added to your Watchlist."
    finally:
        connection.close()


def remove_from_watchlist(buyer_id, product_id):
    connection = get_db_connection()
    try:
        connection.execute("""
            DELETE FROM watchlist WHERE buyer_id = ? AND product_id = ?
        """, (buyer_id, product_id))
        connection.commit()
        return True, "Product removed from your Watchlist."
    finally:
        connection.close()


def get_watchlist_ids(buyer_id):
    connection = get_db_connection()
    try:
        rows = connection.execute(
            "SELECT product_id FROM watchlist WHERE buyer_id = ?",
            (buyer_id,)
        ).fetchall()
        return {row["product_id"] for row in rows}
    finally:
        connection.close()
