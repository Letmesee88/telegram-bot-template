from __future__ import annotations
import sys

from alembic.config import main as alembic_main

if __name__ == "__main__":
    # Force Alembic to use our ini and upgrade to head
    sys.argv = [
        "alembic",
        "-c",
        "alembic.ini",
        "upgrade",
        "head",
    ]
    alembic_main()
