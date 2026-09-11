# Lovelace-Beispiel für Truma iNet X

[English version](#english-version)

`truma-controls.yaml` enthält den vollständigen Truma-Bedienbereich als eine
einzige `vertical-stack`-Karte. Er verwendet ausschließlich native
Home-Assistant-Karten und die Thermostatkarte, die von dieser Integration
automatisch ausgeliefert wird, sowie deren Vorgangsfeedback-Karte.

## Voraussetzungen

- Home Assistant 2024.12 oder neuer
- HACS mit dem benutzerdefinierten Repository
  `https://github.com/rpodgorny/hass-truma-inetx`
- installierte Integration **Truma iNet X (BLE)**
- erfolgreich gekoppeltes Truma-iNet-X-Bedienteil
- ein aktiver ESPHome-Bluetooth-Proxy in Reichweite wird empfohlen

Es werden **keine zusätzlichen Lovelace- oder HACS-Karten** benötigt. Die Karte
`custom:truma-climate-dial-card` gehört zur Integration und wird beim Start von
Home Assistant automatisch registriert. Dasselbe gilt für
`custom:truma-operation-card`: Sie umschließt vorhandene Karten und zeigt
laufende Vorgänge bzw. Fehler. Der zugehörige Sensor **Vorgang** muss unter
`operation_entity` mit seiner tatsächlichen Entity-ID eingetragen werden.
Bei reduzierter Bewegung bleibt die Hervorhebung statisch.
[Benutzeranleitung](../../docs/user-guide.md).

Die beiden Status-Badges sind absichtlich getrennt: **BLE-Sender-Verbindung** zeigt,
ob der für dieses Bedienteil erkannte ESPHome-Proxy in Home Assistant online
ist. **BLE-Truma-Verbindung** zeigt dagegen nur die aktuelle Verbindung zum
Truma-Bedienteil. Bei einem Abfrageintervall größer 0 ist daher normalerweise
der Proxy online, während die BLE-Verbindung zwischen zwei Abfragen aus ist.

## Entity-IDs anpassen

Das Beispiel verwendet als Platzhalter den Gerätepräfix
`truma_inetx_ffb4d1`. Die tatsächlichen Entity-IDs stehen unter:

Einstellungen → Geräte & Dienste → Truma iNet X (BLE) → Gerät

Vor dem Einfügen in `truma-controls.yaml` alle Vorkommen von
`truma_inetx_ffb4d1` durch den eigenen Präfix ersetzen. Beispiel:

```text
climate.truma_inetx_ffb4d1
                └──────┘ ersetzen
```

Die Buttons enthalten den Präfix jeweils zweimal: bei `entity` und unter
`target.entity_id`. Deshalb immer eine globale Ersetzung durchführen.

Einige Entity-IDs werden bei ihrer ersten Anlage aus der aktiven Sprache von
Home Assistant erzeugt. Das Beispiel verwendet die IDs einer deutsch
eingerichteten Installation. Wenn ein eigener Suffix abweicht, muss die
betroffene vollständige Entity-ID ebenfalls ersetzt werden. Maßgeblich ist
immer die auf der Truma-Geräteseite angezeigte ID.

Die Karte verwendet **Energiequelle** als zentrale Auswahl. Beim Wechsel auf
Elektro oder Hybrid aktiviert die Integration den Heizstab zunächst mit
900 W; anschließend kann **Elektrische Heizleistung** auf 1800 W gestellt
werden. Bei Diesel ist diese Leistungswahl automatisch deaktiviert. Der alte
Dieselbrenner-Schalter wurde durch die gemeinsame Energiequellen-Auswahl ersetzt.
Bei nur einer erkannten Energiequelle sind beide Felder deaktiviert; es gibt
keine zusätzliche Aus/Diesel-Auswahl.

## In ein Storage-Dashboard importieren

1. Gewünschtes Dashboard öffnen und **Dashboard bearbeiten** wählen.
2. **Karte hinzufügen** und anschließend **Manuell** auswählen.
3. Den gesamten Inhalt von [`truma-controls.yaml`](truma-controls.yaml)
   einfügen.
4. Den Beispielpräfix wie oben beschrieben ersetzen.
5. Vorschau prüfen und speichern.

Die Karte kann in einer normalen Ansicht oder in einer Section verwendet
werden. Ihre inneren Elemente ordnen sich responsiv an.

## Optionale Hardware

Nicht jedes Fahrzeug meldet alle Entitäten. Fehlt eine Entität, ihren gesamten
`custom:truma-operation-card`-Block einschließlich der enthaltenen Tile-Karte
entfernen, nicht nur die innere Karte. Ein vorhandenes, aber wegen nur einer
Energiequelle deaktiviertes Feld bleibt dagegen stehen. Bei Gas/Elektro die
elektrische Leistungswahl beibehalten: sie bietet separat Aus / 900 W / 1800 W
an, soweit vom Panel unterstützt. Die Integration erzeugt optionale Entitäten erst,
nachdem die Hardware den jeweiligen Parameter gemeldet hat.

## Live-Modus

- `0 Minuten`: einmal sofort synchronisieren und anschließend trennen.
- `1–999 Minuten`: Verbindung für die gewählte Dauer halten.
- **Live-Modus beenden**: gehaltene Verbindung vorzeitig freigeben.

Die Integrationsoption `poll_interval_seconds: 0` ist davon unabhängig und
bedeutet weiterhin dauerhaft verbunden bleiben.

---

<a id="english-version"></a>

# Lovelace example for Truma iNet X

`truma-controls.yaml` contains the complete Truma control area as one
`vertical-stack` card. It only uses built-in Home Assistant cards and the
thermostat and operation-feedback cards served automatically by this integration.

## Requirements

- Home Assistant 2024.12 or newer
- HACS with the custom repository
  `https://github.com/rpodgorny/hass-truma-inetx`
- the **Truma iNet X (BLE)** integration installed
- a successfully paired Truma iNet X panel
- an active ESPHome Bluetooth proxy within range is recommended

No additional Lovelace or HACS cards are required. The integration registers
`custom:truma-climate-dial-card` and `custom:truma-operation-card` automatically
when Home Assistant starts. Set `operation_entity` to your actual **Operation**
sensor ID. The feedback wrapper highlights the matching running operation,
shows errors, and respects reduced-motion preferences.
[User guide](../../docs/user-guide.md#english).

The two status badges deliberately report different links. **BLE transmitter connection**
shows whether the ESPHome proxy identified for this panel is online in Home
Assistant. **BLE Truma connection** only reports the current link to the Truma panel.
With a non-zero poll interval, the proxy is therefore normally online while the
panel link is off between polls.

## Replace the entity prefix

The example uses `truma_inetx_ffb4d1` as a placeholder device prefix and the
entity IDs generated by a German Home Assistant installation. Find the real
entity IDs under Settings → Devices & services → Truma iNet X (BLE) → Device,
then replace every occurrence of `truma_inetx_ffb4d1` in the YAML. If an entity
suffix differs because it was initially created in another language, replace
that complete entity ID as well.

The button cards contain the prefix twice, in `entity` and
`target.entity_id`, so use a global replacement.

## Import into a storage dashboard

1. Open the target dashboard and select **Edit dashboard**.
2. Select **Add card**, then **Manual**.
3. Paste the complete contents of [`truma-controls.yaml`](truma-controls.yaml).
4. Replace the example entity prefix.
5. Check the preview and save.

Remove the entire `custom:truma-operation-card` wrapper, including its inner
tile, for optional entities the heater does not expose. Keep fields that exist
but are disabled because only one energy source is detected. Gas/Electric
heaters retain standalone Off / 900 W / 1800 W electric-output control,
as supported by the panel.
The **Energy source** select coordinates Diesel / Electric / Hybrid. Electric
and Hybrid always start at 900 W; the **Electric heating output** select then
offers 900 W or 1800 W and is disabled again in Diesel mode. The legacy diesel
switch has been replaced by the combined energy-source select.
When only one energy source is detected, both fields are disabled; no additional
Off/Diesel selector is offered.

## Live mode

- `0 minutes`: perform one immediate sync, then disconnect.
- `1–999 minutes`: keep the connection for the selected duration.
- **End live mode**: release an active live connection early.

The integration option `poll_interval_seconds: 0` is separate and continues to
mean stay connected permanently.
