
import os
import smtplib
import uuid

from email.message import EmailMessage

from werkzeug.security import generate_password_hash, check_password_hash

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    current_app,
    send_from_directory
)

from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash

from database import init_db, get_db_connection

from services.auth import (
    register_user,
    login_user,
    create_password_reset,
    reset_password,
    get_user_account,
    update_user_account,
    change_user_password
)

from services.products import (
    get_categories,
    add_product,
    get_seller_products,
    get_seller_product,
    get_product_images,
    update_product,
    delete_product
)

from services.sales import (
    get_seller_sales,
    get_seller_order_details,
    get_seller_financial_summary
)

from services.order_services import (
    get_public_products,
    get_public_product,
    create_order,
    get_buyer_tracking,
    get_seller_tracking,
    get_notifications,
    get_unread_notification_count,
    mark_notifications_read,
    get_cart_items,
    get_cart_count,
    add_to_cart,
    update_cart_quantity,
    remove_from_cart,
    update_order_progress
)

from services.watchlist import (
    get_watchlist,
    add_to_watchlist,
    remove_from_watchlist,
    get_watchlist_ids
)

from services.messaging import (
    get_or_create_conversation,
    get_conversation,
    get_messages,
    add_chat_message,
    get_user_conversations,
    mark_messages_read,
    get_unread_message_count
)

from services.admin import (
    get_admin_dashboard_summary,
    get_admin_seller_finances,
    get_admin_orders,
    create_pending_seller_payout,
    mark_seller_payout_paid,
    get_admin_payouts
)


def send_reset_email(email, token):
    """Send a password-reset link to the user."""

    message = EmailMessage()

    message["Subject"] = "MU Marketplace - Password Reset"
    message["From"] = current_app.config["MAIL_DEFAULT_SENDER"]
    message["To"] = email

    reset_link = url_for(
        "reset_password_page",
        token=token,
        _external=True
    )

    message.set_content(f"""
Hello,

We received a request to reset your MU Marketplace password.

Click the link below to create a new password:

{reset_link}

This link expires in 30 minutes and can only be used once.

If you did not request this reset, you can ignore this email.

MU Marketplace Team
""")

    with smtplib.SMTP(
        current_app.config["MAIL_SERVER"],
        current_app.config["MAIL_PORT"]
    ) as server:

        server.starttls()

        server.login(
            current_app.config["MAIL_USERNAME"],
            current_app.config["MAIL_PASSWORD"]
        )

        server.send_message(message)


