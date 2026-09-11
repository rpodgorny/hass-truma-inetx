# Manual Truma Live Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an immediate BLE refresh and a dashboard-controlled 0–999 minute live mode without writing a heater setting.

**Architecture:** The Truma coordinator owns wake and hold state. Restored number and button entities expose that state to Home Assistant; the existing polling session performs all BLE work and remains responsible for disconnect cleanup.

**Tech Stack:** Python asyncio, Home Assistant entity platforms, standalone repository tests, Lovelace sections dashboard, HACS custom repository.

**Spec:** `docs/plans/2026-09-11-manual-live-mode-design.md`

## Global Constraints

- Accept only whole minutes from 0 through 999.
- Zero performs one refresh and does not create a timed hold.
- A manual stop must not interrupt an in-flight Truma write.
- Existing Truma entities and poll-interval behavior must remain compatible.
- All production behavior is preceded by a failing regression test.

---

### Task 1: Coordinator wake and hold behavior

**Files:**
- Create: `tests/test_manual_live_mode.py`
- Modify: `custom_components/truma_inetx/coordinator.py`

**Interfaces:**
- Produces: `async_request_manual_session(minutes: int) -> None`
- Produces: `async_end_manual_session() -> None`
- Produces: `manual_session_active: bool`

- [ ] Write tests that request 0 minutes, request and extend a timed hold, end a hold, preserve pending writes, and select short reconnect backoff while live mode is active.
- [ ] Run `python3 tests/test_manual_live_mode.py` and confirm failures identify the missing coordinator interface.
- [ ] Implement one-shot wake state, post-connect hold deadlines, early release, and reconnect timing.
- [ ] Run `python3 tests/test_manual_live_mode.py` and confirm every case passes.

### Task 2: Home Assistant controls

**Files:**
- Create: `custom_components/truma_inetx/button.py`
- Create: `tests/test_manual_live_entities.py`
- Modify: `custom_components/truma_inetx/number.py`
- Modify: `custom_components/truma_inetx/__init__.py`
- Modify: `custom_components/truma_inetx/strings.json`
- Modify: `custom_components/truma_inetx/translations/en.json`
- Modify: `custom_components/truma_inetx/icons.json`

**Interfaces:**
- Consumes: coordinator manual-session methods from Task 1.
- Produces: restored `number` entity with range 0–999 and two `button` entities.

- [ ] Write tests for entity range, restoration/default, start forwarding, stop forwarding, disconnected availability, translations, icons, and platform registration.
- [ ] Run `python3 tests/test_manual_live_entities.py` and confirm it fails because the controls do not exist.
- [ ] Implement the number and button entities plus metadata and platform registration.
- [ ] Run both new test files and confirm they pass.

### Task 3: Release, regression, and deployment

**Files:**
- Modify: `custom_components/truma_inetx/manifest.json`
- Modify: `README.md`

**Interfaces:**
- Produces: tagged fork release installable as a HACS custom repository.

- [ ] Document the controls, 0-minute semantics, and interaction with poll mode; bump the fork version.
- [ ] Run every `tests/test_*.py` file and Python compilation checks.
- [ ] Review the diff against this plan and obtain an independent code review.
- [ ] Commit, push the feature branch, merge it into the fork's main branch, tag the release, and create GitHub release notes.
- [ ] Back up the live integration files, install the fork release, restart Home Assistant, and verify the new entities and BLE behavior.
- [ ] Add the three controls to the Vital dashboard using optimistic locking, then re-read and render the written dashboard.
