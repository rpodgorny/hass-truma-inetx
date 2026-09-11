# Truma iNet X – Benutzeranleitung

[English](#english)

## Verbindung und Aktualisierung

- **BLE-Sender-Verbindung**: Home Assistant erreicht den verwendeten ESPHome-Sender.
- **BLE-Truma-Verbindung**: Es besteht tatsächlich eine BLE-Verbindung zum Panel.
- **Jetzt synchronisieren / Live-Modus starten**: sofort eine Aktualisierung anfordern.
- **Live-Modus-Dauer 0 Minuten**: einmal synchronisieren, danach wieder freigeben. Nicht unendlich.
- **1–999 Minuten**: Verbindung für die eingestellte Dauer halten.
- **Live-Modus beenden**: vorzeitig freigeben.

Das normale Abfrageintervall ist davon unabhängig; `poll_interval_seconds: 0`
bedeutet dauerhaft verbunden. Nach einem Steuerbefehl bleibt die Verbindung
mindestens 60 Sekunden offen, weitere Befehle verlängern diesen Nachlauf.
Funkabbrüche können die Verbindung unabhängig davon beenden.

## Energiequelle

**Diesel**, **Elektro** oder **Hybrid** wählen. Elektro und Hybrid beginnen mit
900 W. Anschließend kann die elektrische Heizleistung angepasst werden.
Bei Diesel ist die elektrische Leistungswahl deaktiviert. Die Energiequelle
allein schaltet die Raumheizung nicht ein: dazu die Betriebsart und die
gewünschte Temperatur einstellen.

Diese kombinierte Auswahl gilt für Dieselheizungen mit elektrischem Heizelement.
Bei **nur einer erkannten Energiequelle** sind Energiequellen-Auswahl und
elektrische Leistungswahl beide deaktiviert. Ein-/Ausschalten erfolgt über die
Raumheizung, nicht über eine zusätzliche Aus/Diesel-Auswahl. Bei Gas/Elektro
bleibt die separate elektrische Leistungswahl einschließlich **Aus** erhalten,
soweit das Panel dies anbietet; eine Gas-Umschaltung wird nicht vorgetäuscht.

Während der Transaktion erscheint **Wird umgestellt …**. Erst die Rückmeldung
des Panels bestätigt den neuen Zustand. Ein temporärer Hybrid-Zustand während
zweier zusammengehöriger Schreibbefehle wird nicht als abgeschlossene
Umstellung angezeigt. Ein tatsächlich extern gewählter Hybrid-Modus bleibt
hingegen sichtbar. Bestätigte Energiequellen-Wechsel stehen zusätzlich im
Aktivitätsprotokoll. Bei einem Fehler kann ein Teil der Änderung bereits
übernommen sein; den zurückgelesenen Istzustand prüfen, nicht von einem
automatischen Zurücksetzen ausgehen.

## Vorgangsfeedback im Dashboard

Der **Vorgang**-Sensor ist getrennt von den beiden Verbindungssensoren.
**Synchronisation läuft …**, **Wird umgestellt …**, **Bereit** und **Fehler**
beschreiben den Auftrag, nicht den Zustand des Heizbrenners.
Ein Befehlsfehler bleibt auch bei anschließenden Hintergrundabfragen sichtbar;
ein erfolgreich bestätigter neuer Steuerbefehl löst ihn ab.

Die mitgelieferte `custom:truma-operation-card` kann eine vorhandene Karte
umschließen. Sie zeigt Text und ein dezentes Pulsieren nur bei einem passenden
laufenden Auftrag. Bei reduzierter Bewegung in den Systemeinstellungen bleibt
ein statischer Rahmen. Fehler werden als Text dargestellt, ohne weiterzupulsieren.
Die Animation ist keine Fortschrittsanzeige in Prozent.

```yaml
type: custom:truma-operation-card
operation_entity: sensor.DEIN_GERAET_vorgang
action: energy_source
card:
  type: tile
  entity: select.DEIN_GERAET_energiequelle
  features:
    - type: select-options
```

`action` kann auch eine Liste sein, etwa `[hvac_mode, temperature, fan_level]`
für eine Thermostatkarte. `action` weglassen, um alle Vorgänge zu zeigen. Ohne `card` entsteht eine reine
Statusanzeige. Entity-IDs auf der eigenen Geräteseite nachsehen; sie können
sprachabhängig abweichen. [Vollständiges Importbeispiel](../examples/lovelace/README.md).

Wurde das Dashboard bereits während des Home-Assistant-Neustarts geöffnet und
zeigt Kartenfehler, nach vollständig beendetem Start die Seite neu laden.
Die mitgelieferten Karten werden beim Start der Integration registriert.

<a id="english"></a>

# Truma iNet X – User guide

## Connections and refresh

**BLE transmitter connection** is the HA-to-ESPHome link; **BLE Truma connection**
is the actual connection to the panel. **Sync now / start live mode** requests an
immediate refresh. A live duration of **0 minutes means one sync, not infinity**;
1–999 minutes keeps the connection for that duration. **End live mode** releases
it early. The separate integration option `poll_interval_seconds: 0` means
permanently connected. Commands renew a minimum 60-second hold; radio failures
can still interrupt it.

## Energy source and operation feedback

Choose Diesel, Electric or Hybrid. Electric/Hybrid start at 900 W; adjust electric
output afterwards. Electric output is disabled in Diesel mode. Choosing a source
does not itself start room heating; select the heating mode and target separately.

The combined selector applies to diesel heaters with an electric element.
With **only one detected source**, both source and electric-output controls are
disabled. Use room-heating controls to turn heating on or off, not an extra
Off/Diesel selector. Gas/Electric heaters retain standalone electric output,
including **Off**, as supported by the panel; no gas-source switching is implied.

**Changing …** is displayed until the energy transaction finishes. Intermediate
Hybrid readings from the two writes are not presented as a completed selection.
An externally selected actual Hybrid mode is still reported. Confirmed changes
also appear in device activity. On failure a partial change may have reached the
panel: inspect the read-back state rather than assuming an automatic rollback.

The **Operation** sensor is separate from connection and burner status. The
last command error remains visible across background refreshes until a new
control command succeeds. The
bundled `custom:truma-operation-card` wraps an existing card and renders text and
a subtle pulse for its matching operation. Reduced-motion settings replace the
pulse with a static outline; errors remain text without animation. This is not
a percentage progress indicator. Use the YAML above with your own entity IDs.
`action` also accepts a list, e.g. `[hvac_mode, temperature, fan_level]` for a
thermostat. Omit `action` to show all operations, or omit `card` for a status-only display.
[Complete import example](../examples/lovelace/README.md).

If the dashboard was opened while Home Assistant was still restarting and shows
card errors, reload it after startup completes. The bundled cards are registered
when the integration starts.
