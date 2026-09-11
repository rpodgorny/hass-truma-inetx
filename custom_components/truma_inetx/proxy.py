"""Track the concrete Bluetooth proxy used for a Truma panel."""

from __future__ import annotations

from collections.abc import Callable

from habluetooth import get_manager


class TrumaProxyTracker:
    """Report whether the last proxy route used by the panel is registered."""

    def __init__(self, async_on_change: Callable[[], None]) -> None:
        """Initialize without guessing which of HA's proxies belongs to Truma."""
        self._manager = get_manager()
        self._async_on_change = async_on_change
        self.source: str | None = None

    @property
    def available(self) -> bool | None:
        """Return proxy registration state, or unknown before a route is known."""
        if self.source is None:
            return None
        return self._manager.async_scanner_by_source(self.source) is not None

    def async_setup(self) -> Callable[[], None]:
        """Listen for the identified scanner being added or removed."""
        register = getattr(
            self._manager, "async_register_scanner_registration_callback", None
        )
        if register is None:
            return lambda: None
        return register(self._async_scanner_registration, None)

    def remember_source(self, source: str) -> None:
        """Remember the remote scanner that actually supplied a panel route."""
        if source == self.source:
            return
        self.source = source
        self._async_on_change()

    def _async_scanner_registration(self, event: object) -> None:
        """Notify entities when the selected proxy appears or disappears."""
        scanner = getattr(event, "scanner", None)
        if scanner is not None and scanner.source == self.source:
            self._async_on_change()
