/*
 * truma-climate-dial-card
 *
 * A thermostat dial whose target follows the mode:
 *   - heating   -> the dial sets the temperature setpoint
 *   - fan only  -> the dial sets the fan speed
 *   - off       -> the dial is disabled
 *
 * Home Assistant's own climate dial is bound to temperature and humidity only
 * (more-info-climate.ts toggles _mainControl between "temperature" and
 * "humidity"), and climate fan modes are arbitrary strings rather than a
 * numeric range, so core cannot put fan speed on an arc. The Truma panel picks
 * its own fan speed while heating and has no setpoint at all while venting, so
 * exactly one of the two is meaningful at any time -- which is what this shows.
 *
 * This does NOT reimplement the dial. It instantiates Home Assistant's own
 * <ha-control-circular-slider>, <ha-outlined-icon-button> and <ha-svg-icon>,
 * and copies the layout CSS from the frontend's
 * state-control-circular-slider-style.ts, so it inherits upstream's look and
 * behaviour (including preventInteractionOnScroll, which is what stops a swipe
 * over the dial from dragging it).
 *
 * Those components are internal frontend API with no stability guarantee. That
 * is the deliberate trade: upstream restyling arrives for free, upstream
 * renames break this card. If it ever renders the error below, that is what
 * happened.
 *
 * Install: nothing to do. The integration serves this file at
 * /truma_inetx/truma-climate-dial-card.js and registers it with the frontend,
 * with the integration version in the query string so an update actually
 * reaches the browser past the service worker cache.
 *
 * Usage:
 *   type: custom:truma-climate-dial-card
 *   entity: climate.truma_inetx_ffb4d1
 *   name: Heating            # optional, defaults to the entity's name
 */

const DIAL = "ha-control-circular-slider";
const BUTTON = "ha-outlined-icon-button";

const MDI_MINUS = "M19,13H5V11H19V13Z";
const MDI_PLUS = "M19,13H13V19H11V13H5V11H11V5H13V11H19V13Z";

// Home Assistant's SLIDER_MODES for the climate dial.
const SLIDER_MODES = { heat: "start", fan_only: "full", off: "full" };

// Taps on +/- are batched into a single write after this long. Each write goes
// out over BLE to the panel, so tapping five times should not send five frames.
const STEP_COMMIT_MS = 800;

// Order and icons both mirror Home Assistant's own condensed climate widget so
// the card does not read as a foreign control next to it. The icons are the
// exact MDI paths from the frontend's CLIMATE_HVAC_MODE_ICONS map
// (off: mdiPower, fan_only: mdiFan, heat: mdiFire) — NOT circle/fan/thermometer.
const HVAC_BUTTONS = [
  { mode: "off", label: "Off", icon: "M16.56,5.44L15.11,6.89C16.84,7.94 18,9.83 18,12A6,6 0 0,1 12,18A6,6 0 0,1 6,12C6,9.83 7.16,7.94 8.88,6.88L7.44,5.44C5.36,6.88 4,9.28 4,12A8,8 0 0,0 12,20A8,8 0 0,0 20,12C20,9.28 18.64,6.88 16.56,5.44M13,3H11V13H13V3Z" },
  { mode: "fan_only", label: "Fan", icon: "M12,11A1,1 0 0,0 11,12A1,1 0 0,0 12,13A1,1 0 0,0 13,12A1,1 0 0,0 12,11M12.5,2C17,2 17.11,5.57 14.75,6.75C13.76,7.24 13.32,8.29 13.13,9.22C13.61,9.42 14.03,9.73 14.35,10.13C18.05,8.13 22.03,8.92 22.03,12.5C22.03,17 18.46,17.1 17.28,14.75C16.78,13.75 15.72,13.31 14.79,13.12C14.59,13.6 14.28,14 13.88,14.34C15.88,18.04 15.09,22 11.5,22C7,22 6.91,18.42 9.27,17.24C10.26,16.75 10.7,15.7 10.89,14.77C10.41,14.57 10,14.26 9.67,13.86C5.97,15.86 2,15.07 2,11.5C2,7 5.56,6.89 6.74,9.25C7.24,10.24 8.29,10.68 9.22,10.87C9.42,10.39 9.73,10 10.13,9.65C8.13,5.95 8.92,2 12.5,2Z" },
  { mode: "heat", label: "Heat", icon: "M17.66,11.2C17.43,10.9 17.15,10.64 16.89,10.38C16.22,9.78 15.46,9.35 14.82,8.72C13.33,7.26 13,4.85 13.95,3C13,3.23 12.17,3.75 11.46,4.32C8.87,6.4 7.85,10.07 9.07,13.22C9.11,13.32 9.15,13.42 9.15,13.55C9.15,13.77 9,13.97 8.8,14.05C8.57,14.15 8.33,14.09 8.14,13.93C8.08,13.88 8.04,13.83 8,13.76C6.87,12.33 6.69,10.28 7.45,8.64C5.78,10 4.87,12.3 5,14.47C5.06,14.97 5.12,15.47 5.29,15.97C5.43,16.57 5.7,17.17 6,17.7C7.08,19.43 8.95,20.67 10.96,20.92C13.1,21.19 15.39,20.8 17.03,19.32C18.86,17.66 19.5,15 18.56,12.72L18.43,12.46C18.22,12 17.66,11.2 17.66,11.2M14.5,17.5C14.22,17.74 13.76,18 13.4,18.1C12.28,18.5 11.16,17.94 10.5,17.28C11.69,17 12.4,16.12 12.61,15.23C12.78,14.43 12.46,13.77 12.33,13C12.21,12.26 12.23,11.63 12.5,10.94C12.69,11.32 12.89,11.7 13.13,12C13.9,13 15.11,13.44 15.37,14.8C15.41,14.94 15.43,15.08 15.43,15.23C15.46,16.05 15.1,16.95 14.5,17.5H14.5Z" },
];

