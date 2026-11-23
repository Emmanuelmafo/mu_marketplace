import math
import uuid

from database import get_db_connection


def _commission_for_price(price):
    price = float(price)
    if price < 50:
        return 5.0
    if price < 100:
        return 10.0
    return 15.0 + 5.0 * math.floor((price - 100.0) / 100.0)


def get_public_products(search="", category_id=None, min_price=None, max_price=None, sort="newest"):
    connection = get_db_connection()
    try:
        sql = """
            SELECT products.id, products.name, products.description,
                   products.price, products.stock, products.image,
                   categories.name AS category_name,
                   users.full_name AS seller_name
            FROM products
            LEFT JOIN categories ON categories.id = products.category_id
            JOIN users ON users.id = products.seller_id
            WHERE LOWER(COALESCE(products.status, 'active')) = 'active'
              AND LOWER(COALESCE(products.approval_status, 'approved')) = 'approved'
              AND COALESCE(users.seller_verified, 1) = 1
              AND products.stock > 0
        """
        params = []
        if search.strip():
            sql += """ AND (products.name LIKE ? OR products.description LIKE ?
                           OR categories.name LIKE ?)"""
            term = f"%{search.strip()}%"
            params.extend([term, term, term])
        if category_id:
            sql += " AND products.category_id = ?"
            params.append(category_id)
        if min_price is not None:
            sql += " AND products.price >= ?"
            params.append(min_price)
        if max_price is not None:
            sql += " AND products.price <= ?"
            params.append(max_price)
        order_by = {
            "newest": "products.created_at DESC, products.id DESC",
            "price_low": "products.price ASC, products.id DESC",
            "price_high": "products.price DESC, products.id DESC",
            "name": "products.name COLLATE NOCASE ASC"
        }.get(sort, "products.created_at DESC, products.id DESC")
        sql += " ORDER BY " + order_by
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def get_public_product(product_id):
    connection = get_db_connection()
    try:
        product = connection.execute("""
            SELECT products.id, products.name, products.description,
                   products.price, products.stock, products.image,
                   products.seller_id,
                   categories.name AS category_name
            FROM products
            LEFT JOIN categories ON categories.id = products.category_id
            WHERE products.id = ?
              AND LOWER(COALESCE(products.status, 'active')) = 'active'
              AND LOWER(COALESCE(products.approval_status, 'approved')) = 'approved'
              AND EXISTS (SELECT 1 FROM users su WHERE su.id=products.seller_id AND COALESCE(su.seller_verified,1)=1)
        """, (product_id,)).fetchone()
        if not product:
            return None
        images = connection.execute("""
            SELECT image_path FROM product_images
            WHERE product_id = ? ORDER BY id
        """, (product_id,)).fetchall()
        data = dict(product)
        data["images"] = [row["image_path"] for row in images]
        if data["image"] and data["image"] not in data["images"]:
            data["images"].insert(0, data["image"])
        return data
    finally:
        connection.close()


