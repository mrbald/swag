"""Shared SQLite bank mock, used only through its authenticated HTTP service."""
from .service import Bank, BankError

__all__ = ["Bank", "BankError"]
