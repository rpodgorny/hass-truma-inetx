# Dashboard card

[← README](../README.md)

The integration ships a thermostat card whose dial follows the mode: it sets the
temperature while heating, the fan speed while venting, and is disabled while
off. Home Assistant's own climate dial is bound to temperature and humidity
only, and climate fan modes are arbitrary strings rather than a numeric range,
so core cannot put fan speed on an arc.

**Nothing to install.** The integration serves the card at
`/truma_inetx/truma-climate-dial-card.js` and registers it with the frontend, so
it arrives and updates with the integration. Add it the ordinary way: edit a
dashboard, **+ Add card**, search for *Truma*. The picker shows a live dial and
the editor opens with this integration's climate entity already filled in — the
card carries a `getStubConfig` and a `getConfigForm`. Only the name is left, and
it is optional.

The **By entity** tab will not offer it: Home Assistant builds those suggestions
from core card types alone, and a custom card has no way in.

In YAML, if you would rather:

```yaml
type: custom:truma-climate-dial-card
entity: climate.truma_inetx_ffb4d1
name: Heating          # optional
```

## Why it ships this way

A HACS repository belongs to exactly one category, so this repository cannot
also be published as a HACS *plugin*. Serving the card from the integration
avoids a second repository to version and tag. The integration version is
appended to the URL as a query string, because the frontend service worker
caches assets for weeks and a browser hard-refresh does not bypass it — without
a changing URL an updated card would never reach the browser.

The trade-off: `add_extra_js_url` loads the module on every page load for every
user, not only when the card is on screen. About 19 KB.

The card does not reimplement the dial. It instantiates Home Assistant's own
`ha-control-circular-slider` and `ha-outlined-icon-button` and reuses the
frontend's layout CSS, so it inherits upstream's appearance and behaviour. Those
are internal frontend components with no stability guarantee: upstream
restyling arrives for free, an upstream rename breaks the card, which then
renders an explicit error naming the missing component.
