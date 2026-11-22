import os


class Config:

    # Project directories
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

    # Database settings. Local development uses SQLite; Vercel uses Neon.
    DATABASE_PATH = os.path.join(INSTANCE_DIR, "marketplace.db")
    DATABASE_URL = os.environ.get("DATABASE_URL")

    # Flask security
    SECRET_KEY = os.environ.get(
        "SECRET_KEY",
        "mu_marketplace_development_key"
    )

    # Debug mode
    DEBUG = True

    # Gmail SMTP settings
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False

    # Gmail account details
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = MAIL_USERNAME