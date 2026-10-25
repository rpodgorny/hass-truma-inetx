"""Config flow for the Truma iNet X (BLE) integration.

Discovers the panel over Bluetooth, then walks the user through the one-time
Just Works bond (the panel shows no passkey). The panel uses a rotating
(resolvable private) BLE address, so the unique_id is keyed on the stable
advertised name and the address is treated as a mutable connection detail.
"""

from __future__ import annotations

import time
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
    ConfigEntryState,
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
    describe_scan_modes,
    is_panel_advert,
    note_named,
    scan_mode_report,
    warn_about_names,
)
from .const import DOMAIN, LOGGER, describe_scan_modes
from .coordinator import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .pairing import ensure_bonded

# One card per Home Assistant run explaining a panel that is heard but cannot be
# named, and the flag that keeps it to one. The address rotates every quarter of
# an hour, so a card keyed on the advertisement would reappear seconds after
# being dismissed; the notice is therefore keyed on nothing and shown once.
_UNNAMED_NOTICE = "unnamed_notice_shown"
_UNNAMED_UNIQUE_ID = f"{DOMAIN}-unnamed"

# How long the pairing step busy-loops Pair() while the panel is in add-device
# mode before reporting failure.
_PAIR_TIMEOUT = 60.0

# Offered on the pairing step *after* a failed attempt. Ticking it is the only
# way an existing bond on this host is ever removed: the panel refuses an
# unarmed attempt and a forgotten key identically, so the code cannot tell them
# apart and must not guess. The user can -- they know whether the panel's
# add-device screen was up.
CONF_FORGET_BOND = "forget_bond"


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


# How long a session that came up counts as proof that the bond works. The
# coordinator also says whether one is live right now; this window covers the
# minutes either side of a drop, because a panel that answered 30 s ago has not
# forgotten us. Kept short on purpose: past it the honest answer is "we do not
# know", and not knowing means the attempt is allowed to run.
BOND_PROVEN_WINDOW = 300.0


