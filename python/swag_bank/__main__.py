"""Run with PYTHONPATH=python python -m swag_bank."""
import logging
import os

from aiohttp import web

from .http import create_app
from .service import Bank


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("SWAG_SERVICE_TOKEN", "")
    if not token:
        raise SystemExit("SWAG_SERVICE_TOKEN must be configured")
    bank = Bank(os.environ.get("SWAG_DB_PATH", "/data/bank.sqlite3"))
    web.run_app(create_app(bank, token), host="0.0.0.0", port=8081, access_log=None)


if __name__ == "__main__":
    main()
