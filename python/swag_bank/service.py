"""Small generic CRUD layer with entitlement and optimistic concurrency checks."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from sqlalchemy import String, cast, create_engine, delete, event, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .models import (Base, ENTITIES, MODELS, CashBalance, FxRate, Instrument, Mandate,
                     Portfolio, PortfolioPermission, Position, Price)


class BankError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.fields = fields or {}

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "fields": self.fields}}


def invalid(message: str, fields: dict[str, str] | None = None) -> BankError:
    return BankError(422, "validation", message, fields)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def serialize(record: Any) -> dict[str, Any]:
    result = {}
    for column in record.__table__.columns:
        value = getattr(record, column.name)
        ui = column.info["ui"]
        if value is not None and ui["type"] == "decimal":
            value = format(value, f'.{ui["scale"]}f')
        elif isinstance(value, date):
            value = value.isoformat()
        result[column.name] = value
    return result


class Bank:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine("sqlite:///" + str(path.absolute()),
                                    connect_args={"check_same_thread": False, "timeout": 5})

        @event.listens_for(self.engine, "connect")
        def configure(connection: Any, record: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")

        Base.metadata.create_all(self.engine)
        self._seed()

    def close(self) -> None:
        self.engine.dispose()

    def _seed(self) -> None:
        with Session(self.engine) as session, session.begin():
            # A restart never reseeds a nonempty bank or overwrites existing data.
            if any(session.scalar(select(func.count()).select_from(model)) for model in MODELS):
                return
            audit = {"version": 1, "created_by": "seed", "updated_by": "seed",
                     "created_at": "2026-10-01T09:00:00.000+00:00", "updated_at": "2026-10-01T09:00:00.000+00:00"}
            portfolios = [Portfolio(id=1, name="Shared balanced portfolio", base_currency="CHF", status="active", **audit),
                          Portfolio(id=2, name="Alice private portfolio", base_currency="CHF", status="active", **audit),
                          Portfolio(id=3, name="Bob private portfolio", base_currency="EUR", status="active", **audit)]
            session.add_all(portfolios)
            session.flush()
            session.add_all([PortfolioPermission(user=user, portfolio_id=portfolio, can_write=True)
                             for user, portfolio in (("alice", 1), ("bob", 1), ("alice", 2), ("bob", 3))])
            instruments = [Instrument(id=1, symbol="CHDIV", name="Swiss dividend fund", asset_class="fund", currency="CHF", isin="CH0000000001", **audit),
                           Instrument(id=2, symbol="EUCORE", name="European equity fund", asset_class="fund", currency="EUR", isin="LU0000000002", **audit),
                           Instrument(id=3, symbol="UST2030", name="US Treasury 2030", asset_class="bond", currency="USD", isin="US0000000003", **audit)]
            session.add_all(instruments)
            session.flush()
            for portfolio_id in (1, 2, 3):
                session.add(Mandate(portfolio_id=portfolio_id, name=f"Portfolio {portfolio_id} investment mandate",
                                    strategy="balanced", risk_level=3, status="active", **audit))
                session.add(Position(portfolio_id=portfolio_id, instrument_id=portfolio_id,
                                     quantity=Decimal("125.125000"), average_cost=Decimal("98.7500"), **audit))
                session.add(CashBalance(portfolio_id=portfolio_id, currency="EUR" if portfolio_id == 3 else "CHF",
                                       amount=Decimal("25000.25"), **audit))
                session.add(Price(instrument_id=portfolio_id, as_of=date(2026, 10, 1), price=Decimal("102.5000"),
                                  currency=("CHF", "EUR", "USD")[portfolio_id - 1], **audit))
            session.add_all([FxRate(base_currency="EUR", quote_currency="CHF", as_of=date(2026, 10, 1), rate=Decimal("0.94000000"), **audit),
                             FxRate(base_currency="USD", quote_currency="CHF", as_of=date(2026, 10, 1), rate=Decimal("0.86000000"), **audit)])

    def schema(self, user: str) -> dict[str, Any]:
        with Session(self.engine) as session:
            self._permissions(session, user)
        entities = []
        for model in MODELS:
            # Put domain columns before system columns for generated forms/grids.
            columns = sorted(model.__table__.columns, key=lambda col: (not col.info["ui"]["editable"], col.name == "id"))
            entities.append({"name": model.__tablename__, "label": model.__label__, "title_field": model.__title_field__,
                             "fields": [{"name": column.name, **column.info["ui"]} for column in columns]})
        return {"entities": entities}

    @staticmethod
    def _model(entity: str) -> Any:
        try:
            return ENTITIES[entity]
        except KeyError:
            raise BankError(404, "not_found", "Unknown entity.") from None

    @staticmethod
    def _permissions(session: Session, user: str) -> dict[int, bool]:
        if not re.fullmatch(r"[A-Za-z0-9]{1,48}", user):
            raise BankError(401, "unauthorized", "A valid trusted user identity is required.")
        permissions = dict(session.execute(select(PortfolioPermission.portfolio_id, PortfolioPermission.can_write)
                                           .where(PortfolioPermission.user == user)).all())
        if not permissions:
            raise BankError(403, "forbidden", "No bank entitlement is provisioned for this user.")
        return permissions

    @staticmethod
    def _scope(statement: Any, model: Any, permissions: dict[int, bool]) -> Any:
        if model is Portfolio:
            return statement.where(model.id.in_(permissions))
        if hasattr(model, "portfolio_id"):
            return statement.where(model.portfolio_id.in_(permissions))
        return statement

    def _row(self, session: Session, model: Any, row_id: int, permissions: dict[int, bool]) -> Any:
        row = session.scalar(self._scope(select(model).where(model.id == row_id), model, permissions))
        if row is None:
            raise BankError(404, "not_found", "Row not found.")
        return row

    @staticmethod
    def _writable(model: Any, row: Any, permissions: dict[int, bool]) -> None:
        portfolio_id = row.id if model is Portfolio else getattr(row, "portfolio_id", None)
        if portfolio_id is not None and not permissions.get(portfolio_id, False):
            raise BankError(403, "forbidden", "Write permission is required for this portfolio.")

    @staticmethod
    def _validate(model: Any, values: Any, *, creating: bool) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise invalid("The request body must be a JSON object.")
        columns = {column.name: column for column in model.__table__.columns}
        errors = {}
        clean = {}
        for name in values:
            if name not in columns:
                errors[name] = "Unknown field."
            elif not columns[name].info["ui"]["editable"]:
                errors[name] = "System field is read-only."
        for name, column in columns.items():
            ui = column.info["ui"]
            if not ui["editable"]:
                continue
            if name not in values:
                if creating:
                    if "default" in ui:
                        clean[name] = ui["default"]
                    elif ui["nullable"]:
                        clean[name] = None
                    else:
                        errors[name] = "Required field."
                continue
            value = values[name]
            if value is None:
                if ui["nullable"]:
                    clean[name] = None
                else:
                    errors[name] = "This field cannot be null."
                continue
            kind = ui["type"]
            try:
                if kind in ("string", "enum"):
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError("A nonempty string is required.")
                    if isinstance(column.type, String) and len(value) > column.type.length:
                        raise ValueError(f"Maximum length is {column.type.length}.")
                    if kind == "enum" and value not in ui["choices"]:
                        raise ValueError("Choose one of the allowed values.")
                elif kind in ("integer", "reference"):
                    if type(value) is not int or not -(2 ** 63) <= value < 2 ** 63:
                        raise ValueError("A 64-bit integer is required.")
                    if kind == "reference" and value <= 0:
                        raise ValueError("A positive reference ID is required.")
                elif kind == "decimal":
                    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value, flags=re.ASCII):
                        raise ValueError("An exact decimal string is required.")
                    fraction = value.split(".")[1] if "." in value else ""
                    if len(fraction) > ui["scale"]:
                        raise ValueError(f'Maximum fractional digits: {ui["scale"]}.')
                    value = Decimal(value)
                    scaled = value * (10 ** ui["scale"])
                    if not -(2 ** 63) <= scaled < 2 ** 63:
                        raise ValueError("Decimal is outside the supported range.")
                elif kind == "date":
                    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
                        raise ValueError("A date in YYYY-MM-DD format is required.")
                    try:
                        value = date.fromisoformat(value)
                    except ValueError:
                        raise ValueError("A valid calendar date is required.") from None
                if "minimum" in ui and value < ui["minimum"]:
                    raise ValueError(f'Minimum value is {ui["minimum"]}.')
                if ui.get("exclusive_minimum") and value == ui["minimum"]:
                    raise ValueError(f'Value must be greater than {ui["minimum"]}.')
                if "maximum" in ui and value > ui["maximum"]:
                    raise ValueError(f'Maximum value is {ui["maximum"]}.')
                clean[name] = value
            except ValueError as error:
                errors[name] = str(error)
        if errors:
            raise invalid("Some fields are invalid.", errors)
        return clean

    @staticmethod
    def _references(session: Session, model: Any, values: dict[str, Any], permissions: dict[int, bool]) -> None:
        for name, value in values.items():
            ui = model.__table__.columns[name].info["ui"]
            if ui["type"] != "reference" or value is None:
                continue
            if ui["reference"] == "portfolios":
                # A denied target and an unknown target are intentionally indistinguishable.
                if not permissions.get(value, False):
                    raise BankError(403, "forbidden", "Write permission is required for the target portfolio.")
            referenced = ENTITIES[ui["reference"]]
            if session.get(referenced, value) is None:
                raise invalid("Invalid reference.", {name: "Referenced row does not exist."})

    @staticmethod
    def _version(body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            raise invalid("The request body must be a JSON object.")
        version = body.get("version")
        if type(version) is not int or version < 1 or version >= 2 ** 63:
            raise invalid("An expected version is required.", {"version": "A positive integer version is required."})
        return version, {key: value for key, value in body.items() if key != "version"}

    @staticmethod
    def _constraints(error: IntegrityError) -> BankError:
        # Do not leak SQL, driver diagnostics, or another portfolio's row values.
        if "FOREIGN KEY" in str(error.orig):
            return invalid("The row is referenced by another record, or a reference no longer exists.")
        if "UNIQUE" in str(error.orig):
            return invalid("A record with these unique values already exists.")
        return invalid("A domain constraint rejected these values.")

    def list(self, user: str, entity: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        model = self._model(entity)
        query = query or {}
        allowed = {"offset", "limit", "sort", "direction", "filter"}
        if set(query) - allowed:
            raise invalid("Unknown query parameter.", {key: "Unknown parameter." for key in set(query) - allowed})
        try:
            if not re.fullmatch(r"\d+", str(query.get("offset", "0")), flags=re.ASCII) or not re.fullmatch(r"\d+", str(query.get("limit", "100")), flags=re.ASCII):
                raise ValueError
            offset, limit = int(query.get("offset", "0")), int(query.get("limit", "100"))
            if offset > 2 ** 31 - 1 or not 1 <= limit <= 200:
                raise ValueError
        except (ValueError, TypeError):
            raise invalid("offset must be nonnegative and limit must be between 1 and 200.") from None
        sort, direction = query.get("sort", "id"), query.get("direction", "asc")
        if sort not in model.__table__.columns or direction not in ("asc", "desc"):
            raise invalid("Invalid sort field or direction.")
        term = query.get("filter", "")
        if not isinstance(term, str) or len(term) > 200:
            raise invalid("Filter must contain at most 200 characters.")
        with Session(self.engine) as session:
            permissions = self._permissions(session, user)
            statement = self._scope(select(model), model, permissions)
            if term:
                searchable = [column for column in model.__table__.columns if column.info["ui"]["editable"] and column.info["ui"]["type"] in ("string", "enum", "date", "reference", "integer")]
                statement = statement.where(or_(*(cast(column, String).contains(term, autoescape=True) for column in searchable)))
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            order = model.__table__.columns[sort].asc() if direction == "asc" else model.__table__.columns[sort].desc()
            rows = session.scalars(statement.order_by(order, model.id).offset(offset).limit(limit)).all()
            return {"items": [serialize(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    def get(self, user: str, entity: str, row_id: int) -> dict[str, Any]:
        model = self._model(entity)
        with Session(self.engine) as session:
            return {"item": serialize(self._row(session, model, row_id, self._permissions(session, user)))}

    def create(self, user: str, entity: str, body: Any) -> dict[str, Any]:
        model = self._model(entity)
        values = self._validate(model, body, creating=True)
        try:
            with Session(self.engine) as session, session.begin():
                permissions = self._permissions(session, user)
                self._references(session, model, values, permissions)
                stamp = now()
                row = model(**values, version=1, created_by=user, updated_by=user, created_at=stamp, updated_at=stamp)
                session.add(row)
                session.flush()
                if model is Portfolio:
                    session.add(PortfolioPermission(user=user, portfolio_id=row.id, can_write=True))
                return {"item": serialize(row)}
        except IntegrityError as error:
            raise self._constraints(error) from None

    def patch(self, user: str, entity: str, row_id: int, body: Any) -> dict[str, Any]:
        model = self._model(entity)
        expected, changes = self._version(body)
        values = self._validate(model, changes, creating=False)
        if not values:
            raise invalid("Supply at least one editable field.")
        try:
            with Session(self.engine) as session, session.begin():
                permissions = self._permissions(session, user)
                row = self._row(session, model, row_id, permissions)
                self._writable(model, row, permissions)
                self._references(session, model, values, permissions)
                result = session.execute(update(model).where(model.id == row_id, model.version == expected)
                                         .values(**values, version=expected + 1, updated_by=user, updated_at=now())
                                         .execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    raise BankError(409, "conflict", "The row changed. Reload it before saving.")
                session.expire(row)
                return {"item": serialize(row)}
        except IntegrityError as error:
            raise self._constraints(error) from None
        except OperationalError as error:
            if "locked" in str(error.orig).lower():
                raise BankError(409, "conflict", "A concurrent update is in progress. Reload before saving.") from None
            raise

    def delete(self, user: str, entity: str, row_id: int, body: Any) -> None:
        model = self._model(entity)
        expected, changes = self._version(body)
        if changes:
            raise invalid("Delete accepts only the expected version.")
        try:
            with Session(self.engine) as session, session.begin():
                permissions = self._permissions(session, user)
                row = self._row(session, model, row_id, permissions)
                self._writable(model, row, permissions)
                result = session.execute(delete(model).where(model.id == row_id, model.version == expected)
                                         .execution_options(synchronize_session=False))
                if result.rowcount != 1:
                    raise BankError(409, "conflict", "The row changed. Reload it before deleting.")
        except IntegrityError as error:
            raise self._constraints(error) from None
        except OperationalError as error:
            if "locked" in str(error.orig).lower():
                raise BankError(409, "conflict", "A concurrent update is in progress. Reload before deleting.") from None
            raise
