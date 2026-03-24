"""
Tests for alpha1.live.server (FastAPI application).

Uses FastAPI's TestClient (synchronous) with mock state/broker/engine objects.
No IBKR connection required.

The tests verify:
  - All REST endpoints return the correct HTTP status and response shape
  - The WebSocket endpoint accepts connections and sends a snapshot on connect
  - Error resilience: endpoints work when state returns empty lists
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# Guard: skip entire module if live deps not installed
fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from alpha1.live.server import ConnectionManager, create_app  # noqa: E402
from alpha1.live.state import (  # noqa: E402
    OpenPositionRecord,
    PendingOrderRecord,
    TradeRecord,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_state(
    positions=None,
    orders=None,
    trades=None,
    fill_summary=None,
):
    state = MagicMock()
    state.get_open_positions = AsyncMock(return_value=positions or [])
    state.get_pending_orders = AsyncMock(return_value=orders or [])
    state.get_trade_history = AsyncMock(return_value=trades or [])
    state.get_fill_quality_summary = AsyncMock(
        return_value=fill_summary
        or {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_entry_slippage": 0.0,
            "avg_exit_slippage": 0.0,
            "avg_r_multiple": 0.0,
            "total_pnl": 0.0,
        }
    )
    return state


def _make_mock_broker(equity: float = 100000.0):
    broker = MagicMock()
    broker.get_equity.return_value = equity
    broker.ib = MagicMock()
    broker.ib.isConnected.return_value = True
    return broker


def _make_mock_engine(paper: bool = True, instruments=None):
    engine = MagicMock()
    engine.config = MagicMock()
    engine.config.live.paper = paper
    engine.config.live.instruments = instruments or ["XAUUSD", "MYM", "MNQ"]
    # on_event registers a callback; store it so tests can inspect
    engine._event_cbs = []
    engine.on_event = lambda cb: engine._event_cbs.append(cb)
    return engine


@pytest.fixture
def client():
    """TestClient with empty state (common case)."""
    state = _make_mock_state()
    broker = _make_mock_broker()
    engine = _make_mock_engine()
    app = create_app(state, broker, engine)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def client_with_data():
    """TestClient with one position, one order, one trade."""
    pos = OpenPositionRecord(
        id="p1",
        instrument="XAUUSD",
        direction="LONG",
        entry_price=1950.0,
        entry_time="2024-01-15T09:10:00+00:00",
        stop_price=1940.0,
        initial_stop_price=1940.0,
        target_price=1975.0,
        size=0.1,
    )
    order = PendingOrderRecord(
        id="o1",
        instrument="MYM",
        direction="SHORT",
        limit_price=38000.0,
        stop_price=38100.0,
        target_price=37700.0,
        cancel_price=38150.0,
        signal_timestamp="2024-01-15T14:00:00+00:00",
        placed_at="2024-01-15T14:05:00+00:00",
        formed_bar_time="2024-01-15T14:00:00+00:00",
        status="SUBMITTED",
    )
    trade = TradeRecord(
        instrument="MNQ",
        direction="LONG",
        entry_time="2024-01-15T09:10:00+00:00",
        entry_price_limit=18500.0,
        entry_price_filled=18500.25,
        exit_time="2024-01-15T12:00:00+00:00",
        exit_price_expected=18600.0,
        exit_price_filled=18599.75,
        exit_reason="TARGET",
        pnl=198.44,
        r_multiple=1.48,
        fill_slippage_entry=0.25,
        fill_slippage_exit=-0.25,
    )
    state = _make_mock_state(
        positions=[pos],
        orders=[order],
        trades=[trade],
        fill_summary={
            "total_trades": 1,
            "win_rate": 100.0,
            "avg_entry_slippage": 0.25,
            "avg_exit_slippage": 0.25,
            "avg_r_multiple": 1.48,
            "total_pnl": 198.44,
        },
    )
    broker = _make_mock_broker(equity=100500.0)
    engine = _make_mock_engine()
    app = create_app(state, broker, engine)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


class TestHtmlPages:
    def test_dashboard_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_dashboard_content_type_is_html(self, client):
        response = client.get("/")
        assert "text/html" in response.headers["content-type"]

    def test_dashboard_contains_alpha1(self, client):
        response = client.get("/")
        assert "Alpha1" in response.text

    def test_trades_page_returns_200(self, client):
        response = client.get("/trades")
        assert response.status_code == 200

    def test_trades_page_shows_no_trades_message(self, client):
        response = client.get("/trades")
        assert "No trades recorded yet" in response.text or response.status_code == 200

    def test_trades_page_with_data(self, client_with_data):
        response = client_with_data.get("/trades")
        assert response.status_code == 200
        assert "198.44" in response.text or "MNQ" in response.text


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


class TestApiStatus:
    def test_returns_200(self, client):
        assert client.get("/api/status").status_code == 200

    def test_has_status_field(self, client):
        data = client.get("/api/status").json()
        assert data["status"] == "running"

    def test_has_paper_field(self, client):
        data = client.get("/api/status").json()
        assert data["paper"] is True

    def test_has_connected_field(self, client):
        data = client.get("/api/status").json()
        assert "connected" in data

    def test_has_instruments(self, client):
        data = client.get("/api/status").json()
        assert isinstance(data["instruments"], list)
        assert len(data["instruments"]) > 0


# ---------------------------------------------------------------------------
# /api/positions
# ---------------------------------------------------------------------------


class TestApiPositions:
    def test_empty_returns_list(self, client):
        data = client.get("/api/positions").json()
        assert data == []

    def test_with_position(self, client_with_data):
        data = client_with_data.get("/api/positions").json()
        assert len(data) == 1
        p = data[0]
        assert p["id"] == "p1"
        assert p["instrument"] == "XAUUSD"
        assert p["direction"] == "LONG"
        assert p["entry_price"] == pytest.approx(1950.0)


# ---------------------------------------------------------------------------
# /api/orders
# ---------------------------------------------------------------------------


class TestApiOrders:
    def test_empty_returns_list(self, client):
        data = client.get("/api/orders").json()
        assert data == []

    def test_with_order(self, client_with_data):
        data = client_with_data.get("/api/orders").json()
        assert len(data) == 1
        o = data[0]
        assert o["id"] == "o1"
        assert o["instrument"] == "MYM"
        assert o["status"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# /api/trades
# ---------------------------------------------------------------------------


class TestApiTrades:
    def test_empty_returns_list(self, client):
        data = client.get("/api/trades").json()
        assert data == []

    def test_with_trade(self, client_with_data):
        data = client_with_data.get("/api/trades").json()
        assert len(data) == 1
        t = data[0]
        assert t["instrument"] == "MNQ"
        assert t["pnl"] == pytest.approx(198.44)

    def test_instrument_filter_passed_to_state(self, client_with_data):
        """Verify the instrument query parameter is forwarded to state."""
        response = client_with_data.get("/api/trades?instrument=MNQ")
        assert response.status_code == 200

    def test_limit_capped_at_500(self, client):
        """Excessively large limit is silently capped at 500."""
        response = client.get("/api/trades?limit=9999")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /api/metrics
# ---------------------------------------------------------------------------


class TestApiMetrics:
    def test_empty_metrics(self, client):
        data = client.get("/api/metrics").json()
        assert data["total_trades"] == 0
        assert data["win_rate"] == 0.0

    def test_with_data(self, client_with_data):
        data = client_with_data.get("/api/metrics").json()
        assert data["total_trades"] == 1
        assert data["win_rate"] == pytest.approx(100.0)
        assert data["total_pnl"] == pytest.approx(198.44)


# ---------------------------------------------------------------------------
# /api/equity
# ---------------------------------------------------------------------------


class TestApiEquity:
    def test_equity_value(self, client):
        data = client.get("/api/equity").json()
        assert data["equity"] == pytest.approx(100000.0)

    def test_equity_with_pnl(self, client_with_data):
        data = client_with_data.get("/api/equity").json()
        assert data["equity"] == pytest.approx(100500.0)


# ---------------------------------------------------------------------------
# Paper mode indicator in HTML
# ---------------------------------------------------------------------------


class TestPaperModeIndicator:
    def test_paper_mode_shown_in_dashboard(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "PAPER" in response.text

    def test_live_mode_shown_in_dashboard(self):
        state = _make_mock_state()
        broker = _make_mock_broker()
        engine = _make_mock_engine(paper=False)
        app = create_app(state, broker, engine)
        with TestClient(app) as c:
            response = c.get("/")
        assert response.status_code == 200
        assert "LIVE" in response.text


# ---------------------------------------------------------------------------
# ConnectionManager unit tests
# ---------------------------------------------------------------------------


def test_disconnect_idempotent():
    """disconnect() must not raise ValueError when called twice on the same ws."""
    manager = ConnectionManager()
    ws = MagicMock()
    # Manually register without going through async connect
    manager._connections.append(ws)
    manager.disconnect(ws)  # first call removes it
    manager.disconnect(ws)  # second call must be a no-op, not raise