def create_app():

    app = Flask(__name__)

    app.config.from_object("config.Config")

    # Convert stored image references into browser-ready URLs.
    def image_src(image_reference):
        if not image_reference:
            return ""
        if image_reference.startswith(("https://", "http://")):
            return image_reference
        return url_for("static", filename=image_reference)

    app.jinja_env.filters["image_src"] = image_src

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "Set SECRET_KEY in your environment or Config."
        )

    # Product and profile image uploads.
    # Vercel's deployed source directory is read-only, so use /tmp there.
    # Local development continues using static/uploads as before.
    if os.environ.get("VERCEL"):
        upload_storage_root = os.path.join(
            os.environ.get("TMPDIR", "/tmp"),
            "mu_marketplace_uploads"
        )
    else:
        upload_storage_root = os.path.join(
            app.root_path,
            "static",
            "uploads"
        )

    app.config["UPLOAD_STORAGE_ROOT"] = upload_storage_root
    app.config["UPLOAD_FOLDER"] = os.path.join(
        upload_storage_root,
        "products"
    )
    app.config["PROFILE_UPLOAD_FOLDER"] = os.path.join(
        upload_storage_root,
        "profile_pictures"
    )
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PROFILE_UPLOAD_FOLDER"], exist_ok=True)

    # Keep existing template URLs (static/uploads/...) working for files
    # written to /tmp on Vercel. These temporary files are not persistent
    # across serverless instances; durable image storage is handled separately.
    if os.environ.get("VERCEL"):
        original_static_view = app.view_functions.get("static")

        def serve_static_or_uploaded_file(filename):
            if filename.startswith("uploads/"):
                relative_path = filename[len("uploads/"):]
                return send_from_directory(
                    app.config["UPLOAD_STORAGE_ROOT"],
                    relative_path
                )
            return original_static_view(filename=filename)

        app.view_functions["static"] = serve_static_or_uploaded_file

    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
    MAX_PRODUCT_IMAGES = 5

    def is_valid_image_upload(image):
        """Check both file extension and image signature; reject spoofed uploads."""
        if not image or not image.filename:
            return False
        extension = image.filename.rsplit(".", 1)[-1].lower() if "." in image.filename else ""
        if extension not in ALLOWED_EXTENSIONS:
            return False
        header = image.stream.read(12)
        image.stream.seek(0)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            detected = "png"
        elif header.startswith(b"\xff\xd8\xff"):
            detected = "jpg"
        elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            detected = "webp"
        else:
            return False
        return extension in ("jpg", "jpeg") if detected == "jpg" else extension == detected

    # Initialize database
    init_db()

    # Optional one-time admin bootstrap for hosted deployments.
    # Set ADMIN_BOOTSTRAP_NAME, ADMIN_BOOTSTRAP_EMAIL and
    # ADMIN_BOOTSTRAP_PASSWORD in the hosting environment. Never commit
    # the password to source control.
    admin_name = os.environ.get("ADMIN_BOOTSTRAP_NAME", "").strip()
    admin_email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "").strip().lower()
    admin_password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")
    if admin_name and admin_email and admin_password:
        if "@" not in admin_email or len(admin_password) < 12:
            raise RuntimeError(
                "Admin bootstrap requires a valid email and a password "
                "of at least 12 characters."
            )
        bootstrap_db = get_db_connection()
        try:
            existing_admin = bootstrap_db.execute(
                """
                SELECT id FROM users
                WHERE LOWER(email)=? AND role='admin'
                  AND COALESCE(is_deleted,0)=0
                """,
                (admin_email,)
            ).fetchone()
            password_hash = generate_password_hash(admin_password)
            if existing_admin:
                bootstrap_db.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, full_name = ?
                    WHERE id = ?
                    """,
                    (password_hash, admin_name, existing_admin["id"])
                )
            else:
                bootstrap_db.execute(
                    """
                    INSERT INTO users(full_name,email,phone,password_hash,role)
                    VALUES(?,?, '', ?, 'admin')
                    """,
                    (admin_name, admin_email, password_hash)
                )
            bootstrap_db.commit()
        except Exception:
            bootstrap_db.rollback()
            app.logger.exception("Admin bootstrap failed")
            raise
        finally:
            bootstrap_db.close()

    @app.before_request
    def enforce_marketplace_open_status():
        if request.endpoint is None or request.endpoint == "static":
            return None
        if session.get("user_role") == "admin":
            return None
        if request.endpoint in ("login", "logout", "marketplace_closed"):
            # Keep the login/logout and closure-information pages reachable.
            # All other pages require an open marketplace or an admin session.
            return None
        db = get_db_connection()
        try:
            row = db.execute("SELECT setting_value FROM marketplace_settings WHERE setting_key='is_open'").fetchone()
            is_open = not row or row["setting_value"] == "1"
        finally:
            db.close()
        if not is_open:
            return redirect(url_for("marketplace_closed"))
        return None

    @app.route("/marketplace-closed")
    def marketplace_closed():
        db = get_db_connection()
        try:
            row = db.execute("SELECT setting_value FROM marketplace_settings WHERE setting_key='is_open'").fetchone()
            is_open = not row or row["setting_value"] == "1"
        finally:
            db.close()
        if is_open:
            return redirect(url_for("home"))
        return render_template("marketplace_closed.html")

    @app.route("/service-worker.js")
    def service_worker():
        response = app.send_static_file("service-worker.js")
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.route("/buy-from-abroad", methods=["GET", "POST"])
    def buy_from_abroad():
        if "user_id" not in session:
            flash("Please log in or create an account before placing an overseas order request.")
            return redirect(url_for("login", next=url_for("buy_from_abroad")))

        if session.get("user_role") not in ("buyer", "seller", "admin"):
            flash("A valid marketplace account is required.")
            return redirect(url_for("login"))

        if request.method == "POST":
            item_description = request.form.get("item_description", "").strip()
            if not item_description:
                flash("Please describe what you want to buy.")
                return redirect(url_for("buy_from_abroad"))
            db = get_db_connection()
            try:
                user = db.execute(
                    "SELECT full_name, email, phone FROM users WHERE id=? AND COALESCE(is_deleted,0)=0",
                    (session["user_id"],)
                ).fetchone()
                if not user or not user["phone"]:
                    flash("Your account must have a phone number before placing an overseas order. Please update your account.")
                    return redirect(url_for("buy_from_abroad"))
                cursor = db.execute(
                    "INSERT INTO overseas_orders(user_id, item_description) VALUES (?, ?)",
                    (session["user_id"], item_description)
                )
                overseas_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO notifications(user_id, audience_role, order_id, title, message) VALUES (NULL, 'admin', NULL, ?, ?)",
                    ("New Buy from Abroad order request", f"Overseas request #{overseas_id} from {user['full_name']} ({user['email']}, {user['phone']}): {item_description[:350]}")
                )
                db.commit()
                flash("Your overseas order request has been submitted. The admin team has been notified.")
                return redirect(url_for("home"))
            except Exception:
                db.rollback()
                current_app.logger.exception("Unable to save overseas order request")
                flash("We could not submit your request. Please try again.")
            finally:
                db.close()
        return render_template("buy_from_abroad.html")

    @app.route("/admin/overseas-order/<int:order_id>/status", methods=["POST"])
    def admin_update_overseas_order(order_id):
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Administrator access is required.")
            return redirect(url_for("login"))
        status = request.form.get("status", "pending").strip().lower()
        allowed = {"pending", "processing", "ordered", "shipped", "delivered", "cancelled"}
        if status not in allowed:
            flash("Invalid overseas order status.")
            return redirect(url_for("admin_dashboard"))
        db = get_db_connection()
        try:
            db.execute("UPDATE overseas_orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, order_id))
            db.commit()
            flash("Overseas order status updated.")
        finally:
            db.close()
        return redirect(url_for("admin_dashboard"))

    # Explicitly publish crawl directives for public storefront pages.
    # This also overrides any application-level noindex header on these routes.
    @app.after_request
    def set_search_indexing_headers(response):
        public_indexable_endpoints = {
            "home",
            "browse_products",
            "product_page",
            "robots_txt",
            "sitemap_xml",
        }
        if request.endpoint in public_indexable_endpoints:
            response.headers["X-Robots-Tag"] = "index, follow"
        return response

    # Public discovery endpoints: allow search engines to crawl the storefront.
    @app.route("/robots.txt")
    def robots_txt():
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /admin/\n"
            "Disallow: /buyer/\n"
            "Disallow: /seller/\n"
            "Disallow: /cart\n"
            "Disallow: /messages/\n"
            "Disallow: /account/\n"
            "Sitemap: " + request.url_root.rstrip("/") + "/sitemap.xml\n"
        )
        return app.response_class(body, mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        base_url = request.url_root.rstrip("/")
        urls = [base_url + "/", base_url + "/products"]
        try:
            products = get_public_products()
            urls.extend(
                base_url + url_for("product_page", product_id=item["id"])
                for item in products
            )
        except Exception:
            current_app.logger.exception("Unable to build product sitemap")
        xml_urls = "".join(
            "<url><loc>" + url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</loc></url>" for url in urls
        )
        xml = '<?xml version="1.0" encoding="UTF-8"?>' \
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' \
              + xml_urls + '</urlset>'
        return app.response_class(xml, mimetype="application/xml")

    # Homepage
    @app.route("/")
    def home():
        # Public catalogue: available products are visible to visitors
        # whether or not they are logged in.
        search = request.args.get("q", "").strip()
        category_raw = request.args.get("category", "").strip()
        min_raw = request.args.get("min_price", "").strip()
        max_raw = request.args.get("max_price", "").strip()
        sort = request.args.get("sort", "newest")
        try:
            category_id = int(category_raw) if category_raw else None
            min_price = float(min_raw) if min_raw else None
            max_price = float(max_raw) if max_raw else None
            if (min_price is not None and min_price < 0) or (max_price is not None and max_price < 0) or (min_price is not None and max_price is not None and min_price > max_price):
                raise ValueError
        except ValueError:
            flash("Enter a valid category and price range.")
            return redirect(url_for("home"))

        home_context = {
            "home_products": get_public_products(search, category_id, min_price, max_price, sort),
            "categories": get_categories(),
            "search": search,
            "selected_category": category_raw,
            "min_price": min_raw,
            "max_price": max_raw,
            "selected_sort": sort,
        }

        # Show the cart and notification icons on the public home page
        # only when the signed-in account is a buyer.
        if "user_id" in session and session.get("user_role") == "buyer":
            home_context["cart_count"] = get_cart_count(session["user_id"])
            home_context["watchlist_ids"] = get_watchlist_ids(session["user_id"])
            home_context["notifications"] = get_notifications(
                user_id=session["user_id"]
            )
            home_context["unread_count"] = get_unread_notification_count(
                user_id=session["user_id"]
            )

        return render_template("index.html", **home_context)

    # Public product catalogue and product ordering
    @app.route("/products")
    def browse_products():
        # Keep old links working, but show filtered results on the homepage.
        return redirect(url_for(
            "home",
            q=request.args.get("q", "").strip(),
            category=request.args.get("category", "").strip(),
            min_price=request.args.get("min_price", "").strip(),
            max_price=request.args.get("max_price", "").strip(),
            sort=request.args.get("sort", "newest"),
        ))

    @app.route("/product/<int:product_id>")
    def product_page(product_id):
        product = get_public_product(product_id)
        if not product:
            flash("Product not found or no longer available.")
            return redirect(url_for("home"))
        review_db = get_db_connection()
        try:
            reviews = review_db.execute("""SELECT r.rating,r.review,r.created_at,u.full_name
                FROM product_reviews r JOIN users u ON u.id=r.buyer_id
                WHERE r.product_id=? ORDER BY r.created_at DESC LIMIT 30""", (product_id,)).fetchall()
            average_rating = review_db.execute("SELECT AVG(rating) FROM product_reviews WHERE product_id=?", (product_id,)).fetchone()[0]
        finally:
            review_db.close()
        return render_template("product.html", product=product, reviews=reviews, average_rating=average_rating)

    @app.route("/buy/<int:product_id>", methods=["GET", "POST"])
    def checkout(product_id):
        if "user_id" not in session:
            flash("Please log in with a buyer account before placing an order.")
            return redirect(url_for("login"))
        if session.get("user_role") != "buyer":
            flash("Only buyer accounts can place orders.")
            return redirect(url_for("dashboard"))
        product = get_public_product(product_id)
        if not product:
            flash("Product not found or no longer available.")
            return redirect(url_for("home"))
        if request.method == "POST":
            success, message = create_order(
                session["user_id"], product_id,
                request.form.get("quantity", 1),
                request.form.get("payment_method", ""),
                request.form.get("delivery_address", "").strip(),
                request.form.get("coupon_code", ""),
                request.form.get("contact_phone", "").strip()
            )
            flash(message)
            if success:
                return redirect(url_for("buyer_dashboard"))
        connection = get_db_connection()
        try:
            buyer_row = connection.execute(
                "SELECT phone FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
            current_user_phone = buyer_row["phone"] if buyer_row else ""
        finally:
            connection.close()
        return render_template("checkout.html", product=product, current_user_phone=current_user_phone)

    @app.route("/cart")
    def buyer_cart():
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account to view your cart.")
            return redirect(url_for("login"))
        items = get_cart_items(session["user_id"])
        total = sum(float(item["line_total"]) for item in items)
        return render_template("cart.html", items=items, total=total,
                               cart_count=get_cart_count(session["user_id"]))

    @app.route("/cart/add/<int:product_id>", methods=["POST"])
    def cart_add(product_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account to add products to your cart.")
            return redirect(url_for("login"))
        success, message = add_to_cart(session["user_id"], product_id,
                                       request.form.get("quantity", 1))
        flash(message)
        return redirect(request.referrer or url_for("buyer_cart"))

    @app.route("/cart/update/<int:cart_item_id>", methods=["POST"])
    def cart_update(cart_item_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            return redirect(url_for("login"))
        success, message = update_cart_quantity(session["user_id"], cart_item_id,
                                                request.form.get("quantity", 1))
        flash(message)
        return redirect(url_for("buyer_cart"))

    @app.route("/cart/remove/<int:cart_item_id>", methods=["POST"])
    def cart_remove(cart_item_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            return redirect(url_for("login"))
        success, message = remove_from_cart(session["user_id"], cart_item_id)
        flash(message)
        return redirect(url_for("buyer_cart"))

    @app.route("/notifications/poll")
    def notifications_poll():
        if "user_id" not in session:
            return {"success": False, "notifications": []}, 401
        role = session.get("user_role")
        since_id = request.args.get("since", "0")
        try:
            since_id = max(0, int(since_id))
        except (TypeError, ValueError):
            since_id = 0

        connection = get_db_connection()
        try:
            if role == "admin":
                rows = connection.execute("""
                    SELECT id, title, message, created_at
                    FROM notifications
                    WHERE audience_role = 'admin' AND id > ?
                    ORDER BY id ASC LIMIT 30
                """, (since_id,)).fetchall()
            else:
                rows = connection.execute("""
                    SELECT id, title, message, created_at
                    FROM notifications
                    WHERE user_id = ? AND id > ?
                    ORDER BY id ASC LIMIT 30
                """, (session["user_id"], since_id)).fetchall()
            return {
                "success": True,
                "notifications": [
                    {"id": row["id"], "title": row["title"],
                     "message": row["message"], "created_at": row["created_at"]}
                    for row in rows
                ]
            }
        finally:
            connection.close()

    @app.route("/notifications/read", methods=["POST"])
    def notifications_read():
        if "user_id" not in session:
            return {"success": False, "message": "Login required."}, 401
        role = session.get("user_role")
        if role == "admin":
            mark_notifications_read(role="admin")
        else:
            mark_notifications_read(user_id=session["user_id"])
        return {"success": True}

    @app.route("/admin/update-order/<int:order_id>", methods=["POST"])
    def admin_update_order(order_id):
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("You are not authorized to update orders.")
            return redirect(url_for("login"))
        if request.form.get("order_completed") == "yes":
            selected_order_status = "completed"
        elif request.form.get("order_confirmed") == "yes":
            selected_order_status = "confirmed"
        else:
            selected_order_status = "processing"

        if request.form.get("delivery_delivered") == "yes":
            selected_delivery_status = "delivered"
        elif request.form.get("delivery_on_the_way") == "yes":
            selected_delivery_status = "on_the_way"
        elif request.form.get("delivery_collected") == "yes":
            selected_delivery_status = "collected"
        else:
            selected_delivery_status = "pending"

        success, message = update_order_progress(
            order_id,
            selected_order_status,
            request.form.get("payment_received") == "yes",
            selected_delivery_status,
            request.form.get("payment_method", "cash")
        )
        flash(message)
        return redirect(url_for("admin_dashboard"))

    # Health check
    @app.route("/health")
    def health():
        return {
            "status": "success",
            "message": "MU Marketplace is running!"
        }, 200

    # Registration
    @app.route("/register", methods=["GET", "POST"])
    def register():

        if request.method == "POST":

            full_name = request.form.get(
                "full_name", ""
            ).strip()

            email = request.form.get(
                "email", ""
            ).strip()

            phone = request.form.get(
                "phone", ""
            ).strip()

            password = request.form.get(
                "password", ""
            )

            role = request.form.get(
                "role", "buyer"
            )

            success, message = register_user(
                full_name,
                email,
                phone,
                password,
                role
            )

            flash(message)

            if success:
                return redirect(url_for("login"))

        return render_template("register.html")

    # ==================================================
    # LOGIN
    # ==================================================

    @app.route("/login", methods=["GET", "POST"])
    def login():

        if request.method == "POST":

            email = request.form.get(
                "email", ""
            ).strip().lower()

            password = request.form.get(
                "password", ""
            )

            client_ip = request.remote_addr or "unknown"
            throttle_db = get_db_connection()
            try:
                throttle_db.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now','-1 day')")
                recent_attempts = throttle_db.execute("""SELECT COUNT(*) FROM login_attempts
                    WHERE email=? AND ip_address=? AND attempted_at >= datetime('now','-15 minutes')""",
                    (email.lower(), client_ip)).fetchone()[0]
                if recent_attempts >= 5:
                    flash("Too many login attempts. Please wait 15 minutes before trying again.")
                    return render_template("login.html"), 429
            finally:
                throttle_db.close()

            matching_accounts = []

            for role in ("buyer", "seller", "admin"):

                user = login_user(
                    email,
                    password,
                    role
                )

                if user:
                    matching_accounts.append(user)

            if not matching_accounts:
                failed_db = get_db_connection()
                try:
                    failed_db.execute("INSERT INTO login_attempts(email,ip_address) VALUES(?,?)", (email.lower(), client_ip))
                    failed_db.commit()
                finally:
                    failed_db.close()
                flash("Incorrect email or password.")
                return render_template("login.html")

            admin_account = next(
                (
                    account for account in matching_accounts
                    if account["role"] == "admin"
                ),
                None
            )

            if admin_account:

                session.clear()

                session["user_id"] = admin_account["id"]
                session["user_name"] = admin_account["full_name"]
                session["user_role"] = "admin"

                flash("Admin login successful!")

                return redirect(url_for("dashboard"))

            # Check closure before account selection so valid buyer/seller
            # credentials always receive the closure notice instead of being
            # sent to the account chooser. Admin login was handled above.
            status_db = get_db_connection()
            try:
                status_row = status_db.execute(
                    "SELECT setting_value FROM marketplace_settings WHERE setting_key='is_open'"
                ).fetchone()
                marketplace_is_open = not status_row or status_row["setting_value"] == "1"
            finally:
                status_db.close()

            if not marketplace_is_open:
                flash("MU Marketplace is temporarily closed. Please try again later.")
                return render_template("marketplace_closed.html"), 503

            if len(matching_accounts) > 1:

                session.clear()

                session["pending_accounts"] = [
                    {
                        "id": account["id"],
                        "full_name": account["full_name"],
                        "role": account["role"]
                    }
                    for account in matching_accounts
                ]

                return redirect(url_for("choose_account"))

            user = matching_accounts[0]
            success_db = get_db_connection()
            try:
                success_db.execute("DELETE FROM login_attempts WHERE email=? AND ip_address=?", (email.lower(), client_ip))
                success_db.commit()
            finally:
                success_db.close()

            session.clear()

            session["user_id"] = user["id"]
            session["user_name"] = user["full_name"]
            session["user_role"] = user["role"]

            flash("Login successful!")

            return redirect(url_for("dashboard"))

        return render_template("login.html")

    # ==================================================
    # ACCOUNT SELECTION
    # ==================================================

    @app.route("/choose-account", methods=["GET", "POST"])
    def choose_account():

        accounts = session.get("pending_accounts")

        if not accounts:
            flash("Please log in first.")
            return redirect(url_for("login"))

        if request.method == "POST":

            selected_role = request.form.get("role", "")

            selected_account = next(
                (
                    account for account in accounts
                    if account["role"] == selected_role
                    and account["role"] in ("buyer", "seller", "admin")
                ),
                None
            )

            if not selected_account:
                flash("Invalid account selection.")
                return redirect(url_for("choose_account"))

            session.clear()

            session["user_id"] = selected_account["id"]
            session["user_name"] = selected_account["full_name"]
            session["user_role"] = selected_account["role"]

            flash(
                f"Logged in as {selected_account['role'].capitalize()}!"
            )

            return redirect(url_for("dashboard"))

        return render_template(
            "choose_account.html",
            accounts=accounts
        )

    # ==================================================
    # MAIN DASHBOARD REDIRECT
    # ==================================================

    @app.route("/dashboard")
    def dashboard():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        role = session.get("user_role")

        if role == "buyer":
            return redirect(url_for("buyer_dashboard"))

        if role == "seller":
            return redirect(url_for("seller_dashboard"))

        if role == "admin":
            return redirect(url_for("admin_dashboard"))

        session.clear()

        flash("Invalid account session.")
        return redirect(url_for("login"))

    # ==================================================
    # MESSAGING: BUYER -> ADMIN -> SELLER
    # ==================================================

    @app.route("/messages")
    def message_inbox():
        if "user_id" not in session:
            flash("Please log in to view messages.")
            return redirect(url_for("login"))
        role = session.get("user_role")
        if role not in ("buyer", "seller", "admin"):
            return redirect(url_for("login"))
        conversations = get_user_conversations(session["user_id"], role)
        return render_template(
            "message_inbox.html",
            conversations=conversations,
            role=role
        )

    @app.route("/messages/product/<int:product_id>", methods=["GET", "POST"])
    def product_chat(product_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account to ask about a product.")
            return redirect(url_for("login"))
        conversation = get_or_create_conversation(session["user_id"], product_id)
        if not conversation:
            flash("This product is unavailable for enquiries.")
            return redirect(url_for("home"))
        if request.method == "POST":
            if add_chat_message(
                conversation["id"], "buyer", session["user_id"],
                "admin", request.form.get("message", "")
            ):
                flash("Your message has been sent to the admin.")
            else:
                flash("Please enter a message of up to 3,000 characters.")
            return redirect(url_for("product_chat", product_id=product_id))
        mark_messages_read(conversation["id"], "buyer", session["user_id"])
        messages = get_messages(conversation["id"], "buyer")
        return render_template(
            "chat.html", conversation=conversation,
            messages=messages, role="buyer"
        )

    @app.route("/messages/<int:conversation_id>/send", methods=["POST"])
    def send_chat_message(conversation_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        role = session.get("user_role")
        conversation = get_conversation(conversation_id)
        if not conversation:
            flash("Conversation not found.")
            return redirect(url_for("message_inbox"))
        if role == "buyer" and conversation["buyer_id"] == session["user_id"]:
            recipient = "admin"
        elif role == "seller" and conversation["seller_id"] == session["user_id"]:
            if not get_messages(conversation_id, "seller"):
                flash("You can reply only after the admin forwards the enquiry to you.")
                return redirect(url_for("message_inbox"))
            recipient = "admin"
        else:
            flash("You are not authorized to send a message in this conversation.")
            return redirect(url_for("message_inbox"))
        if add_chat_message(
            conversation_id, role, session["user_id"],
            recipient, request.form.get("message", "")
        ):
            flash("Your message has been sent to the admin.")
        else:
            flash("Please enter a message of up to 3,000 characters.")
        return redirect(url_for("view_chat", conversation_id=conversation_id))

    @app.route("/messages/<int:conversation_id>")
    def view_chat(conversation_id):
        if "user_id" not in session:
            return redirect(url_for("login"))
        role = session.get("user_role")
        conversation = get_conversation(conversation_id)
        if not conversation:
            flash("Conversation not found.")
            return redirect(url_for("message_inbox"))
        if role == "buyer" and conversation["buyer_id"] == session["user_id"]:
            pass
        elif role == "seller" and conversation["seller_id"] == session["user_id"]:
            seller_visible_messages = get_messages(conversation_id, "seller")
            if not seller_visible_messages:
                flash("This enquiry is not available to the seller until the admin forwards it.")
                return redirect(url_for("message_inbox"))
        elif role == "admin":
            pass
        else:
            flash("You are not authorized to view this conversation.")
            return redirect(url_for("message_inbox"))
        mark_messages_read(conversation_id, role, session["user_id"])
        return render_template(
            "chat.html", conversation=conversation,
            messages=get_messages(conversation_id, role), role=role
        )

    @app.route("/admin/messages/<int:conversation_id>", methods=["GET", "POST"])
    def admin_message_chat(conversation_id):
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only the admin can manage and forward messages.")
            return redirect(url_for("login"))
        conversation = get_conversation(conversation_id)
        if not conversation:
            flash("Conversation not found.")
            return redirect(url_for("message_inbox"))
        if request.method == "POST":
            recipient = request.form.get("recipient_role", "")
            if recipient not in ("buyer", "seller"):
                flash("Choose whether to forward the message to the buyer or seller.")
            elif add_chat_message(
                conversation_id, "admin", session["user_id"],
                recipient, request.form.get("message", "")
            ):
                flash("Message forwarded successfully.")
            else:
                flash("Please enter a message of up to 3,000 characters.")
            return redirect(url_for("admin_message_chat", conversation_id=conversation_id))
        mark_messages_read(conversation_id, "admin", session["user_id"])
        return render_template(
            "chat.html", conversation=conversation,
            messages=get_messages(conversation_id, "admin"), role="admin"
        )

    # ==================================================
    # ADMIN DASHBOARD
    # ==================================================

    @app.route("/admin/dashboard")
    def admin_dashboard():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "admin":
            flash("You are not authorized to access the admin dashboard.")
            return redirect(url_for("dashboard"))

        summary = get_admin_dashboard_summary()
        sellers = get_admin_seller_finances()
        orders = get_admin_orders()
        payouts = get_admin_payouts()
        overseas_db = get_db_connection()
        try:
            overseas_orders = overseas_db.execute("""
                SELECT overseas_orders.*, users.full_name AS customer_name,
                       users.email AS customer_email, users.phone AS customer_phone
                FROM overseas_orders JOIN users ON users.id = overseas_orders.user_id
                ORDER BY overseas_orders.created_at DESC, overseas_orders.id DESC
            """).fetchall()
        finally:
            overseas_db.close()
        notifications = get_notifications(role="admin")
        unread_count = get_unread_notification_count(role="admin")
        message_conversations = get_user_conversations(session["user_id"], "admin")
        unread_message_count = get_unread_message_count(session["user_id"], "admin")
        admin_db = get_db_connection()
        try:
            admin_accounts = admin_db.execute("""
                SELECT id, full_name, email, created_at
                FROM users
                WHERE role='admin' AND COALESCE(is_deleted,0)=0
                ORDER BY id ASC
            """).fetchall()
        finally:
            admin_db.close()

        return render_template(
            "admin_dashboard.html",
            user_name=session.get("user_name"),
            summary=summary,
            sellers=sellers,
            orders=orders,
            payouts=payouts,
            overseas_orders=overseas_orders,
            notifications=notifications,
            unread_count=unread_count,
            message_conversations=message_conversations,
            unread_message_count=unread_message_count,
            admin_accounts=admin_accounts
        )


    # ==================================================
    # ADMIN DASHBOARD SECTION PAGES
    # ==================================================

    def render_admin_section_page(page_title, section_template, **extra):
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only an administrator can access this page.")
            return redirect(url_for("login"))
        notifications = get_notifications(role="admin")
        unread_count = get_unread_notification_count(role="admin")
        message_conversations = get_user_conversations(session["user_id"], "admin")
        unread_message_count = get_unread_message_count(session["user_id"], "admin")
        context = {
            "page_title": page_title,
            "section_template": section_template,
            "notifications": notifications,
            "unread_count": unread_count,
            "message_conversations": message_conversations,
            "unread_message_count": unread_message_count,
        }
        context.update(extra)
        return render_template("admin_section_page.html", **context)

    @app.route("/admin/manage-logins")
    def admin_login_management_page():
        admin_db = get_db_connection()
        try:
            admin_accounts = admin_db.execute("""
                SELECT id, full_name, email, created_at
                FROM users
                WHERE role='admin' AND COALESCE(is_deleted,0)=0
                ORDER BY id ASC
            """).fetchall()
        finally:
            admin_db.close()
        return render_admin_section_page(
            "Admin Login Management",
            "admin_sections/admin_login_management.html",
            admin_accounts=admin_accounts
        )

    @app.route("/admin/product-enquiries")
    def admin_product_enquiries_page():
        conversations = get_user_conversations(session.get("user_id"), "admin") if session.get("user_role") == "admin" else []
        return render_admin_section_page(
            "Product Enquiry Messages",
            "admin_sections/admin_product_enquiries.html",
            message_conversations=conversations
        )

    @app.route("/admin/marketplace-overview")
    def admin_marketplace_overview_page():
        return render_admin_section_page(
            "Marketplace Overview",
            "admin_sections/admin_marketplace_overview.html",
            summary=get_admin_dashboard_summary()
        )

    @app.route("/admin/financial-overview")
    def admin_financial_overview_page():
        return render_admin_section_page(
            "Financial Overview",
            "admin_sections/admin_financial_overview.html",
            summary=get_admin_dashboard_summary()
        )

    @app.route("/admin/seller-finances")
    def admin_seller_finances_page():
        return render_admin_section_page(
            "Seller Finances",
            "admin_sections/admin_seller_finances.html",
            sellers=get_admin_seller_finances()
        )

    @app.route("/admin/seller-payouts")
    def admin_seller_payouts_page():
        return render_admin_section_page(
            "Seller Payout Records",
            "admin_sections/admin_seller_payouts.html",
            payouts=get_admin_payouts()
        )

    @app.route("/admin/marketplace-orders")
    def admin_marketplace_orders_page():
        return render_admin_section_page(
            "Marketplace Orders",
            "admin_sections/admin_marketplace_orders.html",
            orders=get_admin_orders()
        )

    # ==================================================
    # ADMIN LOGIN DETAILS AND ADDITIONAL ADMIN ACCOUNTS
    # ==================================================

    @app.route("/admin/manage-logins", methods=["POST"])
    def admin_manage_logins():
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only an administrator can manage admin login details.")
            return redirect(url_for("login"))

        action = request.form.get("action", "").strip()
        connection = get_db_connection()
        try:
            if action == "update_own":
                current_password = request.form.get("current_password", "")
                new_email = request.form.get("new_email", "").strip().lower()
                new_password = request.form.get("new_password", "")
                row = connection.execute("""
                    SELECT password_hash FROM users
                    WHERE id=? AND role='admin' AND COALESCE(is_deleted,0)=0
                """, (session["user_id"],)).fetchone()
                if not row or not check_password_hash(row["password_hash"], current_password):
                    flash("Your current password is incorrect.")
                    return redirect(url_for("admin_dashboard"))
                if not new_email or "@" not in new_email:
                    flash("Enter a valid email address.")
                    return redirect(url_for("admin_dashboard"))
                if new_password and len(new_password) < 8:
                    flash("The new password must contain at least 8 characters.")
                    return redirect(url_for("admin_dashboard"))
                duplicate = connection.execute("""
                    SELECT id FROM users
                    WHERE email=? AND role='admin' AND id<>? AND COALESCE(is_deleted,0)=0
                """, (new_email, session["user_id"])).fetchone()
                if duplicate:
                    flash("That email is already used by another admin account.")
                    return redirect(url_for("admin_dashboard"))
                if new_password:
                    connection.execute("""
                        UPDATE users SET email=?, password_hash=? WHERE id=? AND role='admin'
                    """, (new_email, generate_password_hash(new_password), session["user_id"]))
                else:
                    connection.execute("""
                        UPDATE users SET email=? WHERE id=? AND role='admin'
                    """, (new_email, session["user_id"]))
                connection.commit()
                flash("Your admin login details have been updated.")
            elif action == "add_admin":
                full_name = request.form.get("full_name", "").strip()
                email = request.form.get("email", "").strip().lower()
                password = request.form.get("password", "")
                if not full_name or not email or "@" not in email or len(password) < 8:
                    flash("Enter a name, valid email and password of at least 8 characters.")
                    return redirect(url_for("admin_dashboard"))
                exists = connection.execute("""
                    SELECT id FROM users
                    WHERE email=? AND role='admin' AND COALESCE(is_deleted,0)=0
                """, (email,)).fetchone()
                if exists:
                    flash("An admin account already uses that email address.")
                    return redirect(url_for("admin_dashboard"))
                connection.execute("""
                    INSERT INTO users(full_name,email,phone,password_hash,role)
                    VALUES(?,?, '', ?, 'admin')
                """, (full_name, email, generate_password_hash(password)))
                connection.commit()
                flash("Additional admin login created. It opens the same Admin Dashboard.")
            else:
                flash("Unknown admin login action.")
        except Exception:
            connection.rollback()
            flash("Unable to save admin login details. Check that the email is not already in use.")
        finally:
            connection.close()
        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # CREATE PENDING SELLER PAYOUT
    # ==================================================

    @app.route("/admin/create-payout", methods=["POST"])
    def admin_create_payout():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "admin":
            flash("You are not authorized to perform this action.")
            return redirect(url_for("dashboard"))

        seller_id = request.form.get("seller_id", "")
        amount = request.form.get("amount", "")

        try:
            seller_id = int(seller_id)

            success, message = create_pending_seller_payout(
                seller_id,
                amount
            )

        except (ValueError, TypeError):
            success = False
            message = "Invalid seller or payout amount."

        flash(message)

        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # MARK SELLER PAYOUT AS PAID
    # ==================================================

    @app.route(
        "/admin/mark-payout-paid/<int:payout_id>",
        methods=["POST"]
    )
    def admin_mark_payout_paid(payout_id):

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "admin":
            flash("You are not authorized to perform this action.")
            return redirect(url_for("dashboard"))

        success, message = mark_seller_payout_paid(payout_id)

        flash(message)

        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # BUYER WATCHLIST
    # ==================================================

    @app.route("/buyer/watchlist")
    def buyer_watchlist():
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account to view your Watchlist.")
            return redirect(url_for("login"))
        return render_template(
            "watchlist.html",
            products=get_watchlist(session["user_id"])
        )

    @app.route("/buyer/watchlist/<int:product_id>/add", methods=["POST"])
    def watchlist_add(product_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account to save products.")
            return redirect(url_for("login"))
        success, message = add_to_watchlist(session["user_id"], product_id)
        flash(message)
        return redirect(request.referrer or url_for("buyer_watchlist"))

    @app.route("/buyer/watchlist/<int:product_id>/remove", methods=["POST"])
    def watchlist_remove(product_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Please log in with a buyer account.")
            return redirect(url_for("login"))
        success, message = remove_from_watchlist(session["user_id"], product_id)
        flash(message)
        return redirect(request.referrer or url_for("buyer_watchlist"))

    # ==================================================
    # BUYER DASHBOARD
    # ==================================================

    @app.route("/buyer/dashboard")
    def buyer_dashboard():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "buyer":
            flash("You are not authorized to access that dashboard.")
            return redirect(url_for("dashboard"))

        tracking = get_buyer_tracking(session["user_id"])
        notifications = get_notifications(user_id=session["user_id"])
        unread_count = get_unread_notification_count(user_id=session["user_id"])
        cart_count = get_cart_count(session["user_id"])
        message_conversations = get_user_conversations(session["user_id"], "buyer")
        unread_message_count = get_unread_message_count(session["user_id"], "buyer")
        watchlist_count = len(get_watchlist(session["user_id"]))
        return render_template(
            "buyer_dashboard.html",
            user_name=session.get("user_name"),
            tracking=tracking,
            notifications=notifications,
            unread_count=unread_count,
            cart_count=cart_count,
            message_conversations=message_conversations,
            unread_message_count=unread_message_count,
            watchlist_count=watchlist_count
        )

    # ==================================================
    # SELLER DASHBOARD
    # ==================================================

    @app.route("/seller/dashboard")
    def seller_dashboard():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("You are not authorized to access that dashboard.")
            return redirect(url_for("dashboard"))

        seller_id = session["user_id"]

        products = get_seller_products(seller_id)

        financial_summary = get_seller_financial_summary(
            seller_id
        )
        tracking = get_seller_tracking(seller_id)
        seller_profile = get_user_account(seller_id)
        notifications = get_notifications(user_id=seller_id)
        unread_count = get_unread_notification_count(user_id=seller_id)
        message_conversations = get_user_conversations(seller_id, "seller")
        unread_message_count = get_unread_message_count(seller_id, "seller")

        return render_template(
            "seller_dashboard.html",
            user_name=session.get("user_name"),
            seller_profile=seller_profile,
            products=products,
            financial_summary=financial_summary,
            tracking=tracking,
            notifications=notifications,
            unread_count=unread_count,
            message_conversations=message_conversations,
            unread_message_count=unread_message_count
        )

    # ==================================================
    # SELLER ACCOUNT MANAGEMENT
    # ==================================================

    @app.route("/seller/account", methods=["GET", "POST"])
    def seller_account():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can manage this account.")
            return redirect(url_for("dashboard"))

        user_id = session["user_id"]

        if request.method == "POST":

            action = request.form.get("action", "")

            if action == "upload_profile_picture":
                image = request.files.get("profile_picture")
                if not image or not image.filename:
                    flash("Please choose a profile picture.")
                else:
                    extension = image.filename.rsplit(".", 1)[-1].lower() if "." in image.filename else ""
                    image.stream.seek(0, os.SEEK_END)
                    image_size = image.stream.tell()
                    image.stream.seek(0)
                    if image_size > 5 * 1024 * 1024 or not is_valid_image_upload(image):
                        flash("Please upload a valid PNG, JPG, JPEG or WEBP image under 5 MB.")
                    else:
                        filename = f"seller_{user_id}_{uuid.uuid4().hex}.{extension}"
                        save_path = os.path.join(
                            app.config["PROFILE_UPLOAD_FOLDER"],
                            filename
                        )
                        image.save(save_path)

                        connection = None
                        try:
                            from database import get_db_connection
                            connection = get_db_connection()
                            connection.execute(
                                "UPDATE users SET profile_picture = ? WHERE id = ? AND role = 'seller'",
                                (f"uploads/profile_pictures/{filename}", user_id)
                            )
                            connection.commit()
                            flash("Profile picture updated successfully.")
                        except Exception:
                            flash("Unable to save your profile picture. Please try again.")
                        finally:
                            if connection:
                                connection.close()

                return redirect(url_for("seller_account"))

            if action == "update_account":

                full_name = request.form.get(
                    "full_name", ""
                ).strip()

                email = request.form.get(
                    "email", ""
                ).strip()

                phone = request.form.get(
                    "phone", ""
                ).strip()

                success, message = update_user_account(
                    user_id,
                    full_name,
                    email,
                    phone
                )

                flash(message)

                if success:
                    session["user_name"] = full_name

            elif action == "change_password":

                current_password = request.form.get(
                    "current_password", ""
                )

                new_password = request.form.get(
                    "new_password", ""
                )

                confirm_password = request.form.get(
                    "confirm_password", ""
                )

                if new_password != confirm_password:
                    flash("New passwords do not match.")

                else:

                    success, message = change_user_password(
                        user_id,
                        current_password,
                        new_password
                    )

                    flash(message)

            else:
                flash("Invalid account request.")

            return redirect(url_for("seller_account"))

        user = get_user_account(user_id)

        if not user:
            session.clear()
            flash("Account not found. Please log in again.")
            return redirect(url_for("login"))

        return render_template(
            "seller_account.html",
            user=user,
            user_name=session.get("user_name")
        )

    # ==================================================
    # SELLER SALES
    # ==================================================

    @app.route("/seller/sales")
    def seller_sales():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can view sales.")
            return redirect(url_for("dashboard"))

        sales = get_seller_sales(session["user_id"])

        return render_template(
            "seller_sales.html",
            sales=sales,
            user_name=session.get("user_name")
        )

    # Individual seller order details
    @app.route("/seller/order/<int:order_id>")
    def seller_order_details_page(order_id):

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can view order details.")
            return redirect(url_for("dashboard"))

        seller_id = session["user_id"]

        order_details = get_seller_order_details(
            seller_id,
            order_id
        )

        if not order_details:
            flash(
                "Order not found or you are not authorized to view it."
            )
            return redirect(url_for("seller_sales"))

        return render_template(
            "seller_order_details.html",
            order=order_details["order"],
            items=order_details["items"],
            seller_total=order_details["seller_total"],
            user_name=session.get("user_name")
        )

    # ==================================================
    # MY PRODUCTS
    # ==================================================

    @app.route("/seller/products")
    def my_products():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can view this page.")
            return redirect(url_for("dashboard"))

        products = get_seller_products(session["user_id"])

        return render_template(
            "my_products.html",
            products=products,
            user_name=session.get("user_name")
        )

    # ==================================================
    # ADD PRODUCT
    # ==================================================

    @app.route("/seller/add-product", methods=["GET", "POST"])
    def add_product_page():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can upload products.")
            return redirect(url_for("dashboard"))

        categories = get_categories()

        if request.method == "POST":

            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            price_input = request.form.get("price", "").strip()
            category_input = request.form.get("category_id", "")
            stock_input = request.form.get("stock", "").strip()

            # NEW: Get the seller's product pickup location
            pickup_location = request.form.get(
                "pickup_location", ""
            ).strip()

            images = [
                image for image in request.files.getlist("images")
                if image and image.filename
            ]

            # NEW: Pickup location is now required
            if (
                not name
                or not price_input
                or not category_input
                or not stock_input
                or not pickup_location
            ):
                flash(
                    "Please complete all required fields, including pickup location."
                )
                return render_template(
                    "add_product.html",
                    categories=categories
                )

            try:
                price = float(price_input)
                category_id = int(category_input)
                stock = int(stock_input)

                if price <= 0 or stock < 1:
                    raise ValueError

                if category_id not in [
                    category["id"] for category in categories
                ]:
                    raise ValueError

            except ValueError:
                flash("Enter a valid price, category and stock quantity.")
                return render_template(
                    "add_product.html",
                    categories=categories
                )

            if not images:
                flash("Please select at least one product image.")
                return render_template(
                    "add_product.html",
                    categories=categories
                )

            if len(images) > MAX_PRODUCT_IMAGES:
                flash("You can upload a maximum of 5 product images.")
                return render_template(
                    "add_product.html",
                    categories=categories
                )

            valid_images = []

            for image in images:

                original_name = secure_filename(image.filename)

                image.stream.seek(0, os.SEEK_END)
                image_size = image.stream.tell()
                image.stream.seek(0)
                if image_size > 5 * 1024 * 1024 or not is_valid_image_upload(image):
                    flash(
                        "Only PNG, JPG, JPEG and WEBP images are allowed."
                    )
                    return render_template(
                        "add_product.html",
                        categories=categories
                    )

                extension = original_name.rsplit(".", 1)[1].lower()
                filename = f"{uuid.uuid4().hex}.{extension}"

                valid_images.append((image, filename))

            saved_images = []

            try:

                for image, filename in valid_images:

                    if os.environ.get("VERCEL"):
                        # Persist product photos in public Vercel Blob storage.
                        from vercel.blob import BlobClient

                        if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
                            raise RuntimeError(
                                "Product image storage is not configured. Please contact the marketplace administrator."
                            )

                        image.stream.seek(0)
                        content_type = "image/jpeg"
                        if filename.endswith(".png"):
                            content_type = "image/png"
                        elif filename.endswith(".webp"):
                            content_type = "image/webp"
                        blob = BlobClient().put(
                            f"products/{filename}",
                            image.stream.read(),
                            access="public",
                            add_random_suffix=True,
                            content_type=content_type,
                        )
                        saved_images.append(blob.url)
                    else:
                        image_path = os.path.join(
                            app.config["UPLOAD_FOLDER"],
                            filename
                        )
                        image.save(image_path)
                        saved_images.append(f"uploads/products/{filename}")

                # NEW: Pass pickup location to the product service
                success, message = add_product(
                    session["user_id"],
                    name,
                    description,
                    price,
                    category_id,
                    stock,
                    saved_images,
                    pickup_location
                )

                if success:
                    flash(message)
                    return redirect(url_for("seller_dashboard"))

                for saved_image in saved_images:
                    file_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        os.path.basename(saved_image)
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                flash(message)

            except Exception:
                current_app.logger.exception(
                    "Product image upload failed."
                )

                for saved_image in saved_images:
                    file_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        os.path.basename(saved_image)
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                flash("Something went wrong while uploading your product.")

        return render_template(
            "add_product.html",
            categories=categories
        )

    # ==================================================
    # EDIT PRODUCT
    # ==================================================

    @app.route(
        "/seller/edit-product/<int:product_id>",
        methods=["GET", "POST"]
    )
    def edit_product_page(product_id):

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can edit products.")
            return redirect(url_for("dashboard"))

        seller_id = session["user_id"]

        product = get_seller_product(product_id, seller_id)

        if not product:
            flash("Product not found or you are not authorized.")
            return redirect(url_for("my_products"))

        categories = get_categories()
        existing_images = get_product_images(product_id)

        image_paths = [
            image["image_path"] for image in existing_images
        ]

        if product["image"] and product["image"] not in image_paths:
            image_paths.insert(0, product["image"])

        if request.method == "POST":

            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            price_input = request.form.get("price", "").strip()
            category_input = request.form.get("category_id", "")
            stock_input = request.form.get("stock", "").strip()

            # NEW: Get the updated pickup location
            pickup_location = request.form.get(
                "pickup_location", ""
            ).strip()

            images = [
                image for image in request.files.getlist("images")
                if image and image.filename
            ]

            # NEW: Pickup location is now required when editing
            if (
                not name
                or not price_input
                or not category_input
                or not stock_input
                or not pickup_location
            ):
                flash(
                    "Please complete all required fields, including pickup location."
                )

                return render_template(
                    "edit_product.html",
                    product=product,
                    categories=categories,
                    images=image_paths
                )

            try:
                price = float(price_input)
                category_id = int(category_input)
                stock = int(stock_input)

                if price <= 0 or stock < 1:
                    raise ValueError

                if category_id not in [
                    category["id"] for category in categories
                ]:
                    raise ValueError

            except ValueError:
                flash("Enter a valid price, category and stock quantity.")

                return render_template(
                    "edit_product.html",
                    product=product,
                    categories=categories,
                    images=image_paths
                )

            if len(images) > MAX_PRODUCT_IMAGES:
                flash("You can upload a maximum of 5 images.")

                return render_template(
                    "edit_product.html",
                    product=product,
                    categories=categories,
                    images=image_paths
                )

            valid_images = []

            for image in images:

                original_name = secure_filename(image.filename)

                image.stream.seek(0, os.SEEK_END)
                image_size = image.stream.tell()
                image.stream.seek(0)
                if image_size > 5 * 1024 * 1024 or not is_valid_image_upload(image):
                    flash(
                        "Only PNG, JPG, JPEG and WEBP images are allowed."
                    )

                    return render_template(
                        "edit_product.html",
                        product=product,
                        categories=categories,
                        images=image_paths
                    )

                extension = original_name.rsplit(".", 1)[1].lower()
                filename = f"{uuid.uuid4().hex}.{extension}"

                valid_images.append((image, filename))

            saved_images = []

            try:

                for image, filename in valid_images:

                    image_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        filename
                    )

                    image.save(image_path)

                    saved_images.append(
                        f"uploads/products/{filename}"
                    )

                # NEW: Pass pickup location to the update service
                success, message = update_product(
                    product_id,
                    seller_id,
                    name,
                    description,
                    price,
                    category_id,
                    stock,
                    saved_images if saved_images else None,
                    pickup_location
                )

                if success:

                    if saved_images:

                        for old_image in set(image_paths):

                            old_file = os.path.join(
                                app.config["UPLOAD_FOLDER"],
                                os.path.basename(old_image)
                            )

                            if os.path.exists(old_file):
                                os.remove(old_file)

                    flash(message)
                    return redirect(url_for("my_products"))

                for saved_image in saved_images:

                    file_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        os.path.basename(saved_image)
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                flash(message)

            except Exception:

                current_app.logger.exception(
                    "Product editing failed."
                )

                for saved_image in saved_images:

                    file_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        os.path.basename(saved_image)
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                flash("Something went wrong while updating your product.")

        return render_template(
            "edit_product.html",
            product=product,
            categories=categories,
            images=image_paths
        )

    # ==================================================
    # DELETE PRODUCT
    # ==================================================

    @app.route(
        "/seller/delete-product/<int:product_id>",
        methods=["POST"]
    )
    def delete_product_page(product_id):

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        if session.get("user_role") != "seller":
            flash("Only sellers can delete products.")
            return redirect(url_for("dashboard"))

        seller_id = session["user_id"]

        product = get_seller_product(product_id, seller_id)

        if not product:
            flash("Product not found or you are not authorized.")
            return redirect(url_for("my_products"))

        existing_images = get_product_images(product_id)

        image_paths = [
            image["image_path"] for image in existing_images
        ]

        if product["image"] and product["image"] not in image_paths:
            image_paths.append(product["image"])

        success, message = delete_product(product_id, seller_id)

        if success:

            for image_path in set(image_paths):

                file_path = os.path.join(
                    app.config["UPLOAD_FOLDER"],
                    os.path.basename(image_path)
                )

                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)

                    except OSError:
                        current_app.logger.exception(
                            "Could not delete product image: %s",
                            file_path
                        )

            flash(message)

        else:
            flash(message)

        return redirect(url_for("my_products"))

    # ==================================================
    # SWITCH BETWEEN BUYER AND SELLER ACCOUNTS
    # ==================================================

    @app.route("/switch-account", methods=["GET", "POST"])
    def switch_account():

        if "user_id" not in session:
            flash("Please log in to continue.")
            return redirect(url_for("login"))

        current_role = session.get("user_role")

        if current_role not in ("buyer", "seller"):
            flash(
                "Account switching is only available "
                "for buyers and sellers."
            )
            return redirect(url_for("dashboard"))

        target_role = (
            "seller" if current_role == "buyer" else "buyer"
        )

        if request.method == "POST":

            password = request.form.get("password", "")

            current_user = get_user_account(session["user_id"])

            if not current_user:
                session.clear()
                flash("Account not found. Please log in again.")
                return redirect(url_for("login"))

            email = current_user["email"]

            target_account = login_user(
                email,
                password,
                target_role
            )

            if not target_account:
                flash(
                    f"Incorrect password or no {target_role} "
                    "account exists with this email."
                )
                return redirect(url_for("switch_account"))

            session["user_id"] = target_account["id"]
            session["user_name"] = target_account["full_name"]
            session["user_role"] = target_account["role"]

            flash(
                f"Switched to your {target_role.capitalize()} account!"
            )

            return redirect(url_for("dashboard"))

        return render_template(
            "switch_account.html",
            current_role=current_role,
            target_role=target_role
        )

    # ==================================================
    # FORGOT PASSWORD
    # ==================================================

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():

        if request.method == "POST":

            email = request.form.get("email", "").strip()
            role = request.form.get("role", "")

            if role not in ("buyer", "seller"):
                flash("Please select a valid account type.")
                return redirect(url_for("forgot_password"))

            token = create_password_reset(email, role)

            if token:

                try:
                    send_reset_email(email, token)

                except Exception:
                    current_app.logger.exception(
                        "Password reset email delivery failed."
                    )

            flash(
                "If an account matches those details, "
                "a password-reset email will be sent."
            )

            return redirect(url_for("forgot_password"))

        return render_template("forgot_password.html")

    # ==================================================
    # RESET PASSWORD
    # ==================================================

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password_page(token):

        if request.method == "POST":

            new_password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if new_password != confirm_password:

                flash("Passwords do not match.")

                return render_template(
                    "reset_password.html",
                    token=token
                )

            if len(new_password) < 4:

                flash("Password must contain at least 4 characters.")

                return render_template(
                    "reset_password.html",
                    token=token
                )

            success, message = reset_password(
                token,
                new_password
            )

            flash(message)

            if success:
                return redirect(url_for("login"))

            return render_template(
                "reset_password.html",
                token=token
            )

        return render_template(
            "reset_password.html",
            token=token
        )

    # ==================================================
    # BUYER ACCOUNT MANAGEMENT
    # ==================================================
    @app.route("/buyer/account", methods=["GET", "POST"])
    def buyer_account():
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Only buyers can manage this account.")
            return redirect(url_for("dashboard"))
        user_id = session["user_id"]
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "update_account":
                full_name = request.form.get("full_name", "").strip()
                email = request.form.get("email", "").strip()
                phone = request.form.get("phone", "").strip()
                success, message = update_user_account(user_id, full_name, email, phone)
                if success:
                    session["user_name"] = full_name
            elif action == "change_password":
                new_password = request.form.get("new_password", "")
                if new_password != request.form.get("confirm_password", ""):
                    success, message = False, "New passwords do not match."
                else:
                    success, message = change_user_password(
                        user_id, request.form.get("current_password", ""), new_password
                    )
            else:
                success, message = False, "Invalid account request."
            flash(message)
            return redirect(url_for("buyer_account"))
        user = get_user_account(user_id)
        if not user:
            session.clear()
            return redirect(url_for("login"))
        return render_template("buyer_account.html", user=user, user_name=session.get("user_name"))

    # ==================================================
    # HELP, COMPLAINTS AND ORDER CANCELLATION REQUESTS
    # ==================================================
    @app.route("/support", methods=["GET", "POST"])
    def support_center():
        if "user_id" not in session or session.get("user_role") not in ("buyer", "seller"):
            flash("Please log in with a buyer or seller account.")
            return redirect(url_for("login"))
        connection = get_db_connection()
        try:
            if request.method == "POST":
                request_type = request.form.get("request_type", "support")
                subject = request.form.get("subject", "").strip()[:120]
                message = request.form.get("message", "").strip()[:3000]
                order_number = request.form.get("order_number", "").strip()[:40]
                if request_type not in ("support", "complaint", "cancellation", "refund") or not subject or not message or (request_type in ("cancellation", "refund") and not order_number):
                    flash("Complete the request form. Cancellation requests must include the order number.")
                else:
                    request_cursor = connection.execute("""INSERT INTO support_requests
                        (user_id,user_role,request_type,subject,message,order_number) VALUES (?,?,?,?,?,?)""",
                        (session["user_id"], session["user_role"], request_type, subject, message, order_number or None))
                    connection.execute("""INSERT INTO notifications(audience_role,title,message)
                        VALUES('admin','New support request',?)""",
                        (f"{request_type.title()} request #{request_cursor.lastrowid}: {subject}",))
                    connection.commit()
                    flash("Your request has been sent to the marketplace admin.")
                return redirect(url_for("support_center"))
            requests_list = connection.execute("""SELECT * FROM support_requests
                WHERE user_id=? ORDER BY created_at DESC,id DESC""", (session["user_id"],)).fetchall()
            return render_template("support_center.html", requests=requests_list)
        finally:
            connection.close()

    @app.route("/admin/requests", methods=["GET", "POST"])
    def admin_requests():
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only the admin can manage support requests.")
            return redirect(url_for("login"))
        connection = get_db_connection()
        try:
            if request.method == "POST":
                try:
                    request_id = int(request.form.get("request_id", ""))
                except ValueError:
                    request_id = 0
                status = request.form.get("status", "open")
                response = request.form.get("admin_response", "").strip()[:3000]
                if status not in ("open", "in_review", "resolved", "rejected"):
                    flash("Invalid request status.")
                else:
                    request_row = connection.execute("SELECT * FROM support_requests WHERE id=?", (request_id,)).fetchone()
                    if not request_row:
                        flash("Support request not found.")
                    else:
                        if request_row["request_type"] == "refund" and status == "resolved" and request_row["order_number"]:
                            refund_order = connection.execute("SELECT id,buyer_id,total_amount,status FROM orders WHERE order_number=? AND buyer_id=?", (request_row["order_number"], request_row["user_id"])).fetchone()
                            if not refund_order:
                                flash("Refund order not found for this buyer.")
                                status = "in_review"
                            else:
                                # The admin records the refund only after manually returning the money.
                                connection.execute("UPDATE payments SET status='refunded' WHERE order_id=?", (refund_order["id"],))
                                connection.execute("UPDATE orders SET status='refunded' WHERE id=?", (refund_order["id"],))
                                connection.execute("INSERT INTO refund_records(order_id,request_id,amount,status,admin_note) VALUES(?,?,?,'refunded',?)", (refund_order["id"], request_id, refund_order["total_amount"], response))
                                connection.execute("INSERT INTO notifications(user_id,order_id,title,message) VALUES(?,?,?,?)", (refund_order["buyer_id"], refund_order["id"], "Refund recorded", "The admin has recorded your refund as completed."))
                        if request_row["request_type"] == "cancellation" and status == "resolved" and request_row["order_number"]:
                            order = connection.execute("""SELECT o.id,o.buyer_id,o.status,
                                COALESCE((SELECT d.status FROM deliveries d WHERE d.order_id=o.id ORDER BY d.id DESC LIMIT 1),'pending') AS delivery_status
                                FROM orders o WHERE o.order_number=?""", (request_row["order_number"],)).fetchone()
                            if order and str(order["status"]).lower() == "processing" and str(order["delivery_status"]).lower() == "pending":
                                connection.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order["id"],))
                                order_items = connection.execute("SELECT product_id,quantity FROM order_items WHERE order_id=?", (order["id"],)).fetchall()
                                for order_item in order_items:
                                    connection.execute("UPDATE products SET stock=stock+? WHERE id=?", (order_item["quantity"], order_item["product_id"]))
                                connection.execute("UPDATE payments SET status='cancelled' WHERE order_id=?", (order["id"],))
                                connection.execute("UPDATE deliveries SET status='cancelled' WHERE order_id=?", (order["id"],))
                                connection.execute("UPDATE commissions SET status='cancelled' WHERE order_id=?", (order["id"],))
                                connection.execute("""INSERT INTO notifications(user_id,order_id,title,message) VALUES(?,?,?,?)""",
                                    (order["buyer_id"], order["id"], "Order cancelled", f"Your order {request_row['order_number']} was cancelled by the marketplace admin."))
                            elif order:
                                flash("This order has progressed beyond the cancellation stage. Contact the buyer to arrange the next steps manually.")
                        connection.execute("""UPDATE support_requests SET status=?,admin_response=?,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?""", (status, response, request_id))
                        connection.execute("""INSERT INTO notifications(user_id,title,message)
                            VALUES(?,?,?)""", (request_row["user_id"], "Support request updated",
                            f"Your request '{request_row['subject']}' is now {status.replace('_',' ')}. {response}"))
                        connection.execute("""INSERT INTO admin_activity_log(admin_id,action,details)
                            VALUES(?,?,?)""", (session["user_id"], "Updated support request", f"Request #{request_id}: {status}"))
                        connection.commit()
                        flash("Request updated.")
                return redirect(url_for("admin_requests"))
            requests_list = connection.execute("""SELECT r.*,u.full_name,u.email FROM support_requests r
                JOIN users u ON u.id=r.user_id ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END, r.created_at DESC""").fetchall()
            return render_template("admin_requests.html", requests=requests_list)
        finally:
            connection.close()

    # ==================================================
    # VERIFIED-PURCHASE PRODUCT REVIEWS
    # ==================================================
    @app.route("/buyer/review/<int:product_id>", methods=["GET", "POST"])
    def buyer_review(product_id):
        if "user_id" not in session or session.get("user_role") != "buyer":
            flash("Only buyers can review products.")
            return redirect(url_for("login"))
        connection = get_db_connection()
        try:
            eligible = connection.execute("""SELECT o.id FROM orders o JOIN order_items oi ON oi.order_id=o.id
                WHERE o.buyer_id=? AND oi.product_id=? AND LOWER(o.status) IN ('completed','delivered')
                ORDER BY o.created_at DESC LIMIT 1""", (session["user_id"], product_id)).fetchone()
            if not eligible:
                flash("You can review a product after its order is completed.")
                return redirect(url_for("buyer_dashboard"))
            product = connection.execute("SELECT id,name FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                flash("Product not found.")
                return redirect(url_for("buyer_dashboard"))
            if request.method == "POST":
                try:
                    rating = int(request.form.get("rating", ""))
                except ValueError:
                    rating = 0
                review = request.form.get("review", "").strip()[:1500]
                if rating not in range(1,6) or not review:
                    flash("Choose a rating and enter a review.")
                else:
                    connection.execute("""INSERT INTO product_reviews(product_id,buyer_id,order_id,rating,review)
                        VALUES(?,?,?,?,?) ON CONFLICT(product_id,buyer_id) DO UPDATE SET
                        order_id=excluded.order_id,rating=excluded.rating,review=excluded.review,created_at=CURRENT_TIMESTAMP""",
                        (product_id, session["user_id"], eligible["id"], rating, review))
                    connection.commit()
                    flash("Thank you. Your product review has been saved.")
                    return redirect(url_for("buyer_dashboard"))
            return render_template("buyer_review.html", product=product)
        finally:
            connection.close()

    # ==================================================
    # ADMIN REPORTS AND ANALYTICS
    # ==================================================
    @app.route("/admin/reports")
    def admin_reports():
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only the admin can view reports.")
            return redirect(url_for("login"))
        connection = get_db_connection()
        try:
            metrics = {
                "Registered users": connection.execute("SELECT COUNT(*) FROM users WHERE role IN ('buyer','seller') AND COALESCE(is_deleted,0)=0").fetchone()[0],
                "Active products": connection.execute("SELECT COUNT(*) FROM products WHERE LOWER(COALESCE(status,'active'))='active' AND stock>0").fetchone()[0],
                "Total orders": connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "Total sales": "K" + format(connection.execute("SELECT COALESCE(SUM(total_amount),0) FROM orders WHERE LOWER(status) IN ('completed','delivered')").fetchone()[0], ".2f"),
                "Completed orders": connection.execute("SELECT COUNT(*) FROM orders WHERE LOWER(status) IN ('completed','delivered')").fetchone()[0],
                "Pending orders": connection.execute("SELECT COUNT(*) FROM orders WHERE LOWER(status) NOT IN ('completed','delivered','cancelled')").fetchone()[0],
                "Commission recorded": "K" + format(connection.execute("SELECT COALESCE(SUM(amount),0) FROM commissions WHERE status != 'cancelled'").fetchone()[0], ".2f"),
                "Pending seller payouts": "K" + format(connection.execute("SELECT COALESCE(SUM(amount),0) FROM seller_payouts WHERE status='pending'").fetchone()[0], ".2f"),
                "Open support requests": connection.execute("SELECT COUNT(*) FROM support_requests WHERE status IN ('open','in_review')").fetchone()[0],
                "Product reviews": connection.execute("SELECT COUNT(*) FROM product_reviews").fetchone()[0]
            }
            reviews = connection.execute("""SELECT r.*,p.name AS product_name,u.full_name AS buyer_name
                FROM product_reviews r JOIN products p ON p.id=r.product_id JOIN users u ON u.id=r.buyer_id
                ORDER BY r.created_at DESC LIMIT 20""").fetchall()
            low_stock = connection.execute("SELECT name,stock FROM products WHERE stock<=5 AND LOWER(COALESCE(status,'active'))='active' ORDER BY stock,name LIMIT 30").fetchall()
            return render_template("admin_reports.html", metrics=metrics, reviews=reviews, low_stock=low_stock)
        finally:
            connection.close()

    # ==================================================
    # SELLER VERIFICATION, PRODUCT APPROVAL, PROMOTIONS, PERFORMANCE
    # ==================================================
    @app.route("/seller/verification", methods=["GET", "POST"])
    def seller_verification():
        if "user_id" not in session or session.get("user_role") != "seller":
            flash("Only sellers can submit verification details.")
            return redirect(url_for("login"))
        db = get_db_connection()
        try:
            if request.method == "POST":
                details = request.form.get("details", "").strip()[:1500]
                if len(details) < 10:
                    flash("Please provide enough information for verification.")
                else:
                    db.execute("UPDATE users SET seller_verification_status='pending', seller_verified=0 WHERE id=?", (session["user_id"],))
                    db.execute("INSERT INTO support_requests(user_id,user_role,request_type,subject,message) VALUES(?,'seller','verification','Seller verification request',?)", (session["user_id"], details))
                    db.execute("INSERT INTO notifications(audience_role,title,message) VALUES('admin','Seller verification requested',?)", (f"Seller account #{session['user_id']} submitted verification details.",))
                    db.commit(); flash("Verification request sent to the admin.")
                    return redirect(url_for("seller_dashboard"))
            user=db.execute("SELECT seller_verified,seller_verification_status FROM users WHERE id=?", (session["user_id"],)).fetchone()
            return render_template("seller_verification.html", user=user)
        finally: db.close()

    @app.route("/admin/marketplace-controls", methods=["GET", "POST"])
    def admin_marketplace_controls():
        if "user_id" not in session or session.get("user_role") != "admin":
            flash("Only the admin can manage marketplace approvals."); return redirect(url_for("login"))
        db=get_db_connection()
        try:
            if request.method == "POST":
                action=request.form.get("action")
                note=request.form.get("note", "").strip()[:500]
                try: target_id=int(request.form.get("target_id", "0"))
                except ValueError: target_id=0
                if action in ("close_marketplace", "open_marketplace"):
                    new_value = "0" if action == "close_marketplace" else "1"
                    db.execute("UPDATE marketplace_settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='is_open'", (new_value,))
                    db.execute("INSERT INTO admin_activity_log(admin_id,action,details) VALUES(?,?,?)", (session["user_id"], "Marketplace availability", "Closed marketplace" if new_value == "0" else "Opened marketplace"))
                    db.commit()
                    flash("Marketplace closed. Only administrators can access it." if new_value == "0" else "Marketplace is open to everyone again.")
                    return redirect(url_for("admin_marketplace_controls"))
                if action in ("close_marketplace", "open_marketplace"):
                    new_value = "0" if action == "close_marketplace" else "1"
                    db.execute("UPDATE marketplace_settings SET setting_value=?, updated_at=CURRENT_TIMESTAMP WHERE setting_key='is_open'", (new_value,))
                    db.execute("INSERT INTO admin_activity_log(admin_id,action,details) VALUES(?,?,?)", (session["user_id"], "Marketplace availability", "Closed marketplace" if new_value == "0" else "Opened marketplace"))
                    db.commit()
                    flash("Marketplace closed. Only administrators can access it." if new_value == "0" else "Marketplace is open to everyone again.")
                    return redirect(url_for("admin_marketplace_controls"))
                if action in ("verify_seller", "reject_seller"):
                    verified = 1 if action == "verify_seller" else 0
                    state = "verified" if verified else "rejected"
                    db.execute("UPDATE users SET seller_verified=?,seller_verification_status=? WHERE id=? AND role='seller'", (verified,state,target_id))
                    db.execute("INSERT INTO admin_activity_log(admin_id,action,details) VALUES(?,?,?)", (session["user_id"], "Seller verification", f"Seller #{target_id}: {state}. {note}"))
                    db.execute("INSERT INTO notifications(user_id,title,message) VALUES(?,?,?)", (target_id,"Seller verification updated",f"Your seller verification is {state}. {note}"))
                elif action in ("approve_product", "reject_product"):
                    state="approved" if action=="approve_product" else "rejected"
                    db.execute("UPDATE products SET approval_status=?,approval_note=? WHERE id=?", (state,note,target_id))
                    row=db.execute("SELECT seller_id,name FROM products WHERE id=?", (target_id,)).fetchone()
                    if row: db.execute("INSERT INTO notifications(user_id,title,message) VALUES(?,?,?)", (row["seller_id"],"Product review updated",f"Your listing '{row['name']}' was {state}. {note}"))
                    db.execute("INSERT INTO admin_activity_log(admin_id,action,details) VALUES(?,?,?)", (session["user_id"], "Product moderation", f"Product #{target_id}: {state}. {note}"))
                elif action == "create_promotion":
                    code=request.form.get("code", "").strip().upper()
                    dtype=request.form.get("discount_type", "percent")
                    try:
                        value=float(request.form.get("discount_value", "0")); minimum=float(request.form.get("minimum_order", "0")); limit_raw=request.form.get("usage_limit", "").strip(); limit=int(limit_raw) if limit_raw else None
                        if not code or len(code)>30 or dtype not in ("percent","fixed") or value<=0 or minimum<0 or (dtype=="percent" and value>100) or (limit is not None and limit<1): raise ValueError
                        db.execute("INSERT INTO promotion_codes(code,discount_type,discount_value,minimum_order,usage_limit) VALUES(?,?,?,?,?)", (code,dtype,value,minimum,limit))
                        db.execute("INSERT INTO admin_activity_log(admin_id,action,details) VALUES(?,?,?)", (session["user_id"],"Created promotion",code))
                        flash("Promotion code created.")
                    except Exception:
                        flash("Could not create promotion. Check the code, discount and usage limit; codes must be unique.")
                elif action == "toggle_promotion":
                    db.execute("UPDATE promotion_codes SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (target_id,))
                db.commit()
                if action != "create_promotion": flash("Marketplace settings updated.")
                return redirect(url_for("admin_marketplace_controls"))
            pending_sellers=db.execute("SELECT id,full_name,email,seller_verification_status FROM users WHERE role='seller' AND seller_verified=0 AND seller_verification_status='pending' ORDER BY id").fetchall()
            pending_products=db.execute("SELECT p.id,p.name,p.price,p.created_at,u.full_name AS seller_name FROM products p JOIN users u ON u.id=p.seller_id WHERE p.approval_status='pending' ORDER BY p.created_at").fetchall()
            promotions=db.execute("SELECT * FROM promotion_codes ORDER BY created_at DESC").fetchall()
            market_row=db.execute("SELECT setting_value FROM marketplace_settings WHERE setting_key='is_open'").fetchone()
            marketplace_is_open=not market_row or market_row["setting_value"] == "1"
            return render_template("admin_marketplace_controls.html", sellers=pending_sellers, products=pending_products, promotions=promotions, marketplace_is_open=marketplace_is_open)
        finally: db.close()

    @app.route("/seller/performance")
    def seller_performance():
        if "user_id" not in session or session.get("user_role") != "seller":
            flash("Only sellers can view performance."); return redirect(url_for("login"))
        db=get_db_connection()
        try:
            sid=session["user_id"]
            totals=db.execute("""SELECT COUNT(DISTINCT o.id) AS orders, COALESCE(SUM(oi.quantity),0) AS units,
                COALESCE(SUM(oi.quantity*oi.unit_price),0) AS gross
                FROM order_items oi JOIN orders o ON o.id=oi.order_id
                WHERE oi.seller_id=? AND LOWER(o.status) IN ('completed','delivered')""", (sid,)).fetchone()
            top=db.execute("""SELECT p.id,p.name,COALESCE(SUM(oi.quantity),0) AS units,COALESCE(SUM(oi.quantity*oi.unit_price),0) AS revenue
                FROM products p LEFT JOIN order_items oi ON oi.product_id=p.id AND oi.seller_id=?
                LEFT JOIN orders o ON o.id=oi.order_id AND LOWER(o.status) IN ('completed','delivered')
                WHERE p.seller_id=? GROUP BY p.id ORDER BY units DESC,revenue DESC LIMIT 20""", (sid,sid)).fetchall()
            ratings=db.execute("SELECT COUNT(*) AS reviews,ROUND(AVG(r.rating),1) AS average FROM product_reviews r JOIN products p ON p.id=r.product_id WHERE p.seller_id=?", (sid,)).fetchone()
            stock=db.execute("SELECT COUNT(*) AS listings,COALESCE(SUM(stock),0) AS units, SUM(CASE WHEN stock<=5 THEN 1 ELSE 0 END) AS low_stock FROM products WHERE seller_id=?", (sid,)).fetchone()
            return render_template("seller_performance.html", totals=totals, top=top, ratings=ratings, stock=stock)
        finally: db.close()

    # ==================================================
    # LOGOUT
    # ==================================================

    @app.route("/account/delete", methods=["POST"])
    def delete_account():
        """Deactivate and anonymize a buyer/seller account while retaining financial records."""
        if "user_id" not in session or session.get("user_role") not in ("buyer", "seller"):
            flash("Please sign in with a buyer or seller account.")
            return redirect(url_for("login"))

        user_id = session["user_id"]
        role = session["user_role"]
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "").strip()
        if confirmation != "DELETE" or not password:
            flash("Enter your password and type DELETE to confirm account deletion.")
            return redirect(url_for("buyer_dashboard" if role == "buyer" else "seller_dashboard"))

        connection = get_db_connection()
        try:
            user = connection.execute(
                "SELECT password_hash FROM users WHERE id = ? AND role = ? AND COALESCE(is_deleted, 0) = 0",
                (user_id, role)
            ).fetchone()
            if not user or not check_password_hash(user["password_hash"], password):
                flash("The password is incorrect. Your account has not been deleted.")
                return redirect(url_for("buyer_dashboard" if role == "buyer" else "seller_dashboard"))

            # Keep orders, payments, commissions and audit history for accounting;
            # anonymize the account and disable future access instead of breaking foreign keys.
            connection.execute("DELETE FROM cart_items WHERE buyer_id = ?", (user_id,))
            for table in ("watchlist",):
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if exists:
                    connection.execute(f"DELETE FROM {table} WHERE buyer_id = ?", (user_id,))
            if role == "seller":
                connection.execute("UPDATE products SET status = 'inactive' WHERE seller_id = ?", (user_id,))
            connection.execute("""UPDATE users
                SET full_name = 'Deleted account', email = ?, phone = NULL,
                    password_hash = ?, profile_picture = NULL, is_deleted = 1
                WHERE id = ? AND role = ?""",
                (f"deleted-{user_id}-{uuid.uuid4().hex}@deleted.invalid", uuid.uuid4().hex, user_id, role))
            connection.commit()
        except Exception:
            connection.rollback()
            flash("We could not delete your account. Please try again or contact support.")
            return redirect(url_for("buyer_dashboard" if role == "buyer" else "seller_dashboard"))
        finally:
            connection.close()

        session.clear()
        flash("Your account has been deleted and your personal account details anonymized. Historical order and payment records are retained for accounting.")
        return redirect(url_for("home"))

    @app.route("/logout")
    def logout():
        # Read the marketplace status before clearing the session so that
        # logout during a closure returns everyone to the login page.
        db = get_db_connection()
        try:
            row = db.execute(
                "SELECT setting_value FROM marketplace_settings "
                "WHERE setting_key='is_open'"
            ).fetchone()
            marketplace_is_open = not row or row["setting_value"] == "1"
        finally:
            db.close()

        session.clear()
        flash("You have been logged out.")

        if not marketplace_is_open:
            return redirect(url_for("login"))
        return redirect(url_for("home"))

    return app


# Create Flask application
app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
