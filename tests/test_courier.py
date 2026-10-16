"""Tests for courier tracking: the on-the-way sensor, minutes countdown and fast ETA polling."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.rohlikcz.const import DOMAIN
from custom_components.rohlikcz.utils import is_courier_on_the_way, parse_courier_details

from fixtures_data import sample_api_data

ENTRY_DATA = {CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}
PRAGUE = ZoneInfo("Europe/Prague")

# Real announcements of one delivery, from waiting for the courier to the final approach.
PACKING = "Tašky už se začínají plnit vaším nákupem. Aktuálně to vypadá, že k vám kurýr dorazí zhruba v 09:00. Na viděnou!"
ON_THE_WAY = 'Váš nákup doručíme přibližně za 89 minut, tedy v <span style="color:#009B37">08:47</span>.'
FINAL = "Váš nákup doručíme přibližně za 8 minut."
COURIER = "Petr už Vám veze celkem 6 tašek."
COURIER_NEARBY = "Petr už Vám veze celkem 6 tašek. A kdyby něco, je na čísle +420000000001."


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (PACKING, False),
        ("Kurýr přijede s nákupem přibližně v 10:00. Stále je možné přidávat do objednávky další zboží.", False),
        ("Váš nákup přivezeme 16.10. v 09:00. Až do pátku 02:50 si k objednávce můžete cokoliv přidat!", False),
        (ON_THE_WAY, True),
        (FINAL, True),
        ("Váš nákup doručíme přibližně za 4 minuty.", True),
    ],
)
def test_is_courier_on_the_way(content: str, expected: bool) -> None:
    assert is_courier_on_the_way({"id": 1, "content": content}) is expected


def test_is_courier_on_the_way_without_announcement() -> None:
    assert is_courier_on_the_way(None) is False
    assert is_courier_on_the_way({"id": 1}) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (COURIER, {"courier": "Petr", "bags": 6, "phone": None}),
        (COURIER_NEARBY, {"courier": "Petr", "bags": 6, "phone": "+420000000001"}),
        ("Novák už Vám veze celkem 7 tašek. A kdyby něco, je na čísle +420 000 000 002.",
         {"courier": "Novák", "bags": 7, "phone": "+420000000002"}),
        ("Pokud jste na něco zapomněli, jednoduše to přidejte do košíku.",
         {"courier": None, "bags": None, "phone": None}),
        (None, {"courier": None, "bags": None, "phone": None}),
    ],
)
def test_parse_courier_details(text: str | None, expected: dict) -> None:
    assert parse_courier_details(text) == expected


def _announcement(order_id: int, content: str, additional: str | None = None) -> dict:
    return {
        "id": order_id,
        "title": "Nákup už frčí k Vám!",
        "updatedAt": datetime.now(PRAGUE).isoformat(),
        "content": content,
        "additionalContent": additional,
    }


def _data(*announcements: dict) -> dict:
    slot_start = datetime.now(PRAGUE).replace(second=0, microsecond=0) + timedelta(hours=1)
    data = sample_api_data()
    data["next_order"] = [{
        "id": 7001,
        "deliverySlot": {
            "since": slot_start.isoformat(),
            "till": (slot_start + timedelta(hours=1)).isoformat(),
        },
    }]
    data["delivery_announcements"]["data"]["announcements"] = list(announcements)
    return data


async def _setup(hass: HomeAssistant, data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123456", data=ENTRY_DATA, options={})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.rohlikcz.hub.RohlikAPI.get_data",
        new=AsyncMock(return_value=data),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"123456_{key}")
    assert entity_id is not None
    return entity_id


def _with_announcements(data: dict, *announcements: dict) -> dict:
    updated = copy.deepcopy(data)
    updated["delivery_announcements"]["data"]["announcements"] = list(announcements)
    return updated


async def test_courier_on_the_way_lifecycle(hass: HomeAssistant) -> None:
    """Off while packing, on with courier details once the countdown starts, off when delivered."""
    data = _data(_announcement(7001, PACKING))
    entry = await _setup(hass, data)
    courier_id = _entity_id(hass, "binary_sensor", "courier_on_the_way")
    assert hass.states.get(courier_id).state == "off"

    departed = dt_util.now()
    entry.runtime_data.async_set_updated_data(
        _with_announcements(data, _announcement(7001, ON_THE_WAY, COURIER))
    )
    await hass.async_block_till_done()
    state = hass.states.get(courier_id)
    assert state.state == "on"
    assert state.attributes["order_id"] == "7001"
    assert state.attributes["courier"] == "Petr"
    assert state.attributes["bags"] == 6
    assert state.attributes["nearby"] is False
    assert abs(state.attributes["departed_at"] - departed) < timedelta(seconds=5)
    initial_eta = state.attributes["initial_eta"]
    assert (initial_eta.hour, initial_eta.minute) == (8, 47)

    # A new announcement text keeps the original departure time.
    entry.runtime_data.async_set_updated_data(
        _with_announcements(data, _announcement(7001, FINAL, COURIER_NEARBY))
    )
    await hass.async_block_till_done()
    state = hass.states.get(courier_id)
    assert state.attributes["nearby"] is True
    assert state.attributes["phone"] == "+420000000001"
    assert abs(state.attributes["departed_at"] - departed) < timedelta(seconds=5)
    assert state.attributes["initial_eta"] == initial_eta

    entry.runtime_data.async_set_updated_data(_with_announcements(data))
    await hass.async_block_till_done()
    assert hass.states.get(courier_id).state == "off"


async def test_delivery_minutes_counts_down(hass: HomeAssistant, freezer) -> None:
    """The minutes sensor follows the delivery time and ticks every minute."""
    freezer.move_to(datetime(2026, 10, 16, 8, 52, 1, tzinfo=PRAGUE))
    await _setup(hass, _data(_announcement(7001, FINAL, COURIER)))
    minutes_id = _entity_id(hass, "sensor", "delivery_minutes")
    assert hass.states.get(minutes_id).state == "8"

    with patch(
        "custom_components.rohlikcz.hub.RohlikAccount.refresh_delivery_times",
        new=AsyncMock(),
    ):
        freezer.tick(timedelta(minutes=3))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert hass.states.get(minutes_id).state == "5"


async def test_fast_eta_polling_while_courier_is_on_the_way(hass: HomeAssistant, freezer) -> None:
    """The live ETA is polled every 2 minutes on the way and every minute for the final approach."""
    freezer.move_to(datetime(2026, 10, 16, 7, 17, 13, tzinfo=PRAGUE))
    on_the_way = {"data": {"announcements": [_announcement(7001, ON_THE_WAY, COURIER)]}}
    final = {"data": {"announcements": [_announcement(7001, FINAL, COURIER_NEARBY)]}}
    delivered = {"data": {"announcements": []}}

    data = _data(_announcement(7001, ON_THE_WAY, COURIER))
    entry = await _setup(hass, data)
    get_announcements = AsyncMock(return_value=on_the_way)
    entry.runtime_data._client.delivery.get_announcements = get_announcements

    async def tick(minutes: float) -> None:
        freezer.tick(timedelta(minutes=minutes))
        # The regular 10-minute refresh may fire meanwhile; keep it offline.
        with patch(
            "custom_components.rohlikcz.hub.RohlikAPI.get_data",
            new=AsyncMock(return_value=_with_announcements(data, *get_announcements.return_value["data"]["announcements"])),
        ):
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

    await tick(1)
    assert get_announcements.await_count == 0
    await tick(1)
    assert get_announcements.await_count == 1

    get_announcements.return_value = final
    await tick(2)
    assert get_announcements.await_count == 2
    await tick(1)  # final approach: every minute
    assert get_announcements.await_count == 3

    get_announcements.return_value = delivered
    await tick(1)
    assert get_announcements.await_count == 4
    await tick(3)  # delivered: no more fast polling
    assert get_announcements.await_count == 4

