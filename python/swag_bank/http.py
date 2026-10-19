"""Private HTTP boundary. All domain access is routed through Bank."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
from typing import Any

from aiohttp import web

from .service import Bank, BankError, invalid


LOGGER = logging.getLogger(__name__)


def create_app(bank: Bank, token: str) -> web.Application:
    if not token:
        raise ValueError("SWAG_SERVICE_TOKEN must be configured")

    @web.middleware
    async def boundary(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            if request.path != "/health":
                supplied = request.headers.get("X-Swag-Service-Token", "")
                if not hmac.compare_digest(supplied.encode(), token.encode()):
                    raise BankError(401, "unauthorized", "A valid service token is required.")
                user = request.headers.get("X-Swag-User", "")
                if not re.fullmatch(r"[A-Za-z0-9]{1,48}", user):
                    raise BankError(401, "unauthorized", "A valid trusted user identity is required.")
            return await handler(request)
        except BankError as error:
            return web.json_response(error.envelope(), status=error.status)
        except web.HTTPException as error:
            return web.json_response({"error": {"code": "not_found" if error.status == 404 else "request_error",
                                                 "message": error.reason, "fields": {}}}, status=error.status)
        except Exception:
            LOGGER.exception("Bank request failed")
            return web.json_response({"error": {"code": "internal", "message": "The bank request failed.", "fields": {}}}, status=500)

    app = web.Application(middlewares=[boundary], client_max_size=64 * 1024)

    async def body(request: web.Request) -> Any:
        try:
            return await request.json()
        except (ValueError, UnicodeDecodeError):
            raise invalid("The request body must be valid JSON.") from None

    def row_id(request: web.Request) -> int:
        value = request.match_info["id"]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2 ** 63:
            raise invalid("A positive row ID is required.")
        return int(value)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def schema(request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(bank.schema, request.headers["X-Swag-User"]))

    async def listing(request: web.Request) -> web.Response:
        if any(len(request.query.getall(key)) != 1 for key in request.query):
            raise invalid("Repeated query parameters are not allowed.")
        result = await asyncio.to_thread(bank.list, request.headers["X-Swag-User"], request.match_info["entity"], dict(request.query))
        return web.json_response(result)

    async def get(request: web.Request) -> web.Response:
        result = await asyncio.to_thread(bank.get, request.headers["X-Swag-User"], request.match_info["entity"], row_id(request))
        return web.json_response(result)

    async def create(request: web.Request) -> web.Response:
        result = await asyncio.to_thread(bank.create, request.headers["X-Swag-User"], request.match_info["entity"], await body(request))
        return web.json_response(result, status=201)

    async def patch(request: web.Request) -> web.Response:
        result = await asyncio.to_thread(bank.patch, request.headers["X-Swag-User"], request.match_info["entity"], row_id(request), await body(request))
        return web.json_response(result)

    async def delete(request: web.Request) -> web.Response:
        await asyncio.to_thread(bank.delete, request.headers["X-Swag-User"], request.match_info["entity"], row_id(request), await body(request))
        return web.Response(status=204)

    async def cleanup(application: web.Application) -> None:
        await asyncio.to_thread(bank.close)

    app.router.add_get("/health", health)
    app.router.add_get("/api/schema", schema)
    app.router.add_get("/api/{entity}", listing)
    app.router.add_post("/api/{entity}", create)
    app.router.add_get("/api/{entity}/{id}", get)
    app.router.add_patch("/api/{entity}/{id}", patch)
    app.router.add_delete("/api/{entity}/{id}", delete)
    app.on_cleanup.append(cleanup)
    return app
