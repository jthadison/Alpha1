"""
Tests for alpha1.live.contracts.

These tests exercise contract definitions and metadata WITHOUT requiring an
IBKR connection — no ib_async IB instance is needed.  The resolve_contract()
function IS tested for its error handling using a mock IB object.

Why test contract definitions at all?
  A wrong secType or exchange sends the wrong contract to IBKR, which silently
  qualifies to a different product or raises an obscure error at 3am.
  Explicit assertions catch typos before they reach production.
"""

import pytest

from alpha1.live.contracts import (
    _CONTRACT_SPECS,
    ContractSpec,
    _build_unqualified,
    get_contract_spec,
    get_what_to_show,
    is_forex,
)

# ---------------------------------------------------------------------------
# ContractSpec metadata
# ---------------------------------------------------------------------------


class TestContractSpecs:
    def test_xauusd_is_forex(self):
        assert is_forex("XAUUSD") is True

    def test_mym_is_not_forex(self):
        assert is_forex("MYM") is False

    def test_mnq_is_not_forex(self):
        assert is_forex("MNQ") is False

    def test_xauusd_what_to_show(self):
        assert get_what_to_show("XAUUSD") == "MIDPOINT"

    def test_mym_what_to_show(self):
        assert get_what_to_show("MYM") == "TRADES"

    def test_mnq_what_to_show(self):
        assert get_what_to_show("MNQ") == "TRADES"

    def test_unknown_symbol_is_forex_raises(self):
        with pytest.raises(ValueError, match="Unknown live trading symbol"):
            is_forex("BTCUSD")

    def test_unknown_what_to_show_raises(self):
        with pytest.raises(ValueError, match="No contract spec"):
            get_what_to_show("DOGEUSDT")

    def test_get_contract_spec_xauusd(self):
        spec = get_contract_spec("XAUUSD")
        assert isinstance(spec, ContractSpec)
        assert spec.is_forex is True
        assert spec.what_to_show == "MIDPOINT"

    def test_get_contract_spec_mym(self):
        spec = get_contract_spec("MYM")
        assert isinstance(spec, ContractSpec)
        assert spec.is_forex is False
        assert spec.what_to_show == "TRADES"

    def test_all_instruments_have_specs(self):
        """get_what_to_show() is consistent with _CONTRACT_SPECS for every symbol."""
        for symbol, spec in _CONTRACT_SPECS.items():
            assert get_what_to_show(symbol) == spec.what_to_show

    def test_forex_midpoint_consistency(self):
        """All forex instruments must use MIDPOINT data."""
        for symbol, spec in _CONTRACT_SPECS.items():
            if spec.is_forex:
                assert spec.what_to_show == "MIDPOINT", (
                    f"{symbol} is forex but uses {spec.what_to_show} — "
                    "forex instruments must use MIDPOINT to avoid 'No data of type TRADES' errors"
                )

    def test_futures_trades_consistency(self):
        """All futures instruments must use TRADES data."""
        for symbol, spec in _CONTRACT_SPECS.items():
            if not spec.is_forex:
                assert spec.what_to_show == "TRADES", f"{symbol} is futures but uses {spec.what_to_show}"


# ---------------------------------------------------------------------------
# _build_unqualified — contract construction shapes
# ---------------------------------------------------------------------------


class TestBuildUnqualified:
    """Verify that _build_unqualified creates the correct ib_async contract types."""

    @pytest.fixture(autouse=True)
    def require_ib_async(self):
        pytest.importorskip("ib_async")

    def test_xauusd_is_forex_contract(self):
        from ib_async import Forex

        contract = _build_unqualified("XAUUSD")
        assert isinstance(contract, Forex)

    def test_xauusd_symbol(self):
        contract = _build_unqualified("XAUUSD")
        # ib_async Forex("XAUUSD") sets symbol='XAU', currency='USD'
        assert contract.symbol == "XAU"
        assert contract.currency == "USD"

    def test_xauusd_exchange(self):
        contract = _build_unqualified("XAUUSD")
        assert contract.exchange == "IDEALPRO"

    def test_xauusd_sec_type(self):
        contract = _build_unqualified("XAUUSD")
        assert contract.secType == "CASH"

    def test_mym_is_cont_future(self):
        from ib_async import ContFuture

        contract = _build_unqualified("MYM")
        assert isinstance(contract, ContFuture)

    def test_mym_exchange(self):
        contract = _build_unqualified("MYM")
        assert contract.exchange == "CBOT"

    def test_mym_symbol(self):
        contract = _build_unqualified("MYM")
        assert contract.symbol == "MYM"

    def test_mnq_is_cont_future(self):
        from ib_async import ContFuture

        contract = _build_unqualified("MNQ")
        assert isinstance(contract, ContFuture)

    def test_mnq_exchange(self):
        contract = _build_unqualified("MNQ")
        assert contract.exchange == "CME"

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError, match="No IBKR contract definition"):
            _build_unqualified("FAKE")


# ---------------------------------------------------------------------------
# resolve_contract — error handling (no real IBKR connection)
# ---------------------------------------------------------------------------


class TestResolveContract:
    """Test resolve_contract's error handling via a minimal mock IB."""

    @pytest.fixture(autouse=True)
    def require_ib_async(self):
        pytest.importorskip("ib_async")

    @pytest.mark.asyncio
    async def test_empty_qualification_raises_runtime_error(self):
        """When qualifyContractsAsync returns empty list, we get a clear error."""
        from unittest.mock import AsyncMock, MagicMock

        from alpha1.live.contracts import resolve_contract

        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])

        with pytest.raises(RuntimeError, match="Could not qualify"):
            await resolve_contract(mock_ib, "XAUUSD")

    @pytest.mark.asyncio
    async def test_successful_qualification_returns_contract(self):
        """When IBKR returns a qualified contract, we get it back."""
        from unittest.mock import AsyncMock, MagicMock

        from alpha1.live.contracts import resolve_contract

        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.lastTradeDateOrContractMonth = "20250321"
        mock_contract.exchange = "CBOT"

        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[mock_contract])

        result = await resolve_contract(mock_ib, "MYM")
        assert result is mock_contract

    @pytest.mark.asyncio
    async def test_xauusd_error_mentions_cmdty(self):
        """XAUUSD qualification failure message includes CMDTY account hint."""
        from unittest.mock import AsyncMock, MagicMock

        from alpha1.live.contracts import resolve_contract

        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])

        with pytest.raises(RuntimeError, match="CMDTY"):
            await resolve_contract(mock_ib, "XAUUSD")

    @pytest.mark.asyncio
    async def test_futures_error_mentions_permissions(self):
        """MYM qualification failure message mentions market data subscriptions."""
        from unittest.mock import AsyncMock, MagicMock

        from alpha1.live.contracts import resolve_contract

        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])

        with pytest.raises(RuntimeError, match="permissions"):
            await resolve_contract(mock_ib, "MYM")
