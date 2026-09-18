#!/usr/bin/env python3
"""Offline check that a panel is still found when nothing is active-scanning.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``bt`` helpers and the real ``config_flow`` steps run.

Why this exists: a name reaches Home Assistant only in a scan response, and
only an active scan solicits one. Home Assistant's default scanning mode is
AUTO, which starts passive and turns the radio active only for scheduled
windows -- four minutes after the scanner starts, then once every twelve hours
(habluetooth ``AUTO_INITIAL_SWEEP_DELAY`` / ``AUTO_REDISCOVERY_INTERVAL``).
Per-device windows exist as well, but only for an address some integration has
registered a Bluetooth callback on, and this one registers none.

Measured on the van (2026-09-18) with the adapter in passive mode: forty-five
seconds, 468 advertising reports, not one Name field from any device on the
bus. The panel's whole advertisement is twenty-one bytes --

    02 01 06                                   flags
    11 06 d1 9f 1b 1f 80 f2 b2 8e e8 11 b2 f3 02 00 31 fc
                                               fc310002-f3b2-11e8-...

-- which identifies a panel and cannot key one. So every advert aborted
discovery as "awaiting_name" and the manual step reported "no devices found",
with the panel sitting there advertising: indistinguishable, from the outside,
from not hearing it at all.

It looked like it worked on a host that had bonded the panel before, because
BlueZ keeps ``Name=`` in ``/var/lib/bluetooth/<adapter>/<addr>/info`` and hands
it over whatever the scan mode. That cache is gone exactly when it is needed --
a first pairing, or a re-pair after the bond was dropped.

What it pins:

1. a nameless panel advert asks for an active window rather than only aborting,
2. it does not ask again for every advert that follows -- a panel sends several
   a second,
3. the manual step asks for a window when it has nothing to offer, and offers
   the panel once the window produces a named advert,
4. the manual step does not ask when a named advert is already in hand,
5. a request that opened no window is made again -- the scheduler skips a
   scanner that is mid-connect, and the van, one adapter with other
   integrations polling over it, refused the very first window this asked for
   ("connect in progress and no fallback scanner", 2026-09-18),
6. both callers stop asking the moment a name arrives,
7. a panel that gives no name at all is still keyed on the one BlueZ kept --
   after a successful bond this panel stopped answering scan requests
   altogether (three active windows, no scan response, 2026-09-18), so nothing
   on air carried a name and re-adding had nothing to key on,
8. asking degrades quietly: an older Home Assistant without the API, or a
   request that raises, leaves discovery working exactly as it did before.

Run: ``python3 tests/test_passive_scan_discovery.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-FFB4D1"
# What the panel puts on air in the capture above: one service UUID, no name.
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"
# A resolvable private address, which is all Home Assistant has to call the
# panel while no scan response has arrived.
RPA = "62:4A:BD:AD:73:5D"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Info:
    """Stand-in for a BluetoothServiceInfoBleak advert."""

    def __init__(
        self,
        name: str = "",
        uuids: tuple[str, ...] = (),
        address: str = RPA,
    ) -> None:
        # Home Assistant substitutes the address when there is no local name.
        self.name = name or address
        self.service_uuids = list(uuids)
        self.address = address
        self.time = 0.0
        self.rssi = -70
        self.connectable = True


# What the scanner has heard. A sweep may add to it, which is the whole point.
ADVERTS: list[_Info] = []
# What each sweep makes the scanner hear, popped one per sweep.
SWEEP_YIELDS: list[list[_Info]] = []
# Every sweep asked for, as (duration,). Raises instead if SWEEP_RAISES is set.
SWEEPS: list[float | None] = []
SWEEP_RAISES: list[Exception] = []


async def _request_active_scan(_hass, duration=None) -> None:
    """Stand-in for homeassistant.components.bluetooth.async_request_active_scan."""
    SWEEPS.append(duration)
    if SWEEP_RAISES:
        raise SWEEP_RAISES.pop(0)
    if SWEEP_YIELDS:
        ADVERTS.extend(SWEEP_YIELDS.pop(0))


# What BlueZ remembers, by address: the last resort behind advert_name and the
# only thing a bonded panel leaves to key on.
REMEMBERED: dict[str, str] = {}


class _BluezDevice:
    """What ble.device_from_bluez hands back: props as bleak's scanner has them."""

    def __init__(self, address: str, name: str) -> None:
        self.address = address
        self.name = name
        self.details = {"path": f"/org/bluez/hci0/dev_{address.replace(':', '_')}",
                        "props": {"Address": address, "Name": name, "Alias": name}}


