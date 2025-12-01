from __future__ import annotations
import os

from dotenv import load_dotenv

load_dotenv()

ADMIN_HOST: str = os.getenv("ADMIN_HOST") or "localhost"
ADMIN_PORT: int = int(os.getenv("ADMIN_PORT") or 5000)
DEFAULT_ADMIN_EMAIL: str = os.getenv("DEFAULT_ADMIN_EMAIL") or "admin@example.com"
DEFAULT_ADMIN_PASSWORD: str = os.getenv("DEFAULT_ADMIN_PASSWORD") or "admin"

DEBUG: bool = str(os.getenv("DEBUG")).lower() == "true"

BABEL_DEFAULT_LOCALE = os.getenv("BABEL_DEFAULT_LOCALE") or "en"

# Require strong secret key from environment
SECRET_KEY: str = os.environ["SECRET_KEY"]


# SQLAlchemy config
def database_url() -> str:
    db_host: str = os.getenv("DB_HOST") or "localhost"
    db_port: int = int(os.getenv("DB_PORT") or 5432)
    db_user: str = os.getenv("DB_USER") or "postgres"
    db_pass: str | None = os.getenv("DB_PASS")
    db_name: str = os.getenv("DB_NAME") or "postgres"

    if db_pass:
        return f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
    return f"postgresql://{db_user}@{db_host}:{db_port}/{db_name}"


SQLALCHEMY_DATABASE_URI: str = database_url()
SQLALCHEMY_ECHO = False
SQLALCHEMY_TRACK_MODIFICATIONS = False

# Flask-Security config
SECURITY_URL_PREFIX = "/admin"
SECURITY_PASSWORD_HASH: str = os.getenv("SECURITY_PASSWORD_HASH") or "pbkdf2_sha512"
SECURITY_PASSWORD_SALT: str = os.environ["SECURITY_PASSWORD_SALT"]

# Flask-Security URLs, overridden because they don't put a / at the end
SECURITY_LOGIN_URL = "/login/"
SECURITY_LOGOUT_URL = "/logout/"
SECURITY_REGISTER_URL = "/register/"

SECURITY_POST_LOGIN_VIEW = "/admin/"
SECURITY_POST_LOGOUT_VIEW = "/admin/"
SECURITY_POST_REGISTER_VIEW = "/admin/"

# Flask-Security features
SECURITY_REGISTERABLE = False
SECURITY_SEND_REGISTER_EMAIL = False

# Cache config
CACHE_TYPE = "SimpleCache"
CACHE_DEFAULT_TIMEOUT = 300
