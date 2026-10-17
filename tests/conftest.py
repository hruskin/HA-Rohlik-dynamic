"""Shared fixtures for the Rohlik.cz integration tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def offline_order_detail():
    """Keep the next order's item fetch offline; tests override the return value."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "rohlik_api.services.orders.OrderService.get_detail", new=AsyncMock(return_value=None)
    ) as get_detail:
        yield get_detail