async def _remembered(address):
    name = REMEMBERED.get(address.upper())
    return _BluezDevice(address.upper(), name) if name else None


class _Aborted(Exception):
    """Raised in place of HA's abort, so a step's outcome is unambiguous."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Shown(Exception):
    """Raised in place of HA's async_show_form."""

    def __init__(self, step_id: str, schema=None) -> None:
        super().__init__(step_id)
        self.step_id = step_id
        self.schema = schema


class _Hass:
    """The parts of HomeAssistant the discovery steps touch."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.background: list[str] = []

    def async_create_background_task(self, coro, name, eager_start=True):
        """Start the task the way ``eager_start=True`` does, and finish it.

        Home Assistant runs an eagerly-started task synchronously up to its
        first suspension. The sweep never suspends here -- the stub above
        returns without awaiting anything -- so one step runs it to the end,
        and a coroutine still alive afterwards is a bug in this fake rather
        than something to leave pending when the loop closes.
        """
        self.background.append(name)
        try:
            coro.send(None)
        except StopIteration:
            return None
        coro.close()
        msg = "the background sweep suspended; this fake cannot finish it"
        raise AssertionError(msg)


class _ConfigFlow:
    """The parts of homeassistant.config_entries.ConfigFlow the steps touch."""

    # Class attributes, not __init__: TrumaConfigFlow defines its own __init__
    # and does not chain up, exactly as it does under the real base class.
    hass = _Hass()
    unique_id: str | None = None
    source = "bluetooth"

    def __init_subclass__(cls, **kwargs) -> None:  # domain=DOMAIN
        super().__init_subclass__()

    @property
    def context(self) -> dict:
        if not hasattr(self, "_context"):
            self._context: dict = {}
        return self._context

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        return None

    def _async_current_ids(self):
        return set()

    def _set_confirm_only(self):
        return None

    def async_abort(self, *, reason: str):
        raise _Aborted(reason)

    def async_show_form(self, *, step_id, data_schema=None, **kw):
        raise _Shown(step_id, data_schema)


def _load():
    """Import the real const/bt/config_flow with externals stubbed out."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod(
        "homeassistant.config_entries",
        SOURCE_RECONFIGURE="reconfigure",
        ConfigEntry=dict,
        ConfigFlow=_ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlow=object,
    )
    _mod("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
    _mod("homeassistant.components", __path__=[])
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=object)
    _mod(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=_Info,
        async_ble_device_from_address=lambda *a, **kw: None,
        async_discovered_service_info=lambda _hass, connectable=True: list(ADVERTS),
        async_scanner_devices_by_address=lambda *a, **kw: [],
        async_request_active_scan=_request_active_scan,
    )
    _mod("truma_pkg", __path__=[str(SRC)])
    # config_flow only reads two names off the coordinator, and pairing is
    # never reached by the steps under test.
    _mod("truma_pkg.coordinator", CONF_POLL_INTERVAL="poll", DEFAULT_POLL_INTERVAL=0)
    _mod("truma_pkg.ble", device_from_bluez=_remembered)
    _mod("truma_pkg.pairing", ensure_bonded=None)

    def _real(name: str):
        spec = importlib.util.spec_from_file_location(
            f"truma_pkg.{name}", SRC / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_pkg.{name}"] = module
        spec.loader.exec_module(module)
        return module

    const = _real("const")
    bt = _real("bt")
    config_flow = _real("config_flow")
    return const, bt, config_flow


CONST, BT, CF = _load()
BLUETOOTH = sys.modules["homeassistant.components.bluetooth"]


def _reset() -> None:
    """Forget every advert, every sweep, and the cooldown between them."""
    ADVERTS.clear()
    SWEEP_YIELDS.clear()
    SWEEPS.clear()
    SWEEP_RAISES.clear()
    REMEMBERED.clear()
    _ConfigFlow.hass = _Hass()
    # One window and stop, unless a test says otherwise: the retry loop is the
    # subject of two tests below and only slows down the other seven.
    BT.SWEEP_DEADLINE = 0.0
    BT.SWEEP_RETRY_PAUSE = 0.0


def _nameless() -> _Info:
    """The advert the panel actually sends: service UUID, no name."""
    return _Info(uuids=(SERVICE_UUID,))


def _discover(info: _Info):
    """Run async_step_bluetooth on one advert; return the flow or the abort."""
    flow = CF.TrumaConfigFlow()
    try:
        asyncio.run(flow.async_step_bluetooth(info))
    except _Aborted as abort:
        return abort
    except _Shown:
        pass
    return flow


def _manual_choices() -> list[str]:
    """Names the manual (user) step would offer for the adverts heard."""
    flow = CF.TrumaConfigFlow()
    try:
        asyncio.run(flow.async_step_user())
    except _Aborted as abort:
        assert abort.reason == "no_devices_found", abort.reason
        return []
    except _Shown as shown:
        assert shown.step_id == "user"
    return sorted(flow._discovered)


def test_a_nameless_advert_asks_for_an_active_window() -> None:
    """Without this the panel is heard forever and never offered."""
    _reset()
    result = _discover(_nameless())
    assert isinstance(result, _Aborted)
    assert result.reason == "awaiting_name"
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS


def test_a_named_advert_asks_for_nothing() -> None:
    _reset()
    result = _discover(_Info(name=PANEL, uuids=(SERVICE_UUID,)))
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL
    assert SWEEPS == []


def test_the_panel_does_not_get_a_window_per_advertisement() -> None:
    """A panel advertises a few times a second; each one aborts on no name."""
    _reset()
    for _ in range(20):
        _discover(_nameless())
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS

    # Once the cooldown is behind us, asking again is right: the panel is
    # still there, still nameless, and nothing else is going to ask.
    hass = _ConfigFlow.hass
    hass.data[BT._SWEEP_LAST] -= BT.SWEEP_COOLDOWN + 1.0
    _discover(_nameless())
    assert SWEEPS == [BT.SWEEP_SECONDS, BT.SWEEP_SECONDS], SWEEPS


def test_the_manual_step_asks_before_reporting_an_empty_room() -> None:
    """"No devices found" was being reported about a panel in plain sight."""
    _reset()
    ADVERTS.append(_nameless())
    # The window produces what the advertisement alone never carries.
    SWEEP_YIELDS.append([_Info(name=PANEL, uuids=(SERVICE_UUID,))])
    assert _manual_choices() == [PANEL]
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS


def test_a_bonded_panel_is_keyed_on_the_name_bluez_kept() -> None:
    """Nothing on air carries a name; BlueZ has had it since the bond.

    Measured on the van (2026-09-18): the bond completed, and from that moment
    the panel answered no scan request at all -- three full active windows, not
    one response -- while ``/var/lib/bluetooth/<adapter>/<addr>/info`` held
    ``Name=Truma iNetX-FFB4D1`` the whole time. Home Assistant never passes it
    on: advertisements reach it over an MGMT side channel carrying raw AD
    bytes, so the BlueZ object is never consulted.
    """
    _reset()
    REMEMBERED[RPA] = PANEL
    result = _discover(_nameless())
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL
    # The key, not the address that stood in for it on the air: the entry
    # title, the stored name and the resolver all read this one.
    assert result._name == PANEL
    assert result.context["title_placeholders"] == {"name": PANEL}
    # Nothing to ask for: the name was already available.
    assert SWEEPS == [], SWEEPS
    # And the manual step keys it the same way.
    _reset()
    REMEMBERED[RPA] = PANEL
    ADVERTS.append(_nameless())
    assert _manual_choices() == [PANEL]


def test_a_remembered_address_is_not_a_remembered_name() -> None:
    """BlueZ's own fallback for a device it cannot name is the address again."""
    _reset()
    REMEMBERED[RPA] = RPA.replace(":", "-")
    result = _discover(_nameless())
    assert isinstance(result, _Aborted)
    assert result.reason == "awaiting_name"
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS


def test_a_window_that_opened_nothing_is_asked_for_again() -> None:
    """Asking once is not getting one: a mid-connect scanner is skipped.

    Measured on the van (2026-09-18), single adapter, other integrations
    polling over it: the first window this ever asked for opened nothing, and
    habluetooth said why -- "connect in progress and no fallback scanner". The
    request returns success-shaped either way, so the only answer is to come
    back and ask again.
    """
    _reset()
    BT.SWEEP_DEADLINE = 5.0
    ADVERTS.append(_nameless())
    SWEEP_YIELDS.extend([[], [], [_Info(name=PANEL, uuids=(SERVICE_UUID,))]])
    assert _manual_choices() == [PANEL]
    assert len(SWEEPS) == 3, SWEEPS


def test_the_background_sweep_stops_once_a_name_is_on_the_bus() -> None:
    """It cannot see the flow's state, so it watches the bus instead."""
    _reset()
    BT.SWEEP_DEADLINE = 5.0
    SWEEP_YIELDS.append([_Info(name=PANEL, uuids=(SERVICE_UUID,))])
    result = _discover(_nameless())
    assert isinstance(result, _Aborted)
    assert len(SWEEPS) == 1, SWEEPS


def test_the_manual_step_still_reports_an_empty_room_when_it_is_one() -> None:
    _reset()
    assert _manual_choices() == []
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS


def test_the_manual_step_does_not_ask_when_it_already_has_a_name() -> None:
    """An active-scanning host pays nothing for any of this."""
    _reset()
    ADVERTS.append(_Info(name=PANEL, uuids=(SERVICE_UUID,)))
    assert _manual_choices() == [PANEL]
    assert SWEEPS == []


def test_an_unrelated_device_is_neither_offered_nor_swept_for() -> None:
    """A room full of other BLE devices is still an empty room to us.

    Only the manual step is asked here. The discovery step never sees a device
    that is not a panel -- Home Assistant routes it by the manifest's matchers
    -- and so does not test for one.
    """
    _reset()
    ADVERTS.append(_Info(name="Some Sensor", uuids=("180f",)))
    ADVERTS.append(_Info(uuids=("180f",), address="AA:BB:CC:DD:EE:FF"))
    assert _manual_choices() == []
    # One window, because the room looked empty -- not one per device.
    assert SWEEPS == [BT.SWEEP_SECONDS], SWEEPS


def test_a_home_assistant_without_the_api_still_discovers() -> None:
    """The sweep is an improvement, not a dependency."""
    _reset()
    ADVERTS.append(_Info(name=PANEL, uuids=(SERVICE_UUID,)))
    missing = BLUETOOTH.async_request_active_scan
    del BLUETOOTH.async_request_active_scan
    try:
        assert asyncio.run(BT.async_sweep_for_names(_ConfigFlow.hass)) is False
        assert SWEEPS == []
        assert _manual_choices() == [PANEL]
    finally:
        BLUETOOTH.async_request_active_scan = missing


def test_a_sweep_that_raises_is_not_a_failed_discovery() -> None:
    _reset()
    ADVERTS.append(_nameless())
    SWEEP_RAISES.append(RuntimeError("no scanners"))
    assert _manual_choices() == []
    assert SWEEPS == [BT.SWEEP_SECONDS]
    # And the attempt still counts against the cooldown, so a host with no
    # AUTO scanner at all is not asked once per advertisement forever.
    _discover(_nameless())
    assert SWEEPS == [BT.SWEEP_SECONDS]


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("passive scan discovery: all checks OK")


if __name__ == "__main__":
    main()
