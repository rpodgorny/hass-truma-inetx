"""Config flow for the Truma iNet X (BLE) integration.

Discovers the panel over Bluetooth, then walks the user through the one-time
Just Works bond (the panel shows no passkey). The panel uses a rotating
(resolvable private) BLE address, so the unique_id is keyed on the stable
advertised name and the address is treated as a mutable connection detail.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME

from .bt import (
    advert_name,
    any_panel_named,
    async_known_name,
    async_resolve_device,
    async_sweep_for_names,
    async_sweep_for_names_soon,
    is_panel_advert,
)
from .const import DOMAIN, LOGGER
from .coordinator import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .pairing import ensure_bonded

# How long the pairing step busy-loops Pair() while the panel is in add-device
# mode before reporting failure.
_PAIR_TIMEOUT = 60.0


class TrumaOptionsFlow(OptionsFlow):
    """One knob: how often to talk to the panel, or stay connected."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the poll interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=86400)
                    )
                }
            ),
        )


class TrumaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Truma iNet X."""

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Expose the poll-interval option."""
        return TrumaOptionsFlow()

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        # keyed by stable device name -> latest advertisement seen
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        # The panel being added, resolved before the pairing step.
        self._name: str | None = None
        self._address: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Truma panel found by the Bluetooth integration.

        The panel uses a rotating (resolvable private) BLE address, so we key
        the unique_id on the stable advertised name and treat the address as a
        mutable connection detail. This collapses the per-address discovery
        flows into one and keeps the stored address fresh on rediscovery.
        """
        # A service-UUID matcher also routes here, so an advertisement can
        # arrive before HA has resolved the panel's name — leaving only the
        # ephemeral (rotating) address. Skip those so we surface a single,
        # correctly-named discovery instead of a second card titled with the
        # raw MAC. A name-carrying advertisement follows shortly and keys the
        # flow properly.
        #
        # "Follows shortly" is not free: the name rides the scan response, and
        # nothing solicits one on a scanner that is not actively scanning. So
        # ask for a window on the way out, rate-limited, rather than aborting
        # here several times a second forever — see bt.async_sweep_for_names.
        #
        # What is deliberately NOT required is that the name start with the
        # original panel's prefix. That test turned a renamed panel — the iNet X
        # Panel 2 of issue #6 — into a silent abort: the service-UUID matcher
        # routes its advert here, this step drops it, and no discovery card is
        # ever shown. From the outside that is indistinguishable from the
        # integration not hearing the panel at all. Reaching us at all means a
        # Truma-proprietary service UUID matched, which is identification
        # enough; the name is only wanted as a key.
        name = advert_name(discovery_info) or await async_known_name(
            discovery_info.address
        )
        if name is None:
            async_sweep_for_names_soon(self.hass)
            return self.async_abort(reason="awaiting_name")
        await self.async_set_unique_id(name)
        # The RPA rotates roughly every 15 minutes and every rotation lands
        # here with a new address. `reload_on_update` defaults to True, which
        # reloaded the whole config entry on each one -- tearing down and
        # rebuilding every platform four times an hour, measured at 85-93 %
        # availability instead of ~99 %. The address is still stored: the
        # update is applied before `reload_on_update` is tested. Nothing needs
        # the reload, because the coordinator resolves the panel's live RPA on
        # every connection via bt.async_resolve_device(), matching on the
        # stable name; entry.data[CONF_ADDRESS] is only a bootstrap hint.
        self._abort_if_unique_id_configured(
            updates={CONF_ADDRESS: discovery_info.address},
            reload_on_update=False,
        )
        self._discovery_info = discovery_info
        # The name the flow was keyed on, not the one the advertisement
        # carried: those differ whenever the advert had none and BlueZ
        # supplied it, and everything downstream -- the entry title, the
        # stored CONF_NAME, and the resolver that matches adverts against it
        # -- needs the key rather than the address that stood in for it.
        self._name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered panel, then pair."""
        assert self._discovery_info is not None
        assert self._name is not None
        if user_input is not None:
            self._address = self._discovery_info.address
            return await self.async_step_pair()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by picking from discovered Truma panels."""
        if user_input is not None:
            name = user_input[CONF_ADDRESS]
            info = self._discovered[name]
            await self.async_set_unique_id(name, raise_on_progress=False)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: info.address})
            self._name = name
            self._address = info.address
            return await self.async_step_pair()

        configured_names = self._async_current_ids()
        await self._collect_discovered(configured_names)
        if not self._discovered:
            # Nothing that can be keyed. On a scanner that is not actively
            # scanning that is the normal state, not an empty room: the panel
            # may well be advertising, and heard, and still have given no name
            # (bt.async_sweep_for_names). Ask for windows and keep looking
            # before telling the user there is nothing there -- this step is a
            # deliberate user action, so it is the one place worth waiting.
            # The stop condition watches the bus rather than this flow's own
            # state: collecting needs to ask BlueZ, which cannot be done from
            # inside a sweep's synchronous check.
            await async_sweep_for_names(
                self.hass, until=partial(any_panel_named, self.hass)
            )
            await self._collect_discovered(configured_names)

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(sorted(self._discovered))}
            ),
        )

    async def _collect_discovered(self, configured_names: set[str]) -> None:
        """Fold every keyable panel advert heard so far into ``_discovered``."""
        for info in async_discovered_service_info(self.hass):
            if not is_panel_advert(info):
                continue
            # Same reasoning as async_step_bluetooth: a panel is identified by
            # its service UUID or its name, but only a real name can key it --
            # from this advert, or failing that from what BlueZ remembers for
            # the address, which is all a bonded panel leaves us.
            name = advert_name(info) or await async_known_name(info.address)
            if name is None or name in configured_names:
                continue
            # dedupe by stable name; keep the most recent advertisement
            self._discovered[name] = info

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-pair an already-configured panel (when the bond is lost)."""
        entry = self._get_reconfigure_entry()
        self._name = entry.data[CONF_NAME]
        self._address = entry.data[CONF_ADDRESS]
        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform the one-time Just Works bond with the panel.

        Shows an instruction step; on submit, drives pairing (fast if the panel
        is already bonded). Re-shows with an error if the bond does not take.
        Shared by initial setup and reconfigure (re-pair).
        """
        assert self._name is not None and self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                bonded, client = await ensure_bonded(
                    self.hass,
                    self._name,
                    self._address,
                    adapter_path=self._connectable_adapter_path(),
                    timeout=_PAIR_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 - surface as a flow error, not a crash
                LOGGER.exception("Truma pairing error")
                bonded, client = False, None
            if bonded:
                if client is not None:
                    # Hand the live pairing connection to the coordinator that
                    # async_setup_entry is about to create, so the session runs
                    # on it instead of reconnecting (which wedges the RPA).
                    self.hass.data.setdefault(DOMAIN, {}).setdefault(
                        "pending_clients", {}
                    )[self._address.upper()] = client
                data = {CONF_ADDRESS: self._address, CONF_NAME: self._name}
                if self.source == SOURCE_RECONFIGURE:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data_updates=data
                    )
                return self.async_create_entry(title=self._name, data=data)
            errors["base"] = "pairing_failed"

        return self.async_show_form(
            step_id="pair",
            errors=errors,
            description_placeholders={"name": self._name},
        )

    def _connectable_adapter_path(self) -> str | None:
        """BlueZ adapter object path of the connectable route to the panel.

        Scopes pairing to the adapter HA will actually connect through. Returns
        None for proxy-backed devices (no local BlueZ adapter).

        Resolve the panel the way every other connection does -- by name, over
        whichever RPA it is currently advertising -- rather than by its identity
        address. The panel is almost never connectable *at* its identity
        address, so looking that up returned None, and an unscoped bond search
        then matched a stale bond on a different (even HA-disabled) adapter and
        reported "already bonded" without the panel ever being involved.
        """
        if self._name is None:
            return None
        # local_only: an adapter path is a BlueZ notion, and only a local
        # adapter's device carries one. Asking for any device would hand back
        # the proxy's view of the same address on a host that has both, and
        # this would answer "no adapter" for a panel BlueZ can plainly see.
        device = async_resolve_device(self.hass, self._name, local_only=True)
        details = getattr(device, "details", None)
        if isinstance(details, dict):
            path = details.get("path")
            if isinstance(path, str) and path.startswith("/org/bluez/"):
                return path.rsplit("/", 1)[0]
        if self._address is None:
            return None
        device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        details = getattr(device, "details", None)
        if isinstance(details, dict):
            path = details.get("path")
            if isinstance(path, str) and path.startswith("/org/bluez/"):
                return path.rsplit("/", 1)[0]
        return None
