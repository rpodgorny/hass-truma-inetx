const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const registered = new Map();
// Minimal DOM doubles for lifecycle tests; browser focus/CSS need live checks.
function element() {
  const classes = new Set();
  return {
    children: [],
    classList: {
      toggle: (name, value) => value ? classes.add(name) : classes.delete(name),
      contains: name => classes.has(name),
    },
    setAttribute(name, value) { this[name] = value; },
    append(child) { this.children.push(child); },
  };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
// Drain promise callbacks without wall-clock sleeps.
const flushPromises = () => new Promise(resolve => setImmediate(resolve));
const context = {
  HTMLElement: class {
    attachShadow() {
      this.shadowRoot = {
        set innerHTML(value) { this.control = element(); this.status = element(); },
        querySelector(selector) { return selector === '.control' ? this.control : this.status; },
      };
      return this.shadowRoot;
    }
  },
  customElements: { whenDefined: () => Promise.resolve(), get: name => registered.get(name), define: (name, cls) => registered.set(name, cls) },
  window: {}, console, setTimeout, clearTimeout,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('custom_components/truma_inetx/frontend/truma-climate-dial-card.js', 'utf8'), context);
(async () => {
  await Promise.resolve();
  const Card = registered.get('truma-operation-card');
  assert.ok(Card, 'operation card must register');
  const card = Object.create(Card.prototype);
  card._config = { operation_entity: 'sensor.operation', action: 'sync' };
  card._hass = { language: 'de', states: {} };
  assert.equal(card._feedback().busy, false);
  assert.equal(card._feedback().text, 'Vorgangsstatus nicht verfügbar');
  card._hass.states['sensor.operation'] = { state: 'syncing', attributes: { action: 'sync' } };
  assert.equal(card._feedback().busy, true);
  assert.equal(card._feedback().text, 'Synchronisation läuft …');
  card._hass.states['sensor.operation'] = { state: 'changing', attributes: { action: 'energy_source', target: 'electric' } };
  assert.equal(card._feedback().busy, false, 'other control must not pulse');
  assert.equal(card._feedback().text, '');
  card._config.action = ['hvac_mode', 'energy_source'];
  assert.equal(card._feedback().busy, true);
  card._config.action = 'energy_source';
  assert.equal(card._feedback().text, 'Wird umgestellt …');
  assert.equal(card._feedback().busy, true);
  card._hass.states['sensor.operation'] = { state: 'error', attributes: { action: 'energy_source', error: 'Panel timeout' } };
  assert.equal(card._feedback().busy, false);
  assert.equal(card._feedback().error, true);
  assert.equal(card._feedback().text, 'Fehler: Panel timeout');
  card._hass.states['sensor.operation'].state = 'idle';
  assert.equal(card._feedback().text, '');
  card._config.action = undefined;
  assert.equal(card._feedback().text, 'Bereit');
  card._hass.language = 'en';
  assert.equal(card._feedback().text, 'Ready');
  card._child = { getCardSize: () => Promise.resolve(4) };
  assert.equal(await card.getCardSize(), 5, 'asynchronous child sizing must work');
  card._child = { getCardSize: () => 4 };
  assert.equal(await card.getCardSize(), 5, 'synchronous child sizing must still work');
  card._child = null;
  assert.equal(await card.getCardSize(), 2, 'absent child uses the default size');
  const classes = new Set();
  card._control = { classList: { toggle: (name, value) => value ? classes.add(name) : classes.delete(name) }, setAttribute: (key, value) => { card._control[key] = value; } };
  card._status = { classList: { toggle() {} } };
  card._hass.states['sensor.operation'] = { state: 'changing', attributes: { action: 'energy_source' } };
  card._renderFeedback();
  assert.ok(!classes.has('busy'), 'status-only card must not animate an empty container');
  card._config.card = { type: 'button' };
  card._renderFeedback();
  assert.equal(card._control['aria-busy'], 'true');
  assert.ok(classes.has('busy'));
  card._hass.states['sensor.operation'] = { state: 'error', attributes: { action: 'energy_source', error: '<img src=x onerror=alert(1)>' } };
  card._renderFeedback();
  assert.equal(card._control['aria-busy'], 'false');
  assert.ok(!classes.has('busy'));
  assert.equal(card._status.textContent, 'Error: <img src=x onerror=alert(1)>');
  assert.equal(card._status.innerHTML, undefined, 'error must be text, not markup');
  card._buildError = 'Could not load card';
  card._hass.states['sensor.operation'].state = 'syncing';
  card._renderFeedback();
  assert.equal(card._status.textContent, 'Could not load card');
  assert.ok(!classes.has('busy'));
  const code = fs.readFileSync('custom_components/truma_inetx/frontend/truma-climate-dial-card.js', 'utf8');
  assert.match(code, /prefers-reduced-motion: reduce/);
  assert.match(code, /role="status"/);

  const pending = [];
  context.window.loadCardHelpers = () => {
    const request = deferred();
    pending.push(request);
    return request.promise;
  };
  const created = [];
  const helpers = {
    createCardElement(config) {
      const child = { type: config.type };
      created.push(child);
      return child;
    },
  };
  const lifecycle = new Card();
  lifecycle.hass = { language: 'en', states: {} };
  lifecycle.setConfig({ operation_entity: 'sensor.operation', card: { type: 'old' } });
  lifecycle.setConfig({ operation_entity: 'sensor.operation', card: { type: 'new' } });
  const latestHass = {
    language: 'en',
    states: { 'sensor.operation': { state: 'changing', attributes: { action: 'energy_source' } } },
  };
  lifecycle.hass = latestHass;
  pending[1].resolve(helpers);
  await flushPromises();
  const child = lifecycle._child;
  const control = lifecycle._control;
  assert.equal(child.type, 'new');
  assert.equal(child.hass, latestHass, 'late child receives the latest hass');
  pending[0].resolve(helpers);
  await flushPromises();
  assert.equal(created.length, 1, 'stale resolution must not even construct a child');
  assert.equal(lifecycle._child, child, 'stale resolution must not replace the current child');
  assert.deepEqual(control.children, [child]);

  const idleHass = {
    language: 'en',
    states: { 'sensor.operation': { state: 'idle', attributes: {} } },
  };
  assert.equal(control.classList.contains('busy'), true);
  lifecycle.hass = idleHass;
  assert.equal(lifecycle._control, control, 'hass updates must preserve the container');
  assert.equal(lifecycle._child, child, 'hass updates must preserve child identity');
  assert.deepEqual(control.children, [child], 'hass updates must not append duplicate children');
  assert.equal(child.hass, idleHass, 'existing child receives state updates');
  assert.equal(control.classList.contains('busy'), false, 'feedback still updates around the child');

  lifecycle.setConfig({ operation_entity: 'sensor.operation', card: { type: 'stale' } });
  lifecycle.setConfig({ operation_entity: 'sensor.operation' });
  pending[2].reject(new Error('stale rejection'));
  await flushPromises();
  assert.equal(lifecycle._buildError, null, 'stale rejection must not poison the new config');
  assert.equal(lifecycle._child, null);
  assert.deepEqual(lifecycle._control.children, []);
  assert.equal(lifecycle._status.textContent, 'Ready');
  console.log('operation card tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
