"""Canonical bank domain: persistence and CRUD/UI metadata live together."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class ScaledDecimal(TypeDecorator):
    """SQLite has no exact decimal type; persist the minor units as an integer."""

    impl = BigInteger
    cache_ok = True

    def __init__(self, scale: int):
        super().__init__()
        self.scale = scale

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> int | None:
        if value is None:
            return None
        scaled = Decimal(value) * (10 ** self.scale)
        if scaled != scaled.to_integral_value():
            raise ValueError("decimal precision exceeds field scale")
        integer = int(scaled)
        if not -(2 ** 63) <= integer < 2 ** 63:
            raise ValueError("decimal exceeds SQLite integer range")
        return integer

    def process_result_value(self, value: int | None, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(value).scaleb(-self.scale)


class Base(DeclarativeBase):
    pass


def field(db_type: Any, label: str, kind: str = "string", *, nullable: bool = False,
          editable: bool = True, choices: tuple[str, ...] | None = None,
          reference: str | None = None, scale: int | None = None,
          default: Any = None, minimum: int | None = None, maximum: int | None = None,
          exclusive_minimum: bool = False,
          foreign_key: str | None = None, unique: bool = False) -> Any:
    ui = {"label": label, "type": kind, "editable": editable, "nullable": nullable}
    for key, value in (("choices", choices), ("reference", reference), ("scale", scale),
                       ("default", default), ("minimum", minimum), ("maximum", maximum)):
        if value is not None:
            ui[key] = list(value) if key == "choices" else value
    if exclusive_minimum:
        ui["exclusive_minimum"] = True
    args = [db_type]
    if foreign_key:
        args.append(ForeignKey(foreign_key, ondelete="RESTRICT"))
    return mapped_column(*args, nullable=nullable, unique=unique, info={"ui": ui})


class Record:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, info={"ui": {
        "label": "ID", "type": "integer", "editable": False, "nullable": False}})
    version: Mapped[int] = field(Integer, "Version", "integer", editable=False)
    created_by: Mapped[str] = field(String(48), "Created by", editable=False)
    updated_by: Mapped[str] = field(String(48), "Updated by", editable=False)
    created_at: Mapped[str] = field(String(32), "Created at", editable=False)
    updated_at: Mapped[str] = field(String(32), "Updated at", editable=False)


CURRENCIES = ("CHF", "EUR", "USD", "GBP")


class Portfolio(Record, Base):
    __tablename__ = "portfolios"
    __label__ = "Portfolios"
    __title_field__ = "name"
    name: Mapped[str] = field(String(120), "Name")
    base_currency: Mapped[str] = field(String(3), "Base currency", "enum", choices=CURRENCIES, default="CHF")
    status: Mapped[str] = field(String(16), "Status", "enum", choices=("active", "closed"), default="active")


class Mandate(Record, Base):
    __tablename__ = "mandates"
    __label__ = "Mandates"
    __title_field__ = "name"
    portfolio_id: Mapped[int] = field(Integer, "Portfolio", "reference", reference="portfolios", foreign_key="portfolios.id")
    name: Mapped[str] = field(String(120), "Name")
    strategy: Mapped[str] = field(String(32), "Strategy", "enum", choices=("balanced", "growth", "income", "capital_preservation"), default="balanced")
    risk_level: Mapped[int] = field(Integer, "Risk level", "integer", minimum=1, maximum=5, default=3)
    status: Mapped[str] = field(String(16), "Status", "enum", choices=("active", "suspended", "closed"), default="active")


class Instrument(Record, Base):
    __tablename__ = "instruments"
    __label__ = "Instruments"
    __title_field__ = "name"
    symbol: Mapped[str] = field(String(32), "Symbol", unique=True)
    name: Mapped[str] = field(String(120), "Name")
    asset_class: Mapped[str] = field(String(16), "Asset class", "enum", choices=("equity", "bond", "fund", "cash"), default="equity")
    currency: Mapped[str] = field(String(3), "Currency", "enum", choices=CURRENCIES, default="CHF")
    isin: Mapped[str | None] = field(String(12), "ISIN", nullable=True)


class Position(Record, Base):
    __tablename__ = "positions"
    __label__ = "Positions"
    __title_field__ = "id"
    __table_args__ = (UniqueConstraint("portfolio_id", "instrument_id"),)
    portfolio_id: Mapped[int] = field(Integer, "Portfolio", "reference", reference="portfolios", foreign_key="portfolios.id")
    instrument_id: Mapped[int] = field(Integer, "Instrument", "reference", reference="instruments", foreign_key="instruments.id")
    quantity: Mapped[Decimal] = field(ScaledDecimal(6), "Quantity", "decimal", scale=6, minimum=0)
    average_cost: Mapped[Decimal] = field(ScaledDecimal(4), "Average cost", "decimal", scale=4, minimum=0)


class CashBalance(Record, Base):
    __tablename__ = "cash_balances"
    __label__ = "Cash balances"
    __title_field__ = "id"
    __table_args__ = (UniqueConstraint("portfolio_id", "currency"),)
    portfolio_id: Mapped[int] = field(Integer, "Portfolio", "reference", reference="portfolios", foreign_key="portfolios.id")
    currency: Mapped[str] = field(String(3), "Currency", "enum", choices=CURRENCIES, default="CHF")
    amount: Mapped[Decimal] = field(ScaledDecimal(2), "Amount", "decimal", scale=2)


class Price(Record, Base):
    __tablename__ = "prices"
    __label__ = "Prices"
    __title_field__ = "id"
    __table_args__ = (UniqueConstraint("instrument_id", "as_of", "currency"),)
    instrument_id: Mapped[int] = field(Integer, "Instrument", "reference", reference="instruments", foreign_key="instruments.id")
    as_of: Mapped[Any] = field(Date, "As of", "date")
    price: Mapped[Decimal] = field(ScaledDecimal(4), "Price", "decimal", scale=4, minimum=0)
    currency: Mapped[str] = field(String(3), "Currency", "enum", choices=CURRENCIES, default="CHF")


class FxRate(Record, Base):
    __tablename__ = "fx_rates"
    __label__ = "FX rates"
    __title_field__ = "id"
    __table_args__ = (UniqueConstraint("base_currency", "quote_currency", "as_of"),
                      CheckConstraint("base_currency <> quote_currency", name="different_currencies"))
    base_currency: Mapped[str] = field(String(3), "Base currency", "enum", choices=CURRENCIES, default="EUR")
    quote_currency: Mapped[str] = field(String(3), "Quote currency", "enum", choices=CURRENCIES, default="CHF")
    as_of: Mapped[Any] = field(Date, "As of", "date")
    rate: Mapped[Decimal] = field(ScaledDecimal(8), "Rate", "decimal", scale=8, minimum=0, exclusive_minimum=True)


class PortfolioPermission(Base):
    """Trusted provisioning only; deliberately absent from the public CRUD registry."""
    __tablename__ = "portfolio_permissions"
    user: Mapped[str] = mapped_column(String(48), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True)
    can_write: Mapped[bool] = mapped_column(Boolean, nullable=False)


MODELS = (Portfolio, Mandate, Instrument, Position, CashBalance, Price, FxRate)
ENTITIES = {model.__tablename__: model for model in MODELS}

# The same field metadata also installs database checks, so bypassing HTTP cannot
# introduce values forbidden by the domain. SQLite foreign keys are enabled by DB.
for model in MODELS:
    for column in model.__table__.columns:
        ui = column.info["ui"]
        if "choices" in ui:
            model.__table__.append_constraint(CheckConstraint(column.in_(ui["choices"])))
        if "minimum" in ui:
            minimum_check = column > ui["minimum"] if ui.get("exclusive_minimum") else column >= ui["minimum"]
            model.__table__.append_constraint(CheckConstraint(minimum_check))
        if "maximum" in ui:
            model.__table__.append_constraint(CheckConstraint(column <= ui["maximum"]))
        if isinstance(column.type, String) and ui["editable"]:
            from sqlalchemy import func
            model.__table__.append_constraint(CheckConstraint(func.length(column) <= column.type.length))
