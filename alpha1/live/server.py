"""
FastAPI web dashboard for Alpha1 live trading.

Serves the real-time dashboard via:
  - REST endpoints  → current status, positions, orders, trades, metrics, equity
  - WebSocket /ws   → push updates to all connected browser tabs
  - Jinja2 templates → server-rendered HTML (no build step, no npm)

Usage:
    from alpha1.live.server import create_app
    app = create_app(state, broker, engine)
    # Run via uvicorn inside asyncio.gather() with the engine

    # Testing:
    from fastapi.testclient import TestClient
    client = TestClient(app)

Design notes:
  - broker/state/engine are injected at app creation time and stored in app.state.
    REST endpoints access them via request.app.state.  This avoids module-level
    globals and makes the app testable with mock objects.
  - WebSocket broadcast is driven by engine events wired in __main__.py.
  - Template path is resolved relative to this file so the package works when
    installed anywhere.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from alpha1.live.broker import IBKRBroker
    from alpha1.live.engine import LiveEngine
    from alpha1.live.state import StateManager

log = logging.getLogger("alpha1.live.server")

# Resolved paths for templates and static files
_WEB_DIR = Path(__file__).parent.parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Tracks all open WebSocket connections and broadcasts messages to them."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        log.debug("WebSocket connected; total connections: %d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.remove(ws)
        log.debug("WebSocket disconnected; total connections: %d", len(self._connections))

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to all connected clients, removing dead connections."""
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    state: StateManager,
    broker: IBKRBroker,
    engine: LiveEngine,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Injects broker/state/engine into app.state for use by route handlers.
    Wires the engine event bus to WebSocket broadcast.
    """
    manager = ConnectionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Wire engine → WebSocket broadcast
        async def broadcast_event(event: dict) -> None:
            await manager.broadcast(event)

        engine.on_event(lambda evt: asyncio.ensure_future(broadcast_event(evt)))
        yield

    app = FastAPI(title="Alpha1 Live Trading", lifespan=lifespan)

    # Store dependencies in app.state for route access
    app.state.state_mgr = state
    app.state.broker = broker
    app.state.engine = engine
    app.state.ws_manager = manager

    # Templates and static files
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "instruments": engine.config.live.instruments,
                "paper": engine.config.live.paper,
            },
        )

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request):
        trades = await state.get_trade_history(limit=200)
        fill_summary = await state.get_fill_quality_summary()
        return templates.TemplateResponse(
            request,
            "trades.html",
            {
                "trades": trades,
                "fill_summary": fill_summary,
            },
        )

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status():
        return {
            "status": "running",
            "paper": engine.config.live.paper,
            "instruments": engine.config.live.instruments,
            "connected": broker.ib.isConnected(),
            "ws_connections": manager.connection_count,
        }

    @app.get("/api/positions")
    async def api_positions():
        positions = await state.get_open_positions()
        return [_record_to_dict(p) for p in positions]

    @app.get("/api/orders")
    async def api_orders():
        orders = await state.get_pending_orders()
        return [_record_to_dict(o) for o in orders]

    @app.get("/api/trades")
    async def api_trades(instrument: str | None = None, limit: int = 100):
        trades = await state.get_trade_history(instrument=instrument, limit=min(limit, 500))
        return [_record_to_dict(t) for t in trades]

    @app.get("/api/metrics")
    async def api_metrics():
        return await state.get_fill_quality_summary()

    @app.get("/api/equity")
    async def api_equity():
        return {"equity": broker.get_equity()}

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await manager.connect(ws)
        # Send current state snapshot to newly connected client
        try:
            positions = await state.get_open_positions()
            orders = await state.get_pending_orders()
            await ws.send_json(
                {
                    "type": "snapshot",
                    "data": {
                        "positions": [_record_to_dict(p) for p in positions],
                        "orders": [_record_to_dict(o) for o in orders],
                        "equity": broker.get_equity(),
                    },
                    "time": _now_iso(),
                }
            )
        except Exception:
            log.exception("Error sending WebSocket snapshot.")

        try:
            while True:
                # Keep connection alive; client sends "ping" keep-alives
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            log.exception("WebSocket error.")
            with suppress(ValueError):
                manager.disconnect(ws)

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_to_dict(record) -> dict:
    """Convert a dataclass record to a JSON-serialisable dict."""
    from dataclasses import asdict

    return asdict(record)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
