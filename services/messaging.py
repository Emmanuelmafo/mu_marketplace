from database import get_db_connection


def mark_messages_read(conversation_id, role, user_id):
    """Mark messages addressed to this role as read in one conversation."""
    if role not in ("buyer", "seller", "admin"):
        return 0
    connection = get_db_connection()
    try:
        if role == "admin":
            cursor = connection.execute("""
                UPDATE chat_messages
                SET read_at = CURRENT_TIMESTAMP
                WHERE conversation_id = ?
                  AND recipient_role = 'admin'
                  AND read_at IS NULL
            """, (conversation_id,))
        else:
            participant_column = "buyer_id" if role == "buyer" else "seller_id"
            cursor = connection.execute(f"""
                UPDATE chat_messages
                SET read_at = CURRENT_TIMESTAMP
                WHERE conversation_id = ?
                  AND recipient_role = ?
                  AND read_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM message_conversations mc
                      WHERE mc.id = chat_messages.conversation_id
                        AND mc.{participant_column} = ?
                  )
            """, (conversation_id, role, user_id))
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def get_unread_message_count(user_id, role):
    """Count unread messages addressed to the signed-in user's role."""
    if role not in ("buyer", "seller", "admin"):
        return 0
    connection = get_db_connection()
    try:
        if role == "admin":
            row = connection.execute("""
                SELECT COUNT(*) AS total
                FROM chat_messages
                WHERE recipient_role = 'admin' AND read_at IS NULL
            """).fetchone()
        else:
            participant_column = "buyer_id" if role == "buyer" else "seller_id"
            row = connection.execute(f"""
                SELECT COUNT(*) AS total
                FROM chat_messages cm
                JOIN message_conversations mc ON mc.id = cm.conversation_id
                WHERE mc.{participant_column} = ?
                  AND cm.recipient_role = ?
                  AND cm.read_at IS NULL
            """, (user_id, role)).fetchone()
        return row["total"] if row else 0
    finally:
        connection.close()


def get_or_create_conversation(buyer_id, product_id):
    connection = get_db_connection()
    try:
        product = connection.execute("""
            SELECT id, seller_id, name
            FROM products
            WHERE id = ? AND LOWER(COALESCE(status, 'active')) = 'active'
        """, (product_id,)).fetchone()
        if not product:
            return None
        connection.execute("""
            INSERT OR IGNORE INTO message_conversations
                (buyer_id, seller_id, product_id)
            VALUES (?, ?, ?)
        """, (buyer_id, product["seller_id"], product_id))
        connection.commit()
        return connection.execute("""
            SELECT mc.*, p.name AS product_name
            FROM message_conversations mc
            JOIN products p ON p.id = mc.product_id
            WHERE mc.buyer_id = ? AND mc.product_id = ?
        """, (buyer_id, product_id)).fetchone()
    finally:
        connection.close()


def get_conversation(conversation_id):
    connection = get_db_connection()
    try:
        return connection.execute("""
            SELECT mc.*, p.name AS product_name,
                   b.full_name AS buyer_name,
                   s.full_name AS seller_name
            FROM message_conversations mc
            JOIN products p ON p.id = mc.product_id
            JOIN users b ON b.id = mc.buyer_id
            JOIN users s ON s.id = mc.seller_id
            WHERE mc.id = ?
        """, (conversation_id,)).fetchone()
    finally:
        connection.close()


def get_messages(conversation_id, role):
    connection = get_db_connection()
    try:
        if role == "admin":
            rows = connection.execute("""
                SELECT * FROM chat_messages
                WHERE conversation_id = ?
                ORDER BY created_at, id
            """, (conversation_id,)).fetchall()
        else:
            rows = connection.execute("""
                SELECT * FROM chat_messages
                WHERE conversation_id = ?
                  AND (sender_role = ? OR recipient_role = ?)
                ORDER BY created_at, id
            """, (conversation_id, role, role)).fetchall()
        return rows
    finally:
        connection.close()


