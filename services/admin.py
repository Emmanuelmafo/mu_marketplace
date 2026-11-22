
import math

from database import get_db_connection


# Only successfully fulfilled and paid orders count towards seller earnings.
ELIGIBLE_ORDER_STATUSES = ("completed", "delivered")
ELIGIBLE_PAYMENT_STATUSES = ("paid", "completed")


def get_admin_dashboard_summary():

    connection = get_db_connection()

    try:
        users = connection.execute("""
            SELECT
                COUNT(*) AS total_users,
                SUM(CASE WHEN role = 'buyer' THEN 1 ELSE 0 END)
                    AS total_buyers,
                SUM(CASE WHEN role = 'seller' THEN 1 ELSE 0 END)
                    AS total_sellers,
                SUM(CASE WHEN role = 'admin' THEN 1 ELSE 0 END)
                    AS total_admins
            FROM users
            WHERE COALESCE(is_deleted, 0) = 0
        """).fetchone()

        orders = connection.execute("""
            SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(total_amount), 0) AS total_order_value,
                SUM(CASE WHEN LOWER(status) IN ('pending', 'processing', 'confirmed')
                    THEN 1 ELSE 0 END) AS pending_orders,
                SUM(CASE WHEN LOWER(status) IN ('completed', 'delivered')
                    THEN 1 ELSE 0 END) AS completed_orders
            FROM orders
        """).fetchone()

        payments = connection.execute("""
            SELECT
                COALESCE(SUM(payments.amount), 0) AS completed_payments
            FROM payments
            JOIN orders ON payments.order_id = orders.id
            WHERE LOWER(payments.status) IN ('paid', 'completed')
              AND LOWER(orders.status) IN ('completed', 'delivered')
        """).fetchone()

        commissions = connection.execute("""
            SELECT
                COALESCE(SUM(commissions.amount), 0) AS total_commissions,

                COALESCE(SUM(
                    CASE
                        WHEN LOWER(commissions.status) = 'pending'
                        THEN commissions.amount
                        ELSE 0
                    END
                ), 0) AS pending_commissions,

                COALESCE(SUM(
                    CASE
                        WHEN LOWER(commissions.status) IN ('paid', 'completed')
                        THEN commissions.amount
                        ELSE 0
                    END
                ), 0) AS paid_commissions

            FROM commissions
            JOIN orders ON commissions.order_id = orders.id

            WHERE LOWER(orders.status) IN ('completed', 'delivered')
              AND EXISTS (
                  SELECT 1
                  FROM payments
                  WHERE payments.order_id = orders.id
                    AND LOWER(payments.status) IN ('paid', 'completed')
              )
        """).fetchone()

        payouts = connection.execute("""
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN LOWER(status) IN ('paid', 'completed')
                        THEN amount
                        ELSE 0
                    END
                ), 0) AS completed_payouts,

                COALESCE(SUM(
                    CASE
                        WHEN LOWER(status) = 'pending'
                        THEN amount
                        ELSE 0
                    END
                ), 0) AS pending_payouts

            FROM seller_payouts
        """).fetchone()

        return {
            "total_users": users["total_users"] or 0,
            "total_buyers": users["total_buyers"] or 0,
            "total_sellers": users["total_sellers"] or 0,
            "total_admins": users["total_admins"] or 0,
            "total_orders": orders["total_orders"] or 0,
            "total_order_value": orders["total_order_value"] or 0,
            "pending_orders": orders["pending_orders"] or 0,
            "completed_orders": orders["completed_orders"] or 0,
            "completed_payments": payments["completed_payments"] or 0,
            "total_commissions": commissions["total_commissions"] or 0,
            "pending_commissions": commissions["pending_commissions"] or 0,
            "paid_commissions": commissions["paid_commissions"] or 0,
            "completed_payouts": payouts["completed_payouts"] or 0,
            "pending_payouts": payouts["pending_payouts"] or 0
        }

    finally:
        connection.close()


