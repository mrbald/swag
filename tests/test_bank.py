"""Service-domain and real HTTP boundary checks; no GUI instrumentation/actions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from swag_bank import Bank, BankError
from swag_bank.http import create_app


class BankTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="swag-bank-")
        self.path = Path(self.directory.name) / "bank.sqlite3"
        self.bank = Bank(self.path)

    def tearDown(self):
        self.bank.close()
        self.directory.cleanup()

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(BankError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(set(caught.exception.envelope()["error"]), {"code", "message", "fields"})
        return caught.exception

    def test_metadata_drives_all_seven_entities_and_system_fields(self):
        schema = self.bank.schema("alice")["entities"]
        self.assertEqual({entity["name"] for entity in schema},
                         {"portfolios", "mandates", "instruments", "positions", "cash_balances", "prices", "fx_rates"})
        for entity in schema:
            fields = {field["name"]: field for field in entity["fields"]}
            self.assertIn(entity["title_field"], fields)
            for name in ("id", "version", "created_by", "updated_by", "created_at", "updated_at"):
                self.assertFalse(fields[name]["editable"])
        position = next(entity for entity in schema if entity["name"] == "positions")
        quantity = next(field for field in position["fields"] if field["name"] == "quantity")
        self.assertEqual((quantity["type"], quantity["scale"]), ("decimal", 6))
        self.error(403, self.bank.schema, "charlie")

    def test_service_entitlements_hide_private_books_and_reject_cross_book_writes(self):
        for user, expected in (("alice", {1, 2}), ("bob", {1, 3})):
            self.assertEqual({row["id"] for row in self.bank.list(user, "portfolios")["items"]}, expected)
            for entity in ("mandates", "positions", "cash_balances"):
                self.assertEqual({row["portfolio_id"] for row in self.bank.list(user, entity)["items"]}, expected)
        self.error(404, self.bank.get, "alice", "portfolios", 3)
        self.error(404, self.bank.patch, "alice", "portfolios", 3, {"version": 1, "name": "stolen"})
        self.error(404, self.bank.delete, "alice", "positions", 3, {"version": 1})
        self.error(403, self.bank.create, "alice", "cash_balances", {"portfolio_id": 3, "currency": "USD", "amount": "1.00"})
        self.error(403, self.bank.patch, "alice", "positions", 1, {"version": 1, "portfolio_id": 3})
        self.assertEqual(self.bank.list("alice", "portfolios", {"filter": "Bob"})["total"], 0)
        # A newly created portfolio is granted only to its creator.
        row = self.bank.create("alice", "portfolios", {"name": "New private book"})["item"]
        self.assertEqual(row["created_by"], "alice")
        self.error(404, self.bank.get, "bob", "portfolios", row["id"])
        self.bank.delete("alice", "portfolios", row["id"], {"version": 1})

    def test_read_only_book_permission_is_enforced_by_service(self):
        with self.bank.engine.begin() as connection:
            connection.execute(text("UPDATE portfolio_permissions SET can_write=0 WHERE user='bob' AND portfolio_id=1"))
        self.assertEqual(self.bank.get("bob", "portfolios", 1)["item"]["id"], 1)
        self.error(403, self.bank.patch, "bob", "portfolios", 1, {"version": 1, "name": "no"})
        self.error(403, self.bank.delete, "bob", "cash_balances", 1, {"version": 1})
        self.error(403, self.bank.create, "bob", "cash_balances", {"portfolio_id": 1, "currency": "USD", "amount": "1.00"})

    def test_optimistic_versions_and_trusted_actor(self):
        changed = self.bank.patch("alice", "portfolios", 1, {"version": 1, "name": "Alice edit"})["item"]
        self.assertEqual((changed["version"], changed["created_by"], changed["updated_by"]), (2, "seed", "alice"))
        self.error(409, self.bank.patch, "bob", "portfolios", 1, {"version": 1, "name": "stale"})
        self.error(409, self.bank.delete, "bob", "portfolios", 1, {"version": 1})
        self.assertEqual(self.bank.get("bob", "portfolios", 1)["item"]["name"], "Alice edit")
        for system in ("id", "created_by", "updated_by", "created_at", "updated_at"):
            self.error(422, self.bank.patch, "alice", "portfolios", 1, {"version": 2, system: "spoof"})
        self.error(422, self.bank.create, "alice", "portfolios", {"name": "Spoof", "version": 1})
        self.error(422, self.bank.patch, "alice", "portfolios", 1, {"name": "missing version"})

    def test_concurrent_saves_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)

        def save(user):
            barrier.wait()
            try:
                self.bank.patch(user, "portfolios", 1, {"version": 1, "name": user + " concurrent edit"})
                return 200
            except BankError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(save, ("alice", "bob"))), [200, 409])
        self.assertEqual(self.bank.get("alice", "portfolios", 1)["item"]["version"], 2)

    def test_scaled_integer_decimal_roundtrip_and_restart_persistence(self):
        row = self.bank.create("alice", "cash_balances", {"portfolio_id": 2, "currency": "USD", "amount": "92233720368547758.07"})["item"]
        self.assertEqual(row["amount"], "92233720368547758.07")
        with self.bank.engine.connect() as connection:
            stored = connection.execute(text("SELECT amount, typeof(amount) FROM cash_balances WHERE id=:id"), {"id": row["id"]}).one()
            self.assertEqual(tuple(stored), (9223372036854775807, "integer"))
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
        self.bank.patch("alice", "portfolios", 2, {"version": 1, "name": "Persisted name"})
        self.bank.close()
        self.bank = Bank(self.path)
        self.assertEqual(self.bank.get("alice", "cash_balances", row["id"])["item"]["amount"], row["amount"])
        self.assertEqual(self.bank.get("alice", "portfolios", 2)["item"]["name"], "Persisted name")
        self.assertEqual(self.bank.list("alice", "cash_balances")["total"], 3)
        self.assertEqual(self.bank.get("alice", "positions", 1)["item"]["quantity"], "125.125000")
        for invalid_amount in (1.1, "1.001", "NaN", "1e2", "92233720368547758.08"):
            self.error(422, self.bank.patch, "alice", "cash_balances", 1, {"version": 1, "amount": invalid_amount})

    def test_required_enum_ranges_dates_reference_and_domain_constraints(self):
        self.error(422, self.bank.create, "alice", "portfolios", {})
        self.error(422, self.bank.create, "alice", "portfolios", {"name": "Invalid", "status": "unknown"})
        self.error(422, self.bank.patch, "alice", "mandates", 1, {"version": 1, "risk_level": 0})
        self.error(422, self.bank.patch, "alice", "mandates", 1, {"version": 1, "risk_level": True})
        self.error(422, self.bank.patch, "alice", "positions", 1, {"version": 1, "instrument_id": 999})
        self.error(422, self.bank.patch, "alice", "positions", 1, {"version": 1, "quantity": "-1"})
        self.error(422, self.bank.patch, "alice", "prices", 1, {"version": 1, "as_of": "2026-02-30"})
        self.error(422, self.bank.patch, "alice", "fx_rates", 1, {"version": 1, "base_currency": "CHF"})
        self.error(422, self.bank.patch, "alice", "fx_rates", 1, {"version": 1, "rate": "0"})
        self.error(422, self.bank.delete, "alice", "portfolios", 1, {"version": 1})
        self.error(422, self.bank.create, "alice", "cash_balances", {"portfolio_id": 1, "currency": "CHF", "amount": "1.00"})
        # Nullable instrument metadata is accepted; DB foreign keys remain active
        # even when writes originate outside the generated API.
        instrument = self.bank.create("alice", "instruments", {"symbol": "TEST", "name": "Test", "isin": None})["item"]
        self.assertIsNone(instrument["isin"])
        with self.assertRaises(IntegrityError), self.bank.engine.begin() as connection:
            connection.execute(text("UPDATE positions SET instrument_id=999 WHERE id=1"))
        with self.assertRaises(IntegrityError), self.bank.engine.begin() as connection:
            connection.execute(text("UPDATE mandates SET risk_level=9 WHERE id=1"))

    def test_query_allowlists_paging_and_literal_filter(self):
        result = self.bank.list("alice", "portfolios", {"sort": "name", "direction": "desc", "limit": "1", "offset": "1"})
        self.assertEqual((result["total"], result["offset"], result["limit"], len(result["items"])), (2, 1, 1, 1))
        self.assertEqual(self.bank.list("alice", "portfolios", {"filter": "%"})["total"], 0)
        for query in ({"sort": "id; DROP TABLE portfolios"}, {"direction": "up"}, {"limit": "201"}, {"offset": "-1"}, {"untrusted": "x"}):
            self.error(422, self.bank.list, "alice", "portfolios", query)
        self.error(404, self.bank.list, "alice", "portfolio_permissions")


class BankHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="swag-bank-http-")
        self.bank = Bank(Path(self.directory.name) / "bank.sqlite3")
        self.client = TestClient(TestServer(create_app(self.bank, "test-service-token")))
        self.addAsyncCleanup(self.client.close)
        self.addCleanup(self.directory.cleanup)
        await self.client.start_server()
        self.headers = {"X-Swag-Service-Token": "test-service-token", "X-Swag-User": "alice"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_auth_health_crud_and_error_envelope(self):
        response = await self.client.get("/health")
        self.assertEqual((response.status, await response.json()), (200, {"status": "ok"}))
        for headers in ({}, {"X-Swag-Service-Token": "bad", "X-Swag-User": "alice"},
                        {"X-Swag-Service-Token": "test-service-token", "X-Swag-User": "alice<script>"}):
            response = await self.client.get("/api/schema", headers=headers)
            self.assertEqual(response.status, 401)
            self.assertEqual(set((await response.json())["error"]), {"code", "message", "fields"})
        response = await self.client.get("/api/schema", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())["entities"]), 7)
        response = await self.client.post("/api/portfolios", headers=self.headers, json={"name": "HTTP portfolio"})
        self.assertEqual(response.status, 201)
        row = (await response.json())["item"]
        response = await self.client.patch(f'/api/portfolios/{row["id"]}', headers=self.headers,
                                          json={"version": 1, "name": "Updated through HTTP"})
        self.assertEqual(response.status, 200)
        response = await self.client.patch(f'/api/portfolios/{row["id"]}', headers=self.headers,
                                          json={"version": 1, "name": "Stale HTTP edit"})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "conflict")
        response = await self.client.delete(f'/api/portfolios/{row["id"]}', headers=self.headers, json={"version": 2})
        self.assertEqual(response.status, 204)
        response = await self.client.get("/api/portfolios/3", headers=self.headers)
        self.assertEqual(response.status, 404)
        response = await self.client.post("/api/portfolios", headers=self.headers, data="{broken json")
        self.assertEqual(response.status, 422)
        response = await self.client.get("/api/portfolios?limit=1&limit=2", headers=self.headers)
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