def add_chat_message(conversation_id, sender_role, sender_id, recipient_role, message):
    message = (message or "").strip()
    if not message or len(message) > 3000:
        return False
    if sender_role not in ("buyer", "seller", "admin"):
        return False
    if recipient_role not in ("buyer", "seller", "admin"):
        return False
    # Non-admin users can only send messages to the admin.
    if sender_role in ("buyer", "seller") and recipient_role != "admin":
        return False
    connection = get_db_connection()
    try:
        conversation = connection.execute(
            "SELECT id, buyer_id, seller_id FROM message_conversations WHERE id = ?",
            (conversation_id,)
        ).fetchone()
        if not conversation:
            return False
        connection.execute("""
            INSERT INTO chat_messages
                (conversation_id, sender_role, sender_id, recipient_role, message)
            VALUES (?, ?, ?, ?, ?)
        """, (conversation_id, sender_role, sender_id, recipient_role, message))
        connection.execute("""
            UPDATE message_conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (conversation_id,))

        # Notify only the intended recipient. Buyers and sellers still communicate
        # through the admin; they never receive each other's identity or messages.
        if recipient_role == "admin":
            notification_user_id = None
            audience_role = "admin"
            recipient_label = "Admin"
        elif recipient_role == "buyer":
            notification_user_id = conversation["buyer_id"]
            audience_role = None
            recipient_label = "Buyer"
        else:
            notification_user_id = conversation["seller_id"]
            audience_role = None
            recipient_label = "Seller"

        connection.execute("""
            INSERT INTO notifications
                (user_id, audience_role, title, message)
            VALUES (?, ?, ?, ?)
        """, (
            notification_user_id,
            audience_role,
            "New marketplace message",
            f"You have a new message in conversation #{conversation_id}."
        ))
        connection.commit()
        return True
    finally:
        connection.close()


def get_user_conversations(user_id, role):
    connection = get_db_connection()
    try:
        if role == "buyer":
            where, params = "mc.buyer_id = ?", (user_id,)
            other = "s.full_name AS other_name"
        elif role == "seller":
            # Do not reveal a buyer enquiry or even its conversation entry to the
            # seller until an admin explicitly forwards a message to that seller.
            where = """mc.seller_id = ? AND EXISTS (
                SELECT 1 FROM chat_messages visible
                WHERE visible.conversation_id = mc.id
                  AND (visible.recipient_role = 'seller'
                       OR visible.sender_role = 'seller')
            )"""
            params = (user_id,)
            other = "b.full_name AS other_name"
        else:
            where, params = "1 = 1", ()
            other = "b.full_name AS other_name, s.full_name AS seller_name"

        # Non-admin inbox previews must obey the same visibility rules as chat
        # messages. This prevents buyer-to-admin or seller-to-admin text leaking
        # through the conversation preview.
        if role == "admin":
            visible_message_filter = "cm.conversation_id = mc.id"
        else:
            visible_message_filter = (
                "cm.conversation_id = mc.id AND "
                "(cm.sender_role = ? OR cm.recipient_role = ?)"
            )

        return connection.execute(f"""
            SELECT mc.*, p.name AS product_name, {other},
                   (SELECT message FROM chat_messages cm
                    WHERE {visible_message_filter}
                    ORDER BY cm.created_at DESC, cm.id DESC LIMIT 1) AS last_message,
                   (SELECT sender_role FROM chat_messages cm
                    WHERE {visible_message_filter}
                    ORDER BY cm.created_at DESC, cm.id DESC LIMIT 1) AS last_sender
            FROM message_conversations mc
            JOIN products p ON p.id = mc.product_id
            JOIN users b ON b.id = mc.buyer_id
            JOIN users s ON s.id = mc.seller_id
            WHERE {where}
            ORDER BY mc.updated_at DESC, mc.id DESC
        """, params if role == "admin" else (
            role, role, role, role, params[0]
        )).fetchall()
    finally:
        connection.close()