def bond_is_proven(entry: ConfigEntry | None, now: float | None = None) -> bool:
    """Whether this entry's bond is demonstrably carrying traffic.

    The only solid thing this host knows about the pair
    (``research/truma-reconfigure-damage.md`` §2): a bond that is carrying a
    session right now -- or was a few minutes ago -- is not half-held, whatever
    ``Device1.Paired`` says. That property cannot tell a complete record from the
    signature-only one BlueZ still calls "Paired", so it is deliberately not
    consulted here.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return False
    if getattr(coordinator, "session_live", False):
        return True
    last = getattr(coordinator, "last_session_ok_at", None)
    if not last:
        return False
    return (now or time.time()) - last <= BOND_PROVEN_WINDOW


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
        # The address of a panel that was heard but could not be named, kept for
        # the notice step's description.
        self._unnamed_address: str | None = None

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
            # Nothing on air carries a name, and none is remembered. Ask the
            # panel itself -- but only when there is no Truma entry at all:
            # with one configured, a nameless advertisement is most likely our
            # own panel (bonded, hence unnameable on a passive scan), and a
            # second card plus a second dialler would both be worse than the
            # wait. A first pairing is the case this exists for.
            async_sweep_for_names_soon(self.hass)
            # Say why, in words that name the layer: a user cannot guess that
            # the reason is a scan mode, and the log is where they look.
            warned = warn_about_names(hass=self.hass, address=discovery_info.address, info=discovery_info)
            # The log is not where a user looks. If the reason is one waiting
            # cannot fix -- a scanner pinned passive -- offer a card that says
            # the panel was heard and why it cannot be added.
            if warned and await self._async_offer_unnamed_notice():
                self._unnamed_address = discovery_info.address.upper()
                return await self.async_step_unnamed()
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
        note_named(self.hass, discovery_info.address)
        self._discovery_info = discovery_info
        # The name the flow was keyed on, not the one the advertisement
        # carried: those differ whenever the advert had none and BlueZ
        # supplied it, and everything downstream -- the entry title, the
        # stored CONF_NAME, and the resolver that matches adverts against it
        # -- needs the key rather than the address that stood in for it.
        self._name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_confirm()

    async def async_step_unnamed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A panel is heard and cannot be named: say so, and say why.

        The alternative -- aborting -- is indistinguishable from the integration
        never hearing the panel at all, which is exactly how issue #6 spent weeks
        being diagnosed as "not supported" when the panel was in range the whole
        time. Nothing is offered to add here, because there is nothing to key an
        entry on; this card exists to turn silence into a diagnosis, and it
        closes with **OK**.
        """
        if user_input is not None:
            return self.async_abort(reason="awaiting_name")
        self._set_confirm_only()
        return self.async_show_form(
            step_id="unnamed",
            description_placeholders={
                "address": self._unnamed_address or "",
                "scanners": describe_scan_modes(self.hass),
            },
        )

    async def _async_offer_unnamed_notice(self) -> bool:
        """Whether to show that card now: once per run, and never when configured.

        Two reasons for the two conditions. *Once per run*: the panel's address
        rotates every ~15 minutes and its advertisement arrives a few times a
        second, so a card keyed on it would come back seconds after a dismissal;
        the notice is a statement about the host's scanner, and one per boot is
        the whole of its value. *Never when configured*: with a Truma entry in
        place, a nameless advert is most likely that panel (a bonded panel stops
        answering scan requests), and telling its owner it cannot be named would
        be alarming and wrong.
        """
        if self._async_current_ids():
            return False
        gate = self.hass.data.setdefault(DOMAIN, {})
        if gate.get(_UNNAMED_NOTICE):
            return False
        # Set before the card is shown, not after: the flow can be dismissed,
        # reloaded or replaced, and the flag must survive all of that for the
        # "once" in "once per run" to mean anything.
        gate[_UNNAMED_NOTICE] = True
        await self.async_set_unique_id(_UNNAMED_UNIQUE_ID, raise_on_progress=True)
        self._abort_if_unique_id_configured()
        return True

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
            note_named(self.hass, info.address)
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
        forget_bond = bool((user_input or {}).get(CONF_FORGET_BOND))
        if (
            user_input is not None
            and not forget_bond
            and self.source == SOURCE_RECONFIGURE
            and bond_is_proven(self._get_reconfigure_entry())
        ):
            # Measured on the van 2026-10-25 (0.9.0b37): this exact press unloaded
            # the entry, called Pair() -- which BlueZ answered without putting a
            # single SMP frame on air -- and reloaded it, for ~30 s of outage and
            # nothing learnt. A bond carrying a session cannot be half-held, so
            # decline here, tell the truth, and leave "forget the stale bond" as
            # the way to insist.
            LOGGER.info(
                "Truma %s: reconfigure declined — this bond is carrying a session",
                self._name,
            )
            return self.async_show_form(
                step_id="pair",
                errors={"base": "bond_still_working"},
                description_placeholders={"name": self._name},
                data_schema=vol.Schema(
                    {vol.Optional(CONF_FORGET_BOND, default=False): bool}
                ),
            )

        if user_input is not None:
            # One dialler at a time (#33). The coordinator's reconnect loop
            # competes with this pairing for the panel's single connection slot,
            # and the panel refuses the flow's link while the session holds one.
            # Measured on the van 2026-10-24: same panel, same arming, entry
            # loaded -> every Pair() earned InProgress, cancel at 20 s, re-issue,
            # pairing_failed at 60 s, no bond; entry unloaded -> bonded in
            # seconds with 32 SMP frames. So unload for the attempt, and put the
            # entry back afterwards, success or failure.
            entry = (
                self._get_reconfigure_entry()
                if self.source == SOURCE_RECONFIGURE
                else None
            )
            unloaded = False
            bonded, client = False, None
            if entry is not None and entry.state is ConfigEntryState.LOADED:
                unloaded = await self.hass.config_entries.async_unload(
                    entry.entry_id
                )
                LOGGER.debug(
                    "Truma: entry unloaded for the pairing attempt (one dialler)"
                )
            try:
                bonded, client = await ensure_bonded(
                    self.hass,
                    self._name,
                    self._address,
                    adapter_path=self._connectable_adapter_path(),
                    timeout=_PAIR_TIMEOUT,
                    may_forget_bond=forget_bond,
                    # A configured entry already has a bond, so the panel
                    # answers links: keep the one Home Assistant dials and bond
                    # over it. Bonding through BlueZ instead makes a second
                    # dialler, which is what left the panel refusing us on
                    # 2026-10-25. A first pairing has no such evidence (#26).
                    own_the_link=self.source == SOURCE_RECONFIGURE,
                )
            except Exception:  # noqa: BLE001 - surface as a flow error, not a crash
                LOGGER.exception("Truma pairing error")
                bonded, client = False, None
            finally:
                # Put the entry back on failure -- but NOT when the pairing left
                # a live link to hand over. The reload below sets the entry up
                # *with* that link; setting it up here first dials the panel a
                # second time and leaves the handed-off link to die in the gap.
                # Measured on the van 2026-10-25: the coordinator logged
                # "handed-off connection was already closed; connecting fresh"
                # and the panel carried two links for a moment.
                if (
                    unloaded
                    and entry is not None
                    and entry.state is ConfigEntryState.NOT_LOADED
                    and not (bonded and client is not None)
                ):
                    await self.hass.config_entries.async_setup(entry.entry_id)
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

        # The destructive option appears only alongside a failure: before the
        # first attempt there is nothing to forget, and a success ends the flow.
        return self.async_show_form(
            step_id="pair",
            errors=errors,
            data_schema=(
                vol.Schema({vol.Optional(CONF_FORGET_BOND, default=False): bool})
                if errors
                else None
            ),
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