def get_admin_seller_finances():

    connection = get_db_connection()

    try:
        sellers = connection.execute("""
            SELECT
                users.id AS seller_id,
                users.full_name,
                users.email,
                users.phone,

                COALESCE((
                    SELECT SUM(
                        order_items.quantity * order_items.unit_price
                    )
                    FROM order_items
                    JOIN products
                        ON order_items.product_id = products.id
                    JOIN orders
                        ON order_items.order_id = orders.id
                    WHERE products.seller_id = users.id
                      AND LOWER(orders.status) IN ('completed', 'delivered')
                      AND EXISTS (
                          SELECT 1
                          FROM payments
                          WHERE payments.order_id = orders.id
                            AND LOWER(payments.status) IN ('paid', 'completed')
                      )
                ), 0) AS total_sales,

                COALESCE((
                    SELECT SUM(commissions.amount)
                    FROM commissions
                    JOIN orders
                        ON commissions.order_id = orders.id
                    WHERE commissions.seller_id = users.id
                      AND LOWER(orders.status) IN ('completed', 'delivered')
                      AND EXISTS (
                          SELECT 1
                          FROM payments
                          WHERE payments.order_id = orders.id
                            AND LOWER(payments.status) IN ('paid', 'completed')
                      )
                ), 0) AS total_commissions,

                COALESCE((
                    SELECT SUM(amount)
                    FROM seller_payouts
                    WHERE seller_id = users.id
                      AND LOWER(status) IN ('paid', 'completed')
                ), 0) AS total_paid_payouts,

                COALESCE((
                    SELECT SUM(amount)
                    FROM seller_payouts
                    WHERE seller_id = users.id
                      AND LOWER(status) = 'pending'
                ), 0) AS pending_payouts

            FROM users
            WHERE users.role = 'seller'
            ORDER BY users.full_name
        """).fetchall()

        results = []

        for seller in sellers:

            total_sales = seller["total_sales"] or 0
            total_commissions = seller["total_commissions"] or 0
            total_paid = seller["total_paid_payouts"] or 0
            pending_payouts = seller["pending_payouts"] or 0

            net_earnings = total_sales - total_commissions

            remaining_balance = max(
                net_earnings - total_paid - pending_payouts,
                0
            )

            results.append({
                "seller_id": seller["seller_id"],
                "full_name": seller["full_name"],
                "email": seller["email"],
                "phone": seller["phone"],
                "total_sales": total_sales,
                "total_commissions": total_commissions,
                "net_earnings": net_earnings,
                "total_paid_payouts": total_paid,
                "pending_payouts": pending_payouts,
                "remaining_balance": remaining_balance
            })

        return results

    finally:
        connection.close()


def get_admin_orders():

    connection = get_db_connection()

    try:
        orders = connection.execute("""
            SELECT
                orders.id AS order_id,
                orders.order_number,
                orders.total_amount,
                orders.status AS order_status,
                orders.created_at,

                users.full_name AS buyer_name,
                users.email AS buyer_email,
                COALESCE(NULLIF(orders.contact_phone, ''), users.phone) AS buyer_phone,

                (
                    SELECT deliveries.delivery_address
                    FROM deliveries
                    WHERE deliveries.order_id = orders.id
                    ORDER BY deliveries.id DESC
                    LIMIT 1
                ) AS delivery_address,

                (
                    SELECT deliveries.delivery_fee
                    FROM deliveries
                    WHERE deliveries.order_id = orders.id
                    ORDER BY deliveries.id DESC
                    LIMIT 1
                ) AS delivery_fee,

                (
                    SELECT deliveries.status
                    FROM deliveries
                    WHERE deliveries.order_id = orders.id
                    ORDER BY deliveries.id DESC
                    LIMIT 1
                ) AS delivery_status,

                (
                    SELECT payments.method
                    FROM payments
                    WHERE payments.order_id = orders.id
                    ORDER BY payments.id DESC
                    LIMIT 1
                ) AS payment_method,

                (
                    SELECT payments.status
                    FROM payments
                    WHERE payments.order_id = orders.id
                    ORDER BY payments.id DESC
                    LIMIT 1
                ) AS payment_status

            FROM orders

            JOIN users
                ON orders.buyer_id = users.id

            ORDER BY orders.created_at DESC, orders.id DESC
        """).fetchall()

        results = []

        for order in orders:

            order_items = connection.execute("""
                SELECT
                    COALESCE(order_items.product_name_snapshot, products.name) AS product_name,
                    order_items.quantity,
                    order_items.unit_price,
                    COALESCE(order_items.pickup_location_snapshot, products.pickup_location) AS pickup_location,
                    sellers.full_name AS seller_name,
                    sellers.email AS seller_email,
                    sellers.phone AS seller_phone

                FROM order_items

                LEFT JOIN products ON order_items.product_id = products.id
                LEFT JOIN users AS sellers
                    ON sellers.id = COALESCE(order_items.seller_id, products.seller_id)

                WHERE order_items.order_id = ?

                ORDER BY product_name ASC
            """, (order["order_id"],)).fetchall()

            order_data = dict(order)

            order_data["items"] = [
                dict(item) for item in order_items
            ]

            results.append(order_data)

        return results

    finally:
        connection.close()


