
from database import get_db_connection


def get_categories():
    connection = get_db_connection()

    try:
        categories = connection.execute("""
            SELECT * FROM categories
            ORDER BY name ASC
        """).fetchall()

        return categories

    finally:
        connection.close()


def add_product(
    seller_id,
    name,
    description,
    price,
    category_id,
    stock,
    images,
    pickup_location=""
):
    connection = get_db_connection()

    try:
        cursor = connection.execute("""
            INSERT INTO products (
                seller_id,
                category_id,
                name,
                description,
                price,
                stock,
                image,
                pickup_location,
                approval_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            seller_id,
            category_id,
            name,
            description,
            price,
            stock,
            images[0] if images else None,
            pickup_location.strip()
        ))

        product_id = cursor.lastrowid

        for image_path in images:
            connection.execute("""
                INSERT INTO product_images (
                    product_id,
                    image_path
                )
                VALUES (?, ?)
            """, (product_id, image_path))

        connection.commit()

        return True, "Product submitted successfully. It will appear publicly after seller verification and admin product approval."

    except Exception as error:
        connection.rollback()
        return False, f"Unable to upload product: {error}"

    finally:
        connection.close()


def get_seller_products(seller_id):
    connection = get_db_connection()

    try:
        products = connection.execute("""
            SELECT
                products.*,
                categories.name AS category_name
            FROM products
            LEFT JOIN categories
                ON products.category_id = categories.id
            WHERE products.seller_id = ?
            ORDER BY products.created_at DESC
        """, (seller_id,)).fetchall()

        return products

    finally:
        connection.close()


def get_product_images(product_id):
    connection = get_db_connection()

    try:
        images = connection.execute("""
            SELECT image_path
            FROM product_images
            WHERE product_id = ?
            ORDER BY id ASC
        """, (product_id,)).fetchall()

        return images

    finally:
        connection.close()


def get_seller_product(product_id, seller_id):
    connection = get_db_connection()

    try:
        product = connection.execute("""
            SELECT
                products.*,
                categories.name AS category_name
            FROM products
            LEFT JOIN categories
                ON products.category_id = categories.id
            WHERE products.id = ?
              AND products.seller_id = ?
        """, (product_id, seller_id)).fetchone()

        return product

    finally:
        connection.close()


def update_product(
    product_id,
    seller_id,
    name,
    description,
    price,
    category_id,
    stock,
    images=None,
    pickup_location=None
):
    connection = get_db_connection()

    try:
        product = connection.execute("""
            SELECT id
            FROM products
            WHERE id = ?
              AND seller_id = ?
        """, (product_id, seller_id)).fetchone()

        if not product:
            return False, "Product not found or unauthorized."

        if images:
            connection.execute("""
                UPDATE products
                SET
                    name = ?,
                    description = ?,
                    price = ?,
                    category_id = ?,
                    stock = ?,
                    image = ?,
                    pickup_location = COALESCE(?, pickup_location),
                    approval_status = 'pending', approval_note = NULL
                WHERE id = ?
                  AND seller_id = ?
            """, (
                name,
                description,
                price,
                category_id,
                stock,
                images[0],
                pickup_location.strip() if pickup_location is not None else None,
                product_id,
                seller_id
            ))

            connection.execute("""
                DELETE FROM product_images
                WHERE product_id = ?
            """, (product_id,))

            for image_path in images:
                connection.execute("""
                    INSERT INTO product_images (
                        product_id,
                        image_path
                    )
                    VALUES (?, ?)
                """, (product_id, image_path))

        else:
            connection.execute("""
                UPDATE products
                SET
                    name = ?,
                    description = ?,
                    price = ?,
                    category_id = ?,
                    stock = ?,
                    pickup_location = COALESCE(?, pickup_location),
                    approval_status = 'pending', approval_note = NULL
                WHERE id = ?
                  AND seller_id = ?
            """, (
                name,
                description,
                price,
                category_id,
                stock,
                pickup_location.strip() if pickup_location is not None else None,
                product_id,
                seller_id
            ))

        connection.commit()

        return True, "Product updated successfully!"

    except Exception as error:
        connection.rollback()
        return False, f"Unable to update product: {error}"

    finally:
        connection.close()


def delete_product(product_id, seller_id):
    connection = get_db_connection()

    try:
        product = connection.execute("""
            SELECT id
            FROM products
            WHERE id = ?
              AND seller_id = ?
        """, (product_id, seller_id)).fetchone()

        if not product:
            return False, "Product not found or unauthorized."

        connection.execute("""
            DELETE FROM products
            WHERE id = ?
              AND seller_id = ?
        """, (product_id, seller_id))

        connection.commit()

        return True, "Product deleted successfully!"

    except Exception as error:
        connection.rollback()
        return False, f"Unable to delete product: {error}"

    finally:
        connection.close()
