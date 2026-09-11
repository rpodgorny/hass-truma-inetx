# Manual refresh and timed live mode design

## Goal

Let a dashboard user wake the Truma BLE session immediately, refresh all values,
and optionally keep the connection open for a user-selected 0–999 minutes. Zero
means one complete refresh followed by the normal prompt disconnect. A second
button ends live mode early.

## Architecture

The existing coordinator remains the sole owner of the BLE client. New public
coordinator methods request a session and release a timed hold; entity platforms
never touch the BLE client or private asyncio events directly.

The number platform adds a restored, local-only duration entity. It stores the
chosen number of minutes in Home Assistant and mirrors it into the coordinator.
The button platform adds `Sync now / start live mode` and `End live mode`.

The coordinator treats a manual request as a one-shot wake source, separate from
writes. Once startup has completed, it starts the requested hold period. During
that period an unexpected disconnect uses the short reconnect backoff. At expiry
the normal poll-mode quiet-time disconnect applies. A manual stop releases the
hold without interrupting an in-flight write.

## Safety and compatibility

- Valid duration is an integer from 0 through 999 minutes.
- The feature is active only in non-zero poll mode; permanent-connect mode keeps
  its existing semantics.
- No heater setting is written merely to refresh data.
- Existing entity IDs and config-entry data remain unchanged.
- The new controls stay available while the panel is disconnected.
- The integration version is raised so HACS and the frontend cache can identify
  the fork build.
- Installation is performed from `tomac01/hass-truma-inetx`; upstream remains a
  separate Git remote and can be merged deliberately.

## Dashboard

The Vital view receives a compact Truma control row directly below its heading:
the duration number, the start/sync button, and the stop button. The existing
misleading BLE badge is relabelled by context rather than used as proof that a
physical link is currently open.

## Verification

Offline tests cover immediate wake, zero-minute refresh, timed hold, extension,
early release, reconnect timing, range enforcement, and entity metadata. Live
verification checks entity creation, a zero-minute refresh, a short timed hold,
clean release, retained sensor availability, logs, and the rendered dashboard.