def _get_seller_available_balance(connection, seller_id):

    sales = connection.execute("""
        SELECT COALESCE(SUM(
            order_items.quantity * order_items.unit_price
        ), 0) AS total_sales

        FROM order_items

        JOIN products
            ON order_items.product_id = products.id

        JOIN orders
            ON order_items.order_id = orders.id

        WHERE products.seller_id = ?
          AND LOWER(orders.status) IN ('completed', 'delivered')
          AND EXISTS (
              SELECT 1
              FROM payments
              WHERE payments.order_id = orders.id
                AND LOWER(payments.status) IN ('paid', 'completed')
          )
    """, (seller_id,)).fetchone()

    commissions = connection.execute("""
        SELECT COALESCE(SUM(commissions.amount), 0)
            AS total_commissions

        FROM commissions

        JOIN orders
            ON commissions.order_id = orders.id

        WHERE commissions.seller_id = ?
          AND LOWER(orders.status) IN ('completed', 'delivered')
          AND EXISTS (
              SELECT 1
              FROM payments
              WHERE payments.order_id = orders.id
                AND LOWER(payments.status) IN ('paid', 'completed')
          )
    """, (seller_id,)).fetchone()

    payouts = connection.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total_payouts
        FROM seller_payouts
        WHERE seller_id = ?
          AND LOWER(status) IN ('paid', 'completed', 'pending')
    """, (seller_id,)).fetchone()

    remaining = (
        (sales["total_sales"] or 0)
        - (commissions["total_commissions"] or 0)
        - (payouts["total_payouts"] or 0)
    )

    return max(remaining, 0)


def record_seller_payout(seller_id, amount):

    try:
        amount = float(amount)

        if not math.isfinite(amount):
            return False, "Enter a valid payout amount."

        amount = round(amount, 2)

        if amount <= 0:
            return False, "Payout amount must be greater than zero."

    except (TypeError, ValueError):
        return False, "Enter a valid payout amount."

    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        seller = connection.execute("""
            SELECT id
            FROM users
            WHERE id = ? AND role = 'seller'
        """, (seller_id,)).fetchone()

        if not seller:
            connection.rollback()
            return False, "Seller account not found."

        remaining = _get_seller_available_balance(
            connection,
            seller_id
        )

        if amount > remaining:
            connection.rollback()
            return False, (
                "Payout exceeds the seller's available balance."
            )

        connection.execute("""
            INSERT INTO seller_payouts (
                seller_id,
                amount,
                status,
                paid_at
            )
            VALUES (?, ?, 'paid', CURRENT_TIMESTAMP)
        """, (seller_id, amount))

        connection.commit()

        return True, "Seller payout recorded successfully."

    except Exception:
        connection.rollback()
        return False, "Unable to record seller payout."

    finally:
        connection.close()


def create_pending_seller_payout(seller_id, amount):

    try:
        amount = float(amount)

        if not math.isfinite(amount):
            return False, "Enter a valid payout amount."

        amount = round(amount, 2)

        if amount <= 0:
            return False, "Payout amount must be greater than zero."

    except (TypeError, ValueError):
        return False, "Enter a valid payout amount."

    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        seller = connection.execute("""
            SELECT id
            FROM users
            WHERE id = ? AND role = 'seller'
        """, (seller_id,)).fetchone()

        if not seller:
            connection.rollback()
            return False, "Seller account not found."

        remaining = _get_seller_available_balance(
            connection,
            seller_id
        )

        if amount > remaining:
            connection.rollback()
            return False, "Payout exceeds the seller's available balance."

        connection.execute("""
            INSERT INTO seller_payouts (
                seller_id,
                amount,
                status,
                paid_at
            )
            VALUES (?, ?, 'pending', NULL)
        """, (seller_id, amount))

        connection.commit()

        return True, "Pending payout created successfully."

    except Exception:
        connection.rollback()
        return False, "Unable to create pending payout."

    finally:
        connection.close()


def mark_seller_payout_paid(payout_id):

    connection = get_db_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")

        payout = connection.execute("""
            SELECT id
            FROM seller_payouts
            WHERE id = ?
              AND LOWER(status) = 'pending'
        """, (payout_id,)).fetchone()

        if not payout:
            connection.rollback()
            return False, "Pending payout not found."

        connection.execute("""
            UPDATE seller_payouts
            SET status = 'paid',
                paid_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND LOWER(status) = 'pending'
        """, (payout_id,))

        connection.commit()

        return True, "Payout marked as paid successfully."

    except Exception:
        connection.rollback()
        return False, "Unable to update payout."

    finally:
        connection.close()


def get_admin_payouts():

    connection = get_db_connection()

    try:
        return connection.execute("""
            SELECT
                seller_payouts.id AS payout_id,
                seller_payouts.seller_id,
                seller_payouts.amount,
                seller_payouts.status,
                seller_payouts.paid_at,
                users.full_name AS seller_name

            FROM seller_payouts

            JOIN users
                ON seller_payouts.seller_id = users.id

            ORDER BY
                CASE
                    WHEN LOWER(seller_payouts.status) = 'pending'
                    THEN 0
                    ELSE 1
                END,
                seller_payouts.id DESC
        """).fetchall()

    finally:
        connection.close()
