
from database import get_db_connection


# Only successfully fulfilled and paid orders count towards seller earnings.
ELIGIBLE_ORDER_STATUSES = ("completed", "delivered")
ELIGIBLE_PAYMENT_STATUSES = ("paid", "completed")


def get_seller_sales(seller_id):

    connection = get_db_connection()

    try:
        sales = connection.execute(
            """
            SELECT
                orders.id AS order_id,
                orders.order_number,
                orders.created_at,
                orders.status AS order_status,
                orders.total_amount,

                GROUP_CONCAT(products.name, ', ')
                    AS product_names,

                SUM(order_items.quantity)
                    AS total_quantity,

                SUM(
                    order_items.quantity * order_items.unit_price
                ) AS seller_total,

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
                ) AS payment_status,

                COALESCE((
                    SELECT SUM(commissions.amount)
                    FROM commissions
                    WHERE commissions.order_id = orders.id
                      AND commissions.seller_id = ?
                ), 0) AS commission_amount,

                (
                    SELECT commissions.status
                    FROM commissions
                    WHERE commissions.order_id = orders.id
                      AND commissions.seller_id = ?
                    ORDER BY commissions.id DESC
                    LIMIT 1
                ) AS commission_status

            FROM order_items

            JOIN products
                ON order_items.product_id = products.id

            JOIN orders
                ON order_items.order_id = orders.id

            WHERE products.seller_id = ?

            GROUP BY
                orders.id,
                orders.order_number,
                orders.created_at,
                orders.status,
                orders.total_amount

            ORDER BY
                orders.created_at DESC,
                orders.id DESC
            """,
            (seller_id, seller_id, seller_id)
        ).fetchall()

        return sales

    finally:
        connection.close()


def get_seller_order_details(seller_id, order_id):

    connection = get_db_connection()

    try:
        order = connection.execute(
            """
            SELECT
                orders.id AS order_id,
                orders.order_number,
                orders.created_at,
                orders.status AS order_status,

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
                ) AS payment_status,

                COALESCE((
                    SELECT SUM(commissions.amount)
                    FROM commissions
                    WHERE commissions.order_id = orders.id
                      AND commissions.seller_id = ?
                ), 0) AS commission_amount,

                (
                    SELECT commissions.rate
                    FROM commissions
                    WHERE commissions.order_id = orders.id
                      AND commissions.seller_id = ?
                    ORDER BY commissions.id DESC
                    LIMIT 1
                ) AS commission_rate,

                (
                    SELECT commissions.status
                    FROM commissions
                    WHERE commissions.order_id = orders.id
                      AND commissions.seller_id = ?
                    ORDER BY commissions.id DESC
                    LIMIT 1
                ) AS commission_status,

                (
                    SELECT deliveries.status
                    FROM deliveries
                    WHERE deliveries.order_id = orders.id
                    ORDER BY deliveries.id DESC
                    LIMIT 1
                ) AS delivery_status,

                (
                    SELECT deliveries.delivery_fee
                    FROM deliveries
                    WHERE deliveries.order_id = orders.id
                    ORDER BY deliveries.id DESC
                    LIMIT 1
                ) AS delivery_fee

            FROM orders

            WHERE orders.id = ?

            AND EXISTS (
                SELECT 1
                FROM order_items
                JOIN products
                    ON order_items.product_id = products.id
                WHERE order_items.order_id = orders.id
                  AND products.seller_id = ?
            )
            """,
            (
                seller_id,
                seller_id,
                seller_id,
                order_id,
                seller_id
            )
        ).fetchone()

        if order is None:
            return None

        items = connection.execute(
            """
            SELECT
                products.id AS product_id,
                products.name AS name,
                order_items.quantity,
                order_items.unit_price,

                (
                    order_items.quantity * order_items.unit_price
                ) AS subtotal

            FROM order_items

            JOIN products
                ON order_items.product_id = products.id

            WHERE order_items.order_id = ?
              AND products.seller_id = ?

            ORDER BY products.name
            """,
            (order_id, seller_id)
        ).fetchall()

        seller_total = sum(
            item["subtotal"] for item in items
        )

        return {
            "order": order,
            "items": items,
            "seller_total": seller_total
        }

    finally:
        connection.close()


def get_seller_financial_summary(seller_id):

    connection = get_db_connection()

    try:
        # Only completed/delivered orders with successful payments count.
        sales = connection.execute(
            """
            SELECT
                COALESCE(
                    SUM(
                        order_items.quantity * order_items.unit_price
                    ), 0
                ) AS total_sales

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
            """,
            (seller_id,)
        ).fetchone()

        # Only commissions belonging to eligible orders are counted.
        commissions = connection.execute(
            """
            SELECT
                COALESCE(SUM(commissions.amount), 0)
                    AS total_commissions,

                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(commissions.status) = 'pending'
                            THEN commissions.amount
                            ELSE 0
                        END
                    ), 0
                ) AS pending_commissions,

                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(commissions.status)
                                IN ('paid', 'completed')
                            THEN commissions.amount
                            ELSE 0
                        END
                    ), 0
                ) AS paid_commissions

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
            """,
            (seller_id,)
        ).fetchone()

        # Separate paid payouts from pending payout requests.
        payouts = connection.execute(
            """
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(status) IN ('paid', 'completed')
                            THEN amount
                            ELSE 0
                        END
                    ), 0
                ) AS total_paid_payouts,

                COALESCE(
                    SUM(
                        CASE
                            WHEN LOWER(status) = 'pending'
                            THEN amount
                            ELSE 0
                        END
                    ), 0
                ) AS pending_payouts

            FROM seller_payouts

            WHERE seller_id = ?
            """,
            (seller_id,)
        ).fetchone()

        total_sales = sales["total_sales"] or 0

        total_commissions = (
            commissions["total_commissions"] or 0
        )

        pending_commissions = (
            commissions["pending_commissions"] or 0
        )

        paid_commissions = (
            commissions["paid_commissions"] or 0
        )

        total_paid_payouts = (
            payouts["total_paid_payouts"] or 0
        )

        pending_payouts = (
            payouts["pending_payouts"] or 0
        )

        # Seller earnings after marketplace commissions.
        net_earnings = total_sales - total_commissions

        # Subtract both completed and pending payouts.
        remaining_balance = max(
            net_earnings
            - total_paid_payouts
            - pending_payouts,
            0
        )

        return {
            "total_sales": total_sales,
            "total_commissions": total_commissions,
            "pending_commissions": pending_commissions,
            "paid_commissions": paid_commissions,
            "net_earnings": net_earnings,
            "total_paid_payouts": total_paid_payouts,
            "pending_payouts": pending_payouts,
            "remaining_balance": remaining_balance
        }

    finally:
        connection.close()