def _notify(connection, user_id, audience_role, order_id, title, message):
    connection.execute("""
        INSERT INTO notifications
            (user_id, audience_role, order_id, title, message)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, audience_role, order_id, title, message))


def create_order(buyer_id, product_id, quantity, payment_method, delivery_address, coupon_code="", contact_phone=""):
    if payment_method not in ("cash", "mobile_money"):
        return False, "Choose a valid payment method."
    if not delivery_address.strip():
        return False, "Enter your delivery location."
    contact_phone = (contact_phone or "").strip()
    if not contact_phone:
        return False, "Enter a phone number where the admin can reach you about this order."
    if len(contact_phone) < 7 or len(contact_phone) > 25:
        return False, "Enter a valid contact phone number (7 to 25 characters)."
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return False, "Enter a valid quantity."
    if quantity < 1:
        return False, "Quantity must be at least 1."

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        product = connection.execute("""
            SELECT p.id, p.name, p.price, p.stock, p.seller_id,
                   p.pickup_location, u.full_name AS seller_name
            FROM products p JOIN users u ON u.id = p.seller_id
            WHERE p.id = ? AND LOWER(COALESCE(p.status, 'active')) = 'active' AND LOWER(COALESCE(p.approval_status,'approved'))='approved' AND COALESCE(u.seller_verified,1)=1
        """, (product_id,)).fetchone()

        if not product:
            connection.rollback()
            return False, "This product is no longer available."
        if product["seller_id"] == buyer_id:
            connection.rollback()
            return False, "You cannot buy your own product."
        if quantity > product["stock"]:
            connection.rollback()
            return False, f"Only {product['stock']} item(s) are currently available."

        subtotal = round(float(product["price"]) * quantity, 2)
        discount = 0.0
        coupon_code = (coupon_code or "").strip().upper()
        promotion = None
        if coupon_code:
            promotion = connection.execute("""SELECT * FROM promotion_codes
                WHERE LOWER(code)=LOWER(?) AND active=1
                AND (NULLIF(starts_at, '') IS NULL OR NULLIF(starts_at, '')::timestamp <= CURRENT_TIMESTAMP)
                AND (NULLIF(ends_at, '') IS NULL OR NULLIF(ends_at, '')::timestamp >= CURRENT_TIMESTAMP)
                AND (usage_limit IS NULL OR used_count<usage_limit)""", (coupon_code,)).fetchone()
            if not promotion:
                connection.rollback()
                return False, "That promotion code is invalid, expired or has reached its usage limit."
            if subtotal < float(promotion["minimum_order"] or 0):
                connection.rollback()
                return False, f"This code requires a minimum order of K{float(promotion['minimum_order']):.2f}."
            if promotion["discount_type"] == "percent":
                discount = subtotal * min(float(promotion["discount_value"]), 100.0) / 100.0
            else:
                discount = float(promotion["discount_value"])
            discount = min(round(discount, 2), subtotal)
        total = round(subtotal - discount, 2)
        order_number = "MU-" + uuid.uuid4().hex[:10].upper()
        cursor = connection.execute("""
            INSERT INTO orders (buyer_id, order_number, total_amount, status, discount_amount, promotion_code, contact_phone)
            VALUES (?, ?, ?, 'processing', ?, ?, ?)
        """, (buyer_id, order_number, total, discount, coupon_code or None, contact_phone))
        order_id = cursor.lastrowid

        connection.execute("""
            INSERT INTO order_items
                (order_id, product_id, quantity, unit_price, seller_id,
                 pickup_location_snapshot, product_name_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (order_id, product_id, quantity, product["price"],
              product["seller_id"], product["pickup_location"] or "",
              product["name"]))

        connection.execute("""
            INSERT INTO payments (order_id, method, amount, status)
            VALUES (?, ?, ?, 'pending')
        """, (order_id, payment_method, total))

        connection.execute("""
            INSERT INTO deliveries
                (order_id, delivery_address, delivery_fee, status)
            VALUES (?, ?, 0, 'pending')
        """, (order_id, delivery_address.strip()))

        # For products priced at K100 or below, the listing's total
        # quantity determines how many individual units can incur commission.
        # Only the first permitted units sold are charged; later units are free
        # of commission. Products above K100 are charged on every unit sold.
        product_price = float(product["price"])
        unit_commission = min(
            _commission_for_price(product_price),
            product_price
        )

        if product_price <= 100:
            previous_sales = connection.execute("""
                SELECT COALESCE(SUM(quantity), 0) AS sold_quantity
                FROM order_items
                WHERE product_id = ? AND order_id <> ?
            """, (product_id, order_id)).fetchone()
            sold_before = int(previous_sales["sold_quantity"] or 0)

            # Stock is reduced when each order is placed, so current stock
            # plus all previous ordered units gives the original listed amount.
            listed_quantity = int(product["stock"] or 0) + sold_before

            if listed_quantity <= 5:
                allowed_commission_units = 1
            elif listed_quantity <= 10:
                allowed_commission_units = 2
            else:
                allowed_commission_units = 2 + math.ceil(
                    (listed_quantity - 10) / 10
                )

            remaining_commission_units = max(
                0, allowed_commission_units - sold_before
            )
            commission_units_this_order = min(
                quantity, remaining_commission_units
            )
        else:
            commission_units_this_order = quantity

        # Store each commission as its own record, one record per chargeable
        # unit sold. This keeps the number of commission charges auditable.
        for _ in range(commission_units_this_order):
            connection.execute("""
                INSERT INTO commissions (order_id, seller_id, amount, rate, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (
                order_id,
                product["seller_id"],
                round(unit_commission, 2),
                round(unit_commission, 2)
            ))

        connection.execute("""
            UPDATE products SET stock = stock - ? WHERE id = ?
        """, (quantity, product_id))
        if promotion:
            connection.execute("UPDATE promotion_codes SET used_count=used_count+1 WHERE id=?", (promotion["id"],))

        buyer = connection.execute("""
            SELECT full_name FROM users WHERE id = ?
        """, (buyer_id,)).fetchone()

        # Buyer and seller messages intentionally exclude the other party's
        # identity, phone number and location.
        _notify(
            connection, buyer_id, None, order_id,
            "Order placed successfully",
            f"Your order has been received.\n\n"
            f"Order number: {order_number}\n"
            f"Item: {product['name']}\n"
            f"Quantity: {quantity}\n"
            f"Discount: K{discount:.2f}\n"
            f"Order status: Processing\n\n"
            "The marketplace admin will coordinate the next steps with you."
        )
        _notify(
            connection, product["seller_id"], None, order_id,
            "You have received a new order",
            f"A buyer has placed an order for your item.\n\n"
            f"Item: {product['name']}\n"
            f"Quantity: {quantity}\n"
            "Order status: Processing\n\n"
            "Please prepare the item. The marketplace admin will contact you to coordinate collection."
        )
        admin_message = (
            "A new marketplace order has been placed.\n\n"
            "ORDER DETAILS\n"
            f"Order number: {order_number}\n"
            f"Product: {product['name']}\n"
            f"Quantity: {quantity}\n"
            f"Payment method: {payment_method.replace('_', ' ').title()}\n\n"
            "BUYER DETAILS\n"
            f"Full name: {buyer['full_name']}\n"
            f"Contact phone: {contact_phone}\n"
            f"Delivery location: {delivery_address.strip()}\n\n"
            "SELLER DETAILS\n"
            f"Seller: {product['seller_name']}\n"
            f"Pickup location: {product['pickup_location'] or 'Not provided'}\n\n"
            "ORDER TRACKING\n"
            "Order status: Processing\n"
            "Payment status: Pending\n"
            "Delivery status: Pending"
        )
        _notify(connection, None, "admin", order_id,
                "New marketplace order", admin_message)

        connection.commit()
        return True, f"Order placed successfully. Your order number is {order_number}."
    except Exception as error:
        connection.rollback()
        return False, f"Unable to place order: {error}"
    finally:
        connection.close()


def get_buyer_tracking(buyer_id):
    connection = get_db_connection()
    try:
        orders = connection.execute("""
            SELECT o.id AS order_id, o.order_number, o.total_amount,
                   o.status AS order_status, o.created_at,
                   p.method AS payment_method, p.status AS payment_status,
                   d.status AS delivery_status
            FROM orders o
            LEFT JOIN payments p ON p.id = (
                SELECT MAX(id) FROM payments WHERE order_id = o.id
            )
            LEFT JOIN deliveries d ON d.id = (
                SELECT MAX(id) FROM deliveries WHERE order_id = o.id
            )
            WHERE o.buyer_id = ?
            ORDER BY o.created_at DESC, o.id DESC
        """, (buyer_id,)).fetchall()
        result = []
        for order in orders:
            data = dict(order)
            data["items"] = [dict(row) for row in connection.execute("""
                SELECT COALESCE(oi.product_name_snapshot, pr.name) AS product_name,
                       oi.product_id, oi.quantity, oi.unit_price
                FROM order_items oi LEFT JOIN products pr ON pr.id = oi.product_id
                WHERE oi.order_id = ?
            """, (order["order_id"],)).fetchall()]
            result.append(data)
        return result
    finally:
        connection.close()


def get_seller_tracking(seller_id):
    connection = get_db_connection()
    try:
        orders = connection.execute("""
            SELECT DISTINCT o.id AS order_id, o.order_number, o.status AS order_status,
                   o.created_at,
                   p.status AS payment_status,
                   d.status AS delivery_status
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            LEFT JOIN payments p ON p.id = (
                SELECT MAX(id) FROM payments WHERE order_id = o.id
            )
            LEFT JOIN deliveries d ON d.id = (
                SELECT MAX(id) FROM deliveries WHERE order_id = o.id
            )
            WHERE oi.seller_id = ?
            ORDER BY o.created_at DESC, o.id DESC
        """, (seller_id,)).fetchall()
        result = []
        for order in orders:
            data = dict(order)
            data["items"] = [dict(row) for row in connection.execute("""
                SELECT COALESCE(oi.product_name_snapshot, pr.name) AS product_name,
                       oi.quantity, oi.unit_price
                FROM order_items oi LEFT JOIN products pr ON pr.id = oi.product_id
                WHERE oi.order_id = ? AND oi.seller_id = ?
            """, (order["order_id"], seller_id)).fetchall()]
            result.append(data)
        return result
    finally:
        connection.close()


def get_notifications(user_id=None, role=None, limit=30):
    connection = get_db_connection()
    try:
        if role == "admin":
            rows = connection.execute("""
                SELECT * FROM notifications
                WHERE audience_role = 'admin'
                ORDER BY created_at DESC, id DESC LIMIT ?
            """, (limit,)).fetchall()
        else:
            rows = connection.execute("""
                SELECT * FROM notifications
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
            """, (user_id, limit)).fetchall()
        return rows
    finally:
        connection.close()


def update_order_progress(order_id, order_status, payment_received, delivery_status, payment_method=None):
    allowed_order = {"processing": "Processing", "confirmed": "Order confirmed",
                     "completed": "Order completed"}
    allowed_delivery = {"pending": "Not collected", "collected": "Product collected from seller",
                        "on_the_way": "On its way to buyer", "delivered": "Delivered to buyer"}
    if order_status not in allowed_order or delivery_status not in allowed_delivery:
        return False, "Invalid order progress selection."
    if payment_method is not None and payment_method not in ("cash", "mobile_money"):
        return False, "Choose Cash or Mobile Money as the payment method."

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute("""
            SELECT id, buyer_id, order_number FROM orders WHERE id = ?
        """, (order_id,)).fetchone()
        if not order:
            connection.rollback()
            return False, "Order not found."

        old = connection.execute("""
            SELECT o.status, p.status AS payment_status, d.status AS delivery_status
            FROM orders o
            LEFT JOIN payments p ON p.id = (SELECT MAX(id) FROM payments WHERE order_id=o.id)
            LEFT JOIN deliveries d ON d.id = (SELECT MAX(id) FROM deliveries WHERE order_id=o.id)
            WHERE o.id = ?
        """, (order_id,)).fetchone()

        connection.execute("UPDATE orders SET status = ? WHERE id = ?",
                           (order_status, order_id))
        payment_status = "paid" if payment_received else "pending"
        connection.execute("""
            UPDATE payments SET status = ?,
                method = COALESCE(?, method)
            WHERE id = (SELECT MAX(id) FROM payments WHERE order_id = ?)
        """, (payment_status, payment_method, order_id))
        connection.execute("""
            UPDATE deliveries SET status = ?
            WHERE id = (SELECT MAX(id) FROM deliveries WHERE order_id = ?)
        """, (delivery_status, order_id))

        sellers = connection.execute("""
            SELECT DISTINCT oi.seller_id, GROUP_CONCAT(COALESCE(oi.product_name_snapshot, p.name), ', ') AS products
            FROM order_items oi LEFT JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ? AND oi.seller_id IS NOT NULL
            GROUP BY oi.seller_id
        """, (order_id,)).fetchall()

        # Only send updates when the corresponding state actually changes.
        if (old["status"] != order_status
                or old["payment_status"] != payment_status
                or old["delivery_status"] != delivery_status):
            _notify(
                connection, None, "admin", order_id,
                "Order tracking updated",
                f"Order {order['order_number']} tracking was updated by the admin."
            )
        if old["status"] != order_status:
            buyer_msg = f"Your order {order['order_number']} is now {allowed_order[order_status].lower()}."
            _notify(connection, order["buyer_id"], None, order_id,
                    "Order status updated", buyer_msg)
            for seller in sellers:
                _notify(connection, seller["seller_id"], None, order_id,
                        "Order status updated",
                        f"Your item(s) in order {order['order_number']} are now marked {allowed_order[order_status].lower()}.")
        if old["payment_status"] != payment_status and payment_received:
            _notify(connection, order["buyer_id"], None, order_id,
                    "Payment received", f"Payment for order {order['order_number']} has been marked as received.")
            for seller in sellers:
                _notify(connection, seller["seller_id"], None, order_id,
                        "Payment received", f"Payment for the item(s) in order {order['order_number']} has been received.")
        if old["delivery_status"] != delivery_status:
            buyer_text = {
                "collected": "Your order has been collected and is being prepared for delivery.",
                "on_the_way": "Your order is on its way to you.",
                "delivered": "Your order has been delivered."
            }.get(delivery_status, "Delivery progress has been updated.")
            seller_text = {
                "collected": "Your item has been collected from you.",
                "on_the_way": "Your item is on its way to the buyer.",
                "delivered": "The item has been delivered to the buyer."
            }.get(delivery_status, "Collection and delivery progress has been updated.")
            _notify(connection, order["buyer_id"], None, order_id,
                    "Delivery update", f"Order {order['order_number']}: {buyer_text}")
            for seller in sellers:
                _notify(connection, seller["seller_id"], None, order_id,
                        "Collection and delivery update",
                        f"Order {order['order_number']}: {seller_text}")

        connection.commit()
        return True, "Order tracking has been updated and notifications saved."
    except Exception as error:
        connection.rollback()
        return False, f"Unable to update order: {error}"
    finally:
        connection.close()



def get_unread_notification_count(user_id=None, role=None):
    connection = get_db_connection()
    try:
        if role == "admin":
            row = connection.execute("""
                SELECT COUNT(*) AS total FROM notifications
                WHERE audience_role = 'admin' AND read_at IS NULL
            """).fetchone()
        else:
            row = connection.execute("""
                SELECT COUNT(*) AS total FROM notifications
                WHERE user_id = ? AND read_at IS NULL
            """, (user_id,)).fetchone()
        return int(row["total"] if row else 0)
    finally:
        connection.close()


def mark_notifications_read(user_id=None, role=None):
    connection = get_db_connection()
    try:
        if role == "admin":
            connection.execute("""
                UPDATE notifications SET read_at = CURRENT_TIMESTAMP
                WHERE audience_role = 'admin' AND read_at IS NULL
            """)
        else:
            connection.execute("""
                UPDATE notifications SET read_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND read_at IS NULL
            """, (user_id,))
        connection.commit()
    finally:
        connection.close()


def get_cart_items(buyer_id):
    connection = get_db_connection()
    try:
        return connection.execute("""
            SELECT c.id AS cart_item_id, c.quantity, p.id AS product_id,
                   p.name, p.price, p.stock, p.image,
                   (c.quantity * p.price) AS line_total
            FROM cart_items c
            JOIN products p ON p.id = c.product_id
            WHERE c.buyer_id = ? AND p.stock > 0
              AND LOWER(COALESCE(p.status, 'active')) = 'active'
            ORDER BY c.created_at DESC, c.id DESC
        """, (buyer_id,)).fetchall()
    finally:
        connection.close()


def get_cart_count(buyer_id):
    connection = get_db_connection()
    try:
        row = connection.execute("""
            SELECT COALESCE(SUM(quantity), 0) AS total
            FROM cart_items WHERE buyer_id = ?
        """, (buyer_id,)).fetchone()
        return int(row["total"] if row else 0)
    finally:
        connection.close()


def add_to_cart(buyer_id, product_id, quantity=1):
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return False, "Enter a valid quantity."
    if quantity < 1:
        return False, "Quantity must be at least 1."
    connection = get_db_connection()
    try:
        product = connection.execute("""
            SELECT id, stock, seller_id FROM products
            WHERE id = ? AND LOWER(COALESCE(status, 'active')) = 'active'
        """, (product_id,)).fetchone()
        if not product or product["stock"] < 1:
            return False, "This product is currently unavailable."
        if product["seller_id"] == buyer_id:
            return False, "You cannot add your own product to your cart."
        existing = connection.execute("""
            SELECT quantity FROM cart_items WHERE buyer_id = ? AND product_id = ?
        """, (buyer_id, product_id)).fetchone()
        new_quantity = quantity + (int(existing["quantity"]) if existing else 0)
        if new_quantity > product["stock"]:
            return False, f"Only {product['stock']} item(s) are available."
        connection.execute("""
            INSERT INTO cart_items (buyer_id, product_id, quantity)
            VALUES (?, ?, ?)
            ON CONFLICT(buyer_id, product_id) DO UPDATE SET quantity = excluded.quantity
        """, (buyer_id, product_id, new_quantity))
        connection.commit()
        return True, "Product added to your cart."
    except Exception as error:
        connection.rollback()
        return False, f"Unable to add product to cart: {error}"
    finally:
        connection.close()


def update_cart_quantity(buyer_id, cart_item_id, quantity):
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return False, "Enter a valid quantity."
    if quantity < 1:
        return False, "Quantity must be at least 1."
    connection = get_db_connection()
    try:
        item = connection.execute("""
            SELECT c.id, p.stock FROM cart_items c
            JOIN products p ON p.id = c.product_id
            WHERE c.id = ? AND c.buyer_id = ?
        """, (cart_item_id, buyer_id)).fetchone()
        if not item:
            return False, "Cart item not found."
        if quantity > item["stock"]:
            return False, f"Only {item['stock']} item(s) are available."
        connection.execute("UPDATE cart_items SET quantity = ? WHERE id = ? AND buyer_id = ?",
                           (quantity, cart_item_id, buyer_id))
        connection.commit()
        return True, "Cart updated."
    except Exception as error:
        connection.rollback()
        return False, f"Unable to update cart: {error}"
    finally:
        connection.close()


def remove_from_cart(buyer_id, cart_item_id):
    connection = get_db_connection()
    try:
        connection.execute("DELETE FROM cart_items WHERE id = ? AND buyer_id = ?",
                           (cart_item_id, buyer_id))
        connection.commit()
        return True, "Product removed from your cart."
    finally:
        connection.close()