// Force Home Assistant to evaluate the module that defines the dial. The
// thermostat card side-effect imports ha-state-control-climate-temperature,
// which imports the slider, the outlined icon button and ha-svg-icon.
let loadPromise;
function loadHaComponents(entity) {
  if (customElements.get(DIAL) && customElements.get(BUTTON)) {
    return Promise.resolve();
  }
  if (!loadPromise) {
    loadPromise = (async () => {
      const helpers = await window.loadCardHelpers();
      await helpers.createCardElement({ type: "thermostat", entity });
      await Promise.all([
        customElements.whenDefined(DIAL),
        customElements.whenDefined(BUTTON),
      ]);
    })();
  }
  return loadPromise;
}

class TrumaClimateDialCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._dragging = false;
    this._pending = null; // value being dragged or stepped, not yet sent
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error("A climate entity is required");
    if (!config.entity.startsWith("climate.")) {
      throw new Error("Entity must be a climate entity");
    }
    this._config = config;
    this.shadowRoot.innerHTML = "";
    this._built = false;
    this._building = false;
  }

  getCardSize() {
    return 5;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this._buildOnce();
      return;
    }
    // A drag owns the display until it is committed, so incoming state does not
    // yank the handle out from under a finger.
    if (!this._dragging) this._render();
  }

  async _buildOnce() {
    if (this._building) return;
    this._building = true;
    try {
      await loadHaComponents(this._config.entity);
      this._build();
      this._built = true;
      this._render();
    } catch (err) {
      this.shadowRoot.innerHTML = `<ha-card style="padding:16px">
        Could not load Home Assistant's dial component
        (<code>${DIAL}</code>): ${err}. This card reuses internal frontend
        components, which upstream may have renamed.
      </ha-card>`;
    }
  }

  /* ---------- what the dial currently drives ---------- */

  _target() {
    const st = this._hass && this._hass.states[this._config.entity];
    if (!st) return null;
    const a = st.attributes;
    const off = st.state === "off";

    if (st.state === "fan_only" && Array.isArray(a.fan_modes)) {
      const modes = a.fan_modes;
      const index = modes.indexOf(a.fan_mode);
      return {
        kind: "fan",
        min: 0,
        max: modes.length - 1,
        step: 1,
        value: index < 0 ? null : index,
        display: index < 0 ? "--" : modes[index],
        label: "Fan speed",
        color: "var(--state-fan-active-color, var(--primary-color))",
        modes,
      };
    }

    if (a.temperature !== undefined && a.temperature !== null) {
      return {
        kind: "temperature",
        min: a.min_temp !== undefined ? a.min_temp : 5,
        max: a.max_temp !== undefined ? a.max_temp : 30,
        step: a.target_temp_step || 0.5,
        value: a.temperature,
        display: `${a.temperature} °C`,
        label: off ? "Off" : "Target",
        current: a.current_temperature,
        color: off
          ? "var(--disabled-color, #bdbdbd)"
          : "var(--state-climate-heat-color, var(--primary-color))",
        disabled: off,
      };
    }

    if (st.state === "unknown") {
      // Connected, but the panel has not sent RoomClimate.Mode yet. It only
      // pushes that topic on change, so after a reconnect the mode can stay
      // unset while temperature frames keep arriving.
      return { kind: "none", label: "Waiting for panel", display: "--" };
    }
    return { kind: "none", label: off ? "Off" : "", display: "--", disabled: off };
  }

  /* ---------- writes ---------- */

  _commit(t, value) {
    const entity_id = this._config.entity;
    if (t.kind === "temperature") {
      this._hass.callService("climate", "set_temperature", { entity_id, temperature: value });
    } else if (t.kind === "fan") {
      this._hass.callService("climate", "set_fan_mode", {
        entity_id,
        fan_mode: t.modes[value],
      });
    }
  }

  _bump(delta) {
    const t = this._target();
    if (!t || t.kind === "none" || t.disabled) return;
    const base = this._pending !== null ? this._pending : t.value;
    if (base === null || base === undefined) return;

    this._pending = Number(
      Math.min(Math.max(base + delta * t.step, t.min), t.max).toFixed(2)
    );
    this._render();

    clearTimeout(this._stepTimer);
    this._stepTimer = setTimeout(() => {
      const value = this._pending;
      this._pending = null;
      if (value !== null && value !== t.value) this._commit(t, value);
      this._render();
    }, STEP_COMMIT_MS);
  }

  _setHvacMode(mode) {
    this._hass.callService("climate", "set_hvac_mode", {
      entity_id: this._config.entity,
      hvac_mode: mode,
    });
  }

  /* ---------- dom ---------- */

  _build() {
    const style = document.createElement("style");
    // Layout copied from the frontend's state-control-circular-slider-style.ts
    // so the dial, the centred readout and the +/- buttons sit exactly where
    // Home Assistant puts them. The md/sm/xs size variants are omitted: they
    // need a ResizeController and only serve to hide the buttons on small
    // tiles, which does not apply to a full-width card.
    style.textContent = `
      ha-card { padding: 16px; }
      .name { font-size: 1.1rem; font-weight: 500; margin-bottom: 8px; }
      .wrap { display: flex; justify-content: center; }
      .container { position: relative; width: 320px; max-width: 100%; }
      .info {
        position: absolute;
        top: 0; left: 0; right: 0; bottom: 0;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        pointer-events: none;
        font-size: var(--ha-font-size-l, 16px);
        line-height: var(--ha-line-height-normal, 1.5);
        letter-spacing: 0.1px;
        gap: var(--ha-space-2, 8px);
      }
      .info * { margin: 0; pointer-events: auto; }
      .label {
        width: 60%;
        font-weight: var(--ha-font-weight-medium, 500);
        text-align: center;
        color: var(--action-color, inherit);
        white-space: nowrap;
        min-height: 1.5em;
      }
      .primary-state { font-size: 36px; }
      .secondary { color: var(--secondary-text-color); }
      .buttons {
        position: absolute;
        bottom: 10px;
        left: 0; right: 0;
        margin: 0 auto;
        gap: var(--ha-space-6, 24px);
        display: flex;
        flex-direction: row;
        align-items: center;
        justify-content: center;
        pointer-events: none;
      }
      .buttons > * { pointer-events: auto; }
      .buttons ${BUTTON} {
        --md-outlined-icon-button-container-width: 48px;
        --md-outlined-icon-button-container-height: 48px;
        --md-outlined-icon-button-icon-size: 24px;
      }
      ${DIAL} {
        width: 100%;
        --control-circular-slider-color: var(--state-color, var(--disabled-color));
      }
      .modes { display: flex; justify-content: center; gap: 8px; margin-top: 12px; }
      .modes button {
        display: flex; align-items: center; justify-content: center;
        width: 44px; height: 44px; border: none; border-radius: 22px;
        background: var(--divider-color, #e0e0e0); color: var(--primary-text-color);
        cursor: pointer;
      }
      .modes button[aria-pressed="true"] {
        background: var(--primary-color); color: var(--text-primary-color, #fff);
      }
      .modes svg { width: 24px; height: 24px; }
      .unavailable { text-align: center; color: var(--error-color, #db4437); padding: 16px 0; }
      [hidden] { display: none !important; }
    `;

    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="name"></div>
      <div class="body">
        <div class="wrap">
          <div class="container">
            <${DIAL}></${DIAL}>
            <div class="info">
              <p class="label"></p>
              <p class="primary-state"></p>
              <p class="secondary"></p>
            </div>
            <div class="buttons">
              <${BUTTON} class="minus"><ha-svg-icon></ha-svg-icon></${BUTTON}>
              <${BUTTON} class="plus"><ha-svg-icon></ha-svg-icon></${BUTTON}>
            </div>
          </div>
        </div>
        <div class="modes"></div>
      </div>
      <div class="unavailable" hidden>Entity unavailable</div>
    `;

    this.shadowRoot.append(style, card);
    this._containerEl = card.querySelector(".container");
    this._dial = card.querySelector(DIAL);
    this._labelEl = card.querySelector(".label");
    this._stateEl = card.querySelector(".primary-state");
    this._secondaryEl = card.querySelector(".secondary");
    this._buttonsEl = card.querySelector(".buttons");
    this._minusEl = card.querySelector(".minus");
    this._plusEl = card.querySelector(".plus");
    this._modesEl = card.querySelector(".modes");
    this._nameEl = card.querySelector(".name");
    this._bodyEl = card.querySelector(".body");
    this._unavailableEl = card.querySelector(".unavailable");

    this._minusEl.querySelector("ha-svg-icon").path = MDI_MINUS;
    this._plusEl.querySelector("ha-svg-icon").path = MDI_PLUS;
    this._minusEl.addEventListener("click", () => this._bump(-1));
    this._plusEl.addEventListener("click", () => this._bump(1));

    // Upstream's own scroll guard: a swipe that starts on the dial scrolls the
    // page instead of dragging, which is exactly the behaviour of the stock
    // thermostat card.
    this._dial.preventInteractionOnScroll = true;

    this._dial.addEventListener("value-changing", (ev) => {
      this._dragging = true;
      this._pending = ev.detail.value;
      this._render();
    });
    this._dial.addEventListener("value-changed", (ev) => {
      this._dragging = false;
      const t = this._target();
      const value = ev.detail.value;
      this._pending = null;
      // One write per gesture: value-changed fires on release, value-changing
      // during the drag.
      if (t && t.kind !== "none" && !t.disabled && value !== t.value) {
        this._commit(t, value);
      }
      this._render();
    });

    // Per-instance, NOT stored back onto HVAC_BUTTONS: that array is a module
    // constant, so two of these cards on one dashboard would have the second
    // overwrite the first's element references and the active-mode highlight
    // would land on the wrong card.
    this._modeBtns = HVAC_BUTTONS.map((b) => {
      const btn = document.createElement("button");
      btn.title = b.label;
      btn.innerHTML = `<svg viewBox="0 0 24 24"><path fill="currentColor" d="${b.icon}"></path></svg>`;
      btn.addEventListener("click", () => this._setHvacMode(b.mode));
      this._modesEl.append(btn);
      return { mode: b.mode, el: btn };
    });
  }

  /* ---------- render ---------- */

  _render() {
    const st = this._hass.states[this._config.entity];
    if (!st || st.state === "unavailable") {
      this._bodyEl.hidden = true;
      this._unavailableEl.hidden = false;
      this._nameEl.textContent = this._config.name || this._config.entity;
      return;
    }
    this._bodyEl.hidden = false;
    this._unavailableEl.hidden = true;
    this._nameEl.textContent =
      this._config.name || st.attributes.friendly_name || this._config.entity;

    const t = this._target();
    const shown = this._pending !== null ? this._pending : t.value;
    const settable = (t.kind === "temperature" || t.kind === "fan") && !t.disabled;

    this._containerEl.style.setProperty("--state-color", t.color || "var(--disabled-color)");
    this._containerEl.style.setProperty("--action-color", t.disabled ? "" : t.color || "");

    this._dial.mode = SLIDER_MODES[st.state] || "full";
    this._dial.disabled = Boolean(t.disabled) || t.kind === "none";
    this._dial.inactive = Boolean(t.disabled);
    if (settable || t.kind === "temperature") {
      this._dial.min = t.min;
      this._dial.max = t.max;
      this._dial.step = t.step;
      this._dial.value = shown === null ? undefined : shown;
      this._dial.current = t.current;
    } else {
      this._dial.value = undefined;
      this._dial.current = undefined;
    }

    this._labelEl.textContent = t.label || "";
    this._stateEl.textContent =
      t.kind === "fan" && shown !== null && shown !== undefined
        ? t.modes[shown]
        : t.kind === "temperature" && shown !== null && shown !== undefined
          ? `${shown} °C`
          : t.display;

    const cur = st.attributes.current_temperature;
    this._secondaryEl.textContent =
      cur === undefined || cur === null ? "" : `Currently ${cur} °C`;

    this._buttonsEl.hidden = !settable;
    this._minusEl.disabled = !settable;
    this._plusEl.disabled = !settable;

    for (const b of this._modeBtns) {
      b.el.setAttribute("aria-pressed", String(st.state === b.mode));
    }
  }
}

// Home Assistant swaps window.customElements for the scoped-custom-element-
// registry polyfill while it boots. This file is fetched in parallel with the
// frontend bundle, so on a slow link it can win the race and define the card in
// the *native* registry -- which the polyfill then knows nothing about, and
// Lovelace renders "Custom element doesn't exist". Waiting for an element the
// frontend itself defines puts us in whichever registry Lovelace will query.
customElements.whenDefined("home-assistant").then(() => {
  if (!customElements.get("truma-climate-dial-card")) {
    customElements.define("truma-climate-dial-card", TrumaClimateDialCard);
  }
});

window.customCards = window.customCards || [];
window.customCards.push({
  type: "truma-climate-dial-card",
  name: "Truma climate dial",
  description:
    "Thermostat dial that sets the temperature while heating and the fan speed while venting.",
});
