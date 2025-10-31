from base64 import b64encode
from datetime import datetime, timezone
from decimal import Decimal

from app import repositories


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def test_requires_auth_for_portfolio_value(client):
    response = client.get("/portfolio/value")
    assert response.status_code == 401


def test_returns_portfolio_series_with_auth(monkeypatch, client):
    def fake_nav_series(*args, **kwargs):
        return [
            {"bucket": datetime(2024, 1, 1, tzinfo=timezone.utc), "nav_eur": Decimal("123.45")},
            {"bucket": datetime(2024, 1, 2, tzinfo=timezone.utc), "nav_eur": Decimal("150.00")},
        ]

    monkeypatch.setattr(repositories, "fetch_portfolio_nav_series", fake_nav_series)

    response = client.get("/portfolio/value", headers=_auth_header("apiuser", "apipass"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["points"][0]["value"] == 123.45
    assert len(payload["points"]) == 2


def test_unrealized_endpoint_uses_repository(monkeypatch, client):
    def fake_unrealized(*args, **kwargs):
        return [
            {"symbol": "AAPL", "name": "Apple", "market_value_eur": Decimal("1000.00"), "unrealized_pnl_eur": Decimal("125.50")},
            {"symbol": "MSFT", "name": "Microsoft", "market_value_eur": None, "unrealized_pnl_eur": Decimal("-42.0")},
        ]

    monkeypatch.setattr(repositories, "fetch_unrealized_pnl", fake_unrealized)

    response = client.get("/portfolio/unrealized", headers=_auth_header("apiuser", "apipass"))
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["symbol"] == "AAPL"
    assert body["items"][1]["unrealized_pnl_eur"] == -42.0


def test_exposure_invalid_dimension_returns_400(monkeypatch, client):
    called = False

    def fake_positions(conn, account_id=None):  # pragma: no cover - should not run
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(repositories, "fetch_exposure_positions", fake_positions)

    response = client.get("/portfolio/exposure/foo", headers=_auth_header("apiuser", "apipass"))
    assert response.status_code == 400
    assert "Unsupported exposure dimension" in response.json()["detail"]
    assert called is False


def test_recent_trades_returns_items(monkeypatch, client):
    def fake_trades(conn, limit, account_id=None):
        return [
            {
                "executed_at": datetime(2024, 3, 1, tzinfo=timezone.utc),
                "account_id": "ACC1",
                "symbol": "AAPL",
                "trade_type": "BUY",
                "qty": Decimal("10"),
                "price": Decimal("150"),
                "currency": "USD",
                "net_amount": Decimal("-1500"),
                "fees": Decimal("1.5"),
            }
        ]

    monkeypatch.setattr(repositories, "fetch_recent_trades", fake_trades)

    response = client.get("/transactions/recent", headers=_auth_header("apiuser", "apipass"))
    assert response.status_code == 200
    trades = response.json()["trades"]
    assert trades[0]["symbol"] == "AAPL"
    assert trades[0]["quantity"] == 10.0
