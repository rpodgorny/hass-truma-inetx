# Truma iNet X (BLE) – Home-Assistant-Integration

[English version](#english-version)

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validierung](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml/badge.svg)](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml)
[![Lizenz: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Lokale Push-Integration für das Bedienteil **Truma iNet X** über Bluetooth LE.
Sie liest Raum-, Wasser- und Innentemperatur sowie Versorgungsspannung und
steuert Heizmodus, Solltemperatur, Warmwasser, Energiequelle, elektrische
Heizleistung und Lüfter – ohne Cloud, Truma-Konto oder LIN-Verkabelung.

Zusätzliche Entitäten ermöglichen eine sofortige BLE-Aktualisierung und einen
zeitlich begrenzten Live-Modus.

Nach jedem Steuerbefehl bleibt die BLE-Verbindung mindestens **60 Sekunden**
offen. Jeder weitere Befehl verlängert diese Zeit erneut; eine längere
Live-Modus-Dauer gilt zusätzlich. „Live-Modus beenden“ gibt die Verbindung
ausdrücklich frei. Funkabbrüche können weiterhin eine Wiederverbindung erfordern.
Beim Wechsel der Energiequelle wartet die Integration auf die Rückmeldung
der Heizung. Elektro und Hybrid beginnen mit **900 W**. Bleibt die Bestätigung
aus, wird ein Fehler angezeigt; eine Transportbestätigung allein zählt nicht
als erfolgreiche Übernahme. Nach einer Wiederverbindung werden Befehle erst
nach Abschluss der Geräteabfrage gesendet.
Antwortet die Heizung noch nicht, werden Energiequellen-Sollwerte höchstens
dreimal gesendet und jeweils anhand frischer Rückmeldungen geprüft.

Die erste Synchronisierung beginnt direkt nach dem Start, auch ohne vorhandene
Messwerte. BLE-Teilpakete werden vollständig zusammengesetzt, bevor die Daten
ausgewertet werden. Bestätigte Energiequellen-Wechsel erhalten einen zusätzlichen
Eintrag „Von der Truma bestätigt“ in der Geräteaktivität. Technische Details und
Regressionstests: [BLE-Transport](docs/ble-transport.md).

Der Sensor **Vorgang** zeigt laufende Synchronisationen und Änderungen sowie
Fehler an. Während eines Energiequellen-Wechsels erscheint **Wird umgestellt …**
anstelle eines vorübergehenden Hybrid-Zustands. Die optionale Dashboard-Karte
`custom:truma-operation-card` zeigt den Status und hebt die betroffene Steuerung
dezent hervor. [Benutzeranleitung (Deutsch / English)](docs/user-guide.md) ·
[Importierbare Dashboard-Karten](examples/lovelace/README.md).

Entwickelt wurde die Integration mit einem iNet X an einer **Truma Combi**.
Andere Truma-Geräte sprechen dasselbe Protokoll, sind aber nicht getestet;
Erfahrungsberichte sind willkommen.

## Ein ESP32-Bluetooth-Proxy ist der zuverlässige Weg

Ein Proxy funktioniert unabhängig vom Linux-Kernel. Ein lokaler
Bluetooth-Adapter funktioniert nur mit bestimmten Kernel-Versionen.

Das Bedienteil sendet mit einer **schnell wechselnden Resolvable Private
Address (RPA)**. Eine verschlüsselte Wiederverbindung gelingt nur, wenn der
Client die aktuelle Adresse verwendet. Smartphones lösen sie im
Bluetooth-Controller auf; ESP-IDF macht dasselbe. Daher funktioniert ein
[ESPHome-Bluetooth-Proxy](https://esphome.io/components/bluetooth_proxy.html)
zuverlässig.

Unter Linux hängt das Verhalten von der Kernel-Version ab:

- **Vor 6.19** funktioniert die Wiederverbindung über einen lokalen Adapter.
  `hci_connect_le()` ersetzt die Identitätsadresse vor dem Verbindungsaufbau
  durch die zwischengespeicherte RPA. LL Privacy und ein Proxy sind deshalb
  nicht erforderlich. Kernel 6.12 des Pi 5 aus
  [#13](https://github.com/rpodgorny/hass-truma-inetx/issues/13) gehört dazu;
  `14b06c3a88f7` wurde nicht in die stabilen Reihen 6.12, 6.17 oder 6.18
  zurückportiert.
- **Ab 6.19** funktioniert das meist nicht. Commit
  [`14b06c3a88f7`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=14b06c3a88f7)
  reicht die Identitätsadresse bis zum Controller durch; das Bedienteil hört
  den Verbindungsversuch dadurch nicht. Ein lokaler Adapter funktioniert dann
  nur, wenn der Controller LL Privacy beherrscht und BlueZ den IRK des
  Bedienteils in seine Auflösungsliste eingetragen hat. Bei Dual-Mode-Bonds
  geschieht das derzeit nicht
  ([bluez#2356](https://github.com/bluez/bluez/issues/2356)).

Eine Kernel-Korrektur wurde an
[linux-bluetooth gesendet](https://lore.kernel.org/linux-bluetooth/20260908012048.3681904-2-radek@podgorny.cz/)
und befindet sich im Upstream-Prozess. Bis sie verfügbar ist, sollte ab Kernel
6.19 ein Proxy verwendet werden.

Ältere Berichte in diesem Repository behaupteten, BlueZ könne grundsätzlich
keine Verbindung herstellen. Das war falsch: Die getesteten Adapter liefen
auf Kerneln mit der beschriebenen Regression.

**Die Standard-Proxy-Firmware genügt.** Es ist keine angepasste Firmware
erforderlich. Die Integration erwartet lediglich einen aktiven
`bluetooth_proxy` in einem `esp-idf`-Build:

```yaml
esp32:
  framework:
    type: esp-idf   # erforderlich: mehr Verbindungsplätze und RPA-Auflösung im Controller

bluetooth_proxy:
  active: true
```

Der Proxy sollte sich **höchstens wenige Meter vom Bedienteil entfernt**
befinden. Zu große Entfernung zeigt sich als
`ESP_GATT_CONN_FAIL_ESTABLISH`, nicht als eindeutiger Reichweitenfehler.

Wenn die Integration die Werbung des Bedienteils empfängt, aber wiederholt
keine Verbindung herstellen kann, legt sie unter Einstellungen →
**Reparaturen** einen Hinweis an. Ist das Bedienteil lediglich ausgeschaltet
oder außer Reichweite, bleibt sie still. Nach der nächsten erfolgreichen
Verbindung wird der Hinweis automatisch entfernt.

## Entitäten

| Entität | Plattform | Hinweise |
|---|---|---|
| Truma iNet X | `climate` | Alle vom Bedienteil angebotenen Modi: immer Aus, Heizen und Nur Lüften; je nach Fahrzeug zusätzlich Auto, Kühlen oder Trocknen. 5–30 °C in 1-°C-Schritten. Angezeigt wird nur die im aktuellen Modus sinnvolle Regelung: Solltemperatur beim Heizen oder Lüfterstufe (`off`, `1`–`10`) beim Lüften |
| Raumtemperatur | `sensor` | °C |
| Wassertemperatur | `sensor` | °C |
| Innentemperatur | `sensor` | °C |
| Versorgungsspannung | `sensor` | V |
| Warmwasser | `select` | Aus / Eco (40 °C) / Comfort (60 °C) / Hot (70 °C), soweit vom Bedienteil angeboten |
| Energiequelle | `select` | Bei Dieselheizungen mit elektrischem Heizelement: Diesel / Elektro / Hybrid. Bei nur einer erkannten Energiequelle deaktiviert. Elektro und Hybrid starten aus Sicherheitsgründen immer mit 900 W |
| Elektrische Heizleistung | `select` | Bei nur einer erkannten Energiequelle deaktiviert. Bei Diesel/Elektro: 900 W / 1800 W, nur in Elektro- und Hybridbetrieb aktiv. Bei Gas/Elektro bleibt die separate Steuerung mit Aus / 900 W / 1800 W erhalten, soweit vom Panel angeboten |
| Gas | `binary_sensor` | Zeigt, ob die Heizung Gas verwendet. Schreibgeschützt und nur bei Gasheizungen |
| Lüfterstufe | `number` | 0–10 |
| Live-Modus-Dauer | `number` | Ganze Minuten von 0 bis 999 |
| Jetzt synchronisieren / Live-Modus starten | `button` | Verbindet sofort und aktualisiert alle Werte. Bei `0` wird danach regulär getrennt; bei `1`–`999` bleibt die Verbindung entsprechend lange bestehen |
| Live-Modus beenden | `button` | Beendet einen zeitlich begrenzten Live-Modus, ohne einen bereits laufenden Befehl abzubrechen |
| Flamme | `binary_sensor` | Brenner ist aktuell aktiv |
| BLE-Truma-Verbindung | `binary_sensor` | Besteht derzeit die Verbindung zwischen Home Assistant und dem Truma-Bedienteil? Zustand: verbunden oder getrennt |
| BLE-Sender-Verbindung | `binary_sensor` | Besteht die Verbindung zwischen Home Assistant und dem ESP32-Sender, über den dieses Bedienteil zuletzt erreicht wurde? Zustand: verbunden oder getrennt; bis zur ersten erkannten Route unbekannt |
| Vorgang | `sensor` | Bereit / Synchronisation läuft / Wird umgestellt … / Fehler; unabhängig vom Brenner- und Verbindungszustand |
| Frischwasser | `sensor` | %, nur bei vorhandenem Tanksensor |
| Grauwasser | `sensor` | %, nur bei vorhandenem Tanksensor |
| Frischwasserpumpe | `switch` | Nur bei vorhandener Pumpe |
| Warmwasser-Boost | `switch` | `WaterHeating.BoostMode`, nur wenn vom Heizgerät gemeldet |
| Schnelles Wasseraufheizen | `switch` | `WaterHeating.FasterHeatingMode`, nur wenn vom Heizgerät gemeldet |
| Schnellaufheizzeit | `sensor` | Diagnose in Sekunden; nur wenn vom Heizgerät gemeldet |
| Starterbatterie | `sensor` | V, nur wenn `VBat.Voltage` gemeldet wird |
| Aufbaubatterie | `sensor` | V, nur wenn `L1Bat.Voltage` gemeldet wird |
| Flammenstatus | `sensor` | Diagnose, standardmäßig deaktiviert; Rohwert von `System.FlameStatus` |

Die Modusliste der Climate-Entität und die Optionen für Warmwasser und
elektrische Leistung
sind nicht fest vorgegeben. Das Bedienteil beschreibt die Parameter des
konkreten Fahrzeugs. Ein Fahrzeug ohne Klimaanlage erhält deshalb keinen
Kühlmodus; eine Heizung ohne elektrisches Element bietet keine 1800 W an. Wenn
das Bedienteil keine Beschreibung liefert, verwendet die Integration die
vollständige Fallback-Liste. Die vom Bedienteil in seiner Anzeigesprache
gelieferten Namen werden nicht direkt angezeigt, damit die Oberflächentexte
übersetzbar bleiben.

Alles, was in der Tabelle mit „nur wenn“ gekennzeichnet ist, wird erst als
Entität angelegt, nachdem die entsprechende Hardware einen Wert gemeldet hat.
Fahrzeuge unterscheiden sich stark: Eine Combi D besitzt kein elektrisches
Element, eine Gas-/Elektro-Combi keinen Dieselbrenner und viele Fahrzeuge weder
Tank- noch Elektroblock. Eine dauerhaft unbekannte Entität sähe sonst genauso
aus wie eine defekte Integration.

Gas ist absichtlich ein Sensor und kein Schalter. `EnergySrc.GasLevel` ist zwar
beschreibbar, wird aber auch von der Heizung selbst gesetzt. Bei einer
Gas-/Elektro-Combi wurde beobachtet, dass das Abschalten des Heizelements die
Gasquelle selbstständig aktiviert. Eine Steuerung würde daher gegen das Gerät
arbeiten; der Sensor bildet stattdessen dessen tatsächliche Wahl ab.

Die beiden Warmwasser-Prioritätsschalter bündeln die gesamte Brennerleistung
für den Boiler. Welchen davon das Bedienteil selbst verwendet, ist noch nicht
geklärt. Das rückentwickelte Schema kennt `WaterHeating.BoostMode` und
`WaterHeating.FasterHeatingMode` als getrennte 0/1-Parameter; beim zweiten
steht zusätzlich eine Dauer. Deshalb wird jeder Schalter nur angelegt, wenn
sein eigener Parameter gemeldet wird. Ein Diagnosedownload eines Fahrzeugs,
das einen dieser Werte meldet, würde die offene Frage aus
[#7](https://github.com/rpodgorny/hass-truma-inetx/issues/7) klären.

Die Bedeutung von `System.FlameStatus` ist nicht veröffentlicht. Am Fahrzeug
wurde sie gegen die reale Leistungsaufnahme geprüft: `0` bedeutet aus, `1`
aktiv und `2` Bereitschaft. Der binäre Flammensensor ist deshalb nur bei `1`
eingeschaltet; der Rohwert bleibt als standardmäßig deaktivierter
Diagnosesensor verfügbar.

Die Optionen der Warmwasser-Auswahl wurden in 0.7.1b4 von
`40 °C / 60 °C / 70 °C` auf
`Eco (40 °C) / Comfort (60 °C) / Hot (70 °C)` geändert. Automationen und
Skripte mit den alten Texten für `select.select_option` müssen angepasst
werden; die Werte auf dem Bus sind unverändert.

Wenn ein Fahrzeug die elektrische Auswahl oder den Dieselschalter bereits vor
deren bedingter Erzeugung besaß, behält Home Assistant die alte Entität in der
Registry und zeigt sie eventuell als nicht verfügbar. Sie kann einmalig auf
der Geräteseite gelöscht werden. Die Integration entfernt Entitäten nicht
automatisch, weil „noch nicht gemeldet“ nicht dasselbe bedeutet wie „Hardware
nicht vorhanden“.

Aktualisierungen werden direkt übernommen, wenn das Bedienteil sie sendet
(ungefähr 25 Frames pro Minute). Tankstände sind die Ausnahme: Ein Tanksensor
meldet den zuletzt angeforderten Messwert. Daher fordert die Integration beim
Verbindungsaufbau und während einer gehaltenen Verbindung alle 60 Sekunden
eine neue Messung beim jeweils meldenden Busgerät an.

### Sofortsynchronisierung und Live-Modus

Bei einem Abfrageintervall größer null können die manuellen Entitäten die
Wartezeit vorübergehend übersteuern, ohne das konfigurierte Intervall zu
verändern:

1. **Live-Modus-Dauer** auf eine ganze Zahl zwischen `0` und `999` stellen.
2. **Jetzt synchronisieren / Live-Modus starten** drücken.

Bei `0` stellt die Integration sofort eine Verbindung her, aktualisiert die
Werte und trennt anschließend wieder. `0` bedeutet hier **nicht unendlich**.
Bei `1` bis `999` beginnt die Zeitmessung erst nach dem abgeschlossenen
BLE-Start-Handshake und die Verbindung bleibt für die gewählte Minutenzahl
offen. Bricht sie währenddessen ab, versucht die Integration mit kurzer
Wartezeit erneut zu verbinden.

**Live-Modus beenden** gibt die Verbindung vorzeitig frei. Ein bereits
laufender Schreibbefehl wird zuvor abgeschlossen. Danach gilt wieder das in den
Integrationsoptionen konfigurierte Abfrageintervall.

Davon zu unterscheiden ist `poll_interval_seconds: 0` in den
Integrationsoptionen: Diese bestehende Option bedeutet weiterhin dauerhaft
verbunden bleiben.

Während des Heizens regelt das Bedienteil seinen Lüfter selbst; beim Lüften
existiert dagegen kein Temperatursollwert. Die Climate-Entität bietet deshalb
immer nur die im aktuellen Modus sinnvolle Funktion an. Im ausgeschalteten
Zustand bleibt der Sollwert erhalten. Die separate `number`-Entität stellt die
Lüfterstufe für Automationen in jedem Modus bereit.

## Dashboard-Karte der Integration

Die Integration liefert eine eigene Thermostatkarte mit. Der Drehregler passt
sich dem Modus an: Beim Heizen stellt er die Temperatur ein, beim Lüften die
Lüfterstufe; im ausgeschalteten Zustand ist er deaktiviert. Die Standardkarte
von Home Assistant kann keine numerische Lüfterstufe auf ihrem Temperaturbogen
darstellen.

**Es ist keine zusätzliche Installation erforderlich.** Die Integration stellt
die Karte unter `/truma_inetx/truma-climate-dial-card.js` bereit und registriert
sie automatisch im Frontend:

```yaml
type: custom:truma-climate-dial-card
entity: climate.truma_inetx_ffb4d1
name: Heizung          # optional
```

Ein HACS-Repository kann nur einer Kategorie angehören und deshalb nicht
gleichzeitig als HACS-Plugin veröffentlicht werden. Die Auslieferung aus der
Integration vermeidet ein zweites Repository. Die Integrationsversion wird als
Query-String an die URL angehängt, damit der Frontend-Service-Worker neue
Versionen trotz seines langen Caches lädt.

Der Nachteil: `add_extra_js_url` lädt das etwa 19 KB große Modul bei jedem
Seitenaufruf für jeden Benutzer, auch wenn die Karte nicht angezeigt wird.

Die Karte baut den Regler nicht selbst nach. Sie verwendet Home Assistants
interne Komponenten `ha-control-circular-slider` und
`ha-outlined-icon-button` sowie dessen Layout-CSS. Damit übernimmt sie das
Erscheinungsbild des Frontends, hängt aber auch von nicht stabil garantierten
internen Komponenten ab. Wenn Home Assistant sie umbenennt, zeigt die Karte
eine ausdrückliche Fehlermeldung mit dem fehlenden Komponentennamen.

Ein vollständiger, direkt einfügbarer Bedienbereich mit Live-Modus,
Raumheizung, Warmwasser, Energiequellen und Temperaturen liegt unter
[`examples/lovelace/`](examples/lovelace/). Die dortige Anleitung nennt alle
Voraussetzungen und erklärt die Anpassung der gerätespezifischen Entity-IDs.

## Installation

### HACS als benutzerdefiniertes Repository

[![In HACS öffnen](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rpodgorny&repository=hass-truma-inetx&category=integration)

Alternativ manuell:

1. HACS → ⋮ → **Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/rpodgorny/hass-truma-inetx` mit der Kategorie
   **Integration** hinzufügen.
3. **Truma iNet X (BLE)** installieren und Home Assistant neu starten.
4. Unter Einstellungen → Geräte & Dienste sollte das Bedienteil gefunden
   werden; siehe [Kopplung](#kopplung).

### Symbol

Die Integration enthält eigene Grafiken unter
`custom_components/truma_inetx/brand/` (`icon.png` mit 256×256 und
`icon@2x.png` mit 512×512 Pixeln). Es handelt sich um das Truma-iNet-X-Zeichen
ohne Wortmarke, neu zentriert. Es ist eine **Marke von Truma und nicht von der
GPL-3.0-Lizenz dieses Repositorys erfasst**. Quelle, Änderungen und
Markenhinweis stehen in
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md).

Seit [Home Assistant 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
werden diese Dateien direkt über den Brands-Proxy der Integration ausgeliefert
und haben Vorrang vor dem Brands-CDN. Bei älteren Versionen verwendet die
Oberfläche ein Standardsymbol. Auch HACS kann noch einen Platzhalter anzeigen,
weil es Symbole aus dem HACS-CDN lädt
([hacs/integration#5223](https://github.com/hacs/integration/issues/5223)).

### Manuell

Den Ordner `custom_components/truma_inetx/` nach
`config/custom_components/` der Home-Assistant-Installation kopieren und Home
Assistant neu starten.

## Kopplung

Das Bedienteil verwendet **Just Works** ohne angezeigten Passkey und akzeptiert
eine Bindung nur im aktiven Modus zum Hinzufügen eines Geräts:

1. Das Bedienteil **frisch** über die Truma-iNet-X-App oder direkt am Panel in
   den Modus zum Hinzufügen eines Geräts versetzen.
2. In Home Assistant sollte es als gefundenes Gerät erscheinen. Andernfalls
   Einstellungen → Geräte & Dienste → **Integration hinzufügen** →
   *Truma iNet X (BLE)* öffnen.
3. **Einmal Absenden.** Wiederholtes Absenden gegen ein nicht mehr sauber
   bereites Panel führt zu „Etwas ist schiefgelaufen“; anschließend muss der
   Kopplungsmodus erneut aktiviert werden.

Die Kopplung dauert normalerweise nur wenige Sekunden.

### Wenn die Kopplung fehlschlägt

Vor jedem Versuch den Modus zum Hinzufügen eines Geräts neu aktivieren:

1. **Gespeicherte Bluetooth-Geräteliste des Bedienteils löschen.** Sie nimmt
   nur ungefähr vier Geräte auf und lehnt neue Bindungen bei voller Liste ohne
   eindeutige Meldung ab. Danach den Kopplungsmodus neu aktivieren.
2. **Wenn das Löschen nicht hilft, Bedienteil stromlos neu starten.** Danach
   erneut in den Kopplungsmodus wechseln und den gesamten Vorgang wiederholen.
   Das beendet hängende Verbindungen und erzeugt eine neue Bluetooth-Adresse.

Bindungen auf dem Bluetooth-Proxy müssen nicht gelöscht werden. Hat der Proxy
noch eine vom Bedienteil vergessene Bindung, lehnt das Panel nur diese eine
Adresse mit `error: 97` ab. Die Integration wechselt zur nächsten Adresse und
kann sie normal koppeln.

Für eine spätere erneute Kopplung auf der Geräteseite **Neu konfigurieren**
verwenden.

## Diagnose

Über ⋮ → **Diagnose herunterladen** auf der Geräteseite erhält man den
Konfigurationseintrag, den Erfolg der letzten Aktualisierung und den vollständig
dekodierten Panelzustand. `seen_devices` enthält alle Busadressen, von denen die
Integration Daten empfangen hat, und zeigt damit, welche Geräte tatsächlich am
Fahrzeugbus vorhanden sind.

`param_meta` beschreibt die vom Panel gemeldete Bedeutung eines Parameters:
Bereich, Schreibbarkeit sowie bei Aufzählungen die Namen aller Werte und deren
Verfügbarkeit. Da Truma das Protokoll nicht dokumentiert, beantwortet der Dump
viele Fragen direkt. Dieselben Beschreibungen werden auf Debug-Stufe einmalig
pro Parameter protokolliert.

BLE-Adresse, Panelname und gespeicherte App-Identität (`muid` / `uuid`) werden
geschwärzt. Die Adresse kann trotz ihres privaten Charakters ein Panel einem
Ort zuordnen; die Identität wird für die Bindung verwendet. Der Panelzustand
selbst enthält keine Identifikationsdaten.

## Bekannte Einschränkungen

- **Wiederverbindungen können hängen.** Nach einem Verbindungsabbruch schlägt
  die Verbindung zur selben Adresse manchmal wiederholt mit
  `ESP_GATT_CONN_FAIL_ESTABLISH` (0x3e) fehl. Die Integration wartet und
  wechselt zwischen den gesendeten Adressen. Meist erholt sie sich; gelegentlich
  ist ein Neustart des Bedienteils erforderlich.
- **Doppelte Einträge in der Geräteliste des Bedienteils.** Eine Kopplung kann
  einen zusätzlichen Datensatz hinterlassen. Das ist bislang harmlos, belegt
  aber einen der ungefähr vier Plätze.
- Zur Erkennung werden nur lokaler Name und Service-UUID verwendet. Die
  gespeicherte Adresse gilt als flüchtig, weil sie regelmäßig wechselt.

## Entwicklung

Die Prüfungen unter `tests/` verwenden Stubs für Home Assistant, bleak und dbus
und benötigen weder eine Home-Assistant-Installation noch echte Hardware:

```bash
python3 tests/test_pairing_rotation.py
python3 tests/test_pairing_transport_dispatch.py
python3 tests/test_device_from_bluez.py
python3 tests/test_no_proxy_issue.py
python3 tests/test_water_entities.py
python3 tests/test_energy_entities.py
python3 tests/test_manual_live_entities.py
python3 tests/test_manual_live_mode.py
```

Für die übrigen Tests werden `voluptuous` beziehungsweise `cbor2` benötigt:

```bash
pip install voluptuous
python3 tests/test_panel2_discovery.py

pip install cbor2==5.6.5
python3 tests/test_param_discovery.py
python3 tests/test_measure_request.py
python3 tests/test_param_meta.py
python3 tests/test_panel_declared_options.py
```

## Danksagung und Lizenz

Die Home-Assistant-Integration – Koordinator, BLE-Transport, Kopplung,
Konfigurationsfluss und alle Entitätsplattformen – ist Originalarbeit dieses
Repositorys und steht unter **GPL-3.0**; siehe [LICENSE](LICENSE).

Die Protokollimplementierung in `custom_components/truma_inetx/truma/`
(`protocol.py`, `state.py`, `const.py`) wurde aus
[daaaaan/truma-inetx-ble](https://github.com/daaaaan/truma-inetx-ble)
übernommen. Dessen Rückentwicklung machte diese Integration möglich. Das
Projekt veröffentlicht keine Lizenz; daher behält sein Autor alle Rechte, und
die GPL-3.0 gilt **nicht** für diese Dateien. Sie liegen in einem eigenen
Unterpaket, damit die Grenze sichtbar bleibt.

`protocol.py` ist unverändert übernommen. `state.py` und `const.py` enthalten
lokale Ergänzungen: Frischwasserpumpe, beide Tankstände, `seen_devices`,
`topic_source`, zusätzliche Gerätestarts und Themen der Parametererkennung sowie
Konstanten für Messanforderungen. Diese Ergänzungen sind Originalarbeit dieses
Repositorys, liegen aber in Dateien mit einer anders lizenzierten Basis.

Das Integrationssymbol ist das beschreibend verwendete Truma-iNet-X-Zeichen;
siehe [`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md).
Es ist von der GPL-3.0 ausgenommen.

„Truma“ und das Truma-iNet-X-Zeichen sind Marken der Truma Gerätetechnik GmbH &
Co. KG. Dieses Projekt ist weder mit Truma verbunden noch von Truma empfohlen,
gesponsert oder unterstützt.

---

<a id="english-version"></a>

# Truma iNet X (BLE) — Home Assistant integration

After each control command, the BLE connection stays open for at least
**60 seconds**. Each further command renews this period; a longer live-mode
duration also applies. “End live mode” explicitly releases the connection.
Radio disconnections may still require reconnection. Energy-source changes
wait for heater feedback and enter Electric/Hybrid at **900 W**. Missing
confirmation raises an error; transport acknowledgement alone is not success.
After reconnecting, commands wait until device discovery has completed.
Energy-source setpoints are attempted at most three times while the heater
wakes up, with fresh device feedback checked after each attempt.

Initial synchronization starts immediately after startup, including when no
measurements are available yet. BLE fragments are reassembled before decoding.
Confirmed energy-source changes add a “Confirmed by Truma” device activity entry.
Technical details and regression tests: [BLE transport](docs/ble-transport.md).

The **Operation** sensor reports synchronization, changes and errors. During an
energy-source transaction, **Changing …** replaces transient Hybrid states.
The optional `custom:truma-operation-card` displays operation feedback around
the affected control. [User guide (Deutsch / English)](docs/user-guide.md) ·
[Importable dashboard cards](examples/lovelace/README.md).

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml/badge.svg)](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Local push integration for the **Truma iNet X** control panel over Bluetooth LE.
Reads room/water/internal temperatures and supply voltage, and controls heating
mode, target temperature, water heating, energy source, electric heating output
and the fan — no cloud, no Truma account, no LIN wiring.

Additional entities provide an immediate BLE refresh and a timed live session.

Developed against an iNet X driving a **Truma Combi**. Other Truma appliances
speak the same protocol but are untested; reports welcome.

## An ESP32 Bluetooth proxy is the reliable route

A proxy works everywhere. A local adapter works on some kernels and not
others, and which one you are on decides it.

The panel advertises a **fast-rotating Resolvable Private Address** and only
accepts an encrypted reconnect from a client that puts that current address on
air. Phones resolve it in the Bluetooth controller, and ESP-IDF does the same,
so an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
always works.

On Linux it depends on the kernel version:

- **Below 6.19** a local adapter reconnects fine. `hci_connect_le()`
  substitutes the peer's cached RPA for the identity address before it puts a
  connection on air, so no LL Privacy and no proxy is needed. Kernel 6.12, the
  one the Pi 5 in [#13](https://github.com/rpodgorny/hass-truma-inetx/issues/13)
  runs, is in this range, and `14b06c3a88f7` has not been backported to any
  6.12, 6.17 or 6.18 stable release.
- **6.19 and later** it usually does not. Commit
  [`14b06c3a88f7`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=14b06c3a88f7)
  made the kernel keep the identity address all the way to the controller, so
  the panel never hears the connect. A local adapter then only works if the
  controller supports LL Privacy *and* BlueZ has programmed the panel's IRK
  into its resolving list, which currently does not happen for dual-mode bonds
  ([bluez#2356](https://github.com/bluez/bluez/issues/2356)).

A kernel fix is
[posted to linux-bluetooth](https://lore.kernel.org/linux-bluetooth/20260908012048.3681904-2-radek@podgorny.cz/)
and is working its way upstream. Until it lands, a proxy is the answer on 6.19
and later.

Older reports in this repo claim BlueZ can never do this. That was wrong: the
adapters tested happened to be on kernels carrying the regression.

**Stock proxy firmware is enough** — nothing custom is needed. A plain
`bluetooth_proxy: active: true` on an `esp-idf` build is all this integration
expects:

```yaml
esp32:
  framework:
    type: esp-idf   # required: more connection slots + in-controller RPA resolution

bluetooth_proxy:
  active: true
```

Put the proxy **within a few metres of the panel**. Distance shows up as
`ESP_GATT_CONN_FAIL_ESTABLISH` connect failures rather than as a clean error.

If the integration can hear the panel advertising but cannot connect to it
repeatedly, it raises an issue under Settings → **Repairs** saying so, rather
than leaving the entities unavailable with no explanation. It stays quiet while
the panel is simply switched off or out of range — that is not the same fault —
and clears the issue on the next successful connect.

## Entities

| Entity | Platform | Notes |
|---|---|---|
| Truma iNet X | `climate` | Whichever modes the panel offers — Off / Heat / Fan-only everywhere, plus Auto, Cool or Dry where the vehicle has them. 5–30 °C in 1 °C steps. Offers only the control the current mode uses: the target temperature while heating, the fan speed as the fan mode (`off`, `1`–`10`) while venting |
| Room temperature | `sensor` | °C |
| Water temperature | `sensor` | °C |
| Internal temperature | `sensor` | °C |
| Supply voltage | `sensor` | V |
| Water heating | `select` | Off / Eco (40 °C) / Comfort (60 °C) / Hot (70 °C) — the steps the panel offers |
| Energy source | `select` | Diesel / Electric / Hybrid on diesel heaters with an electric element; disabled when only one energy source is detected. Electric and Hybrid always enter at the safer 900 W level |
| Electric heating output | `select` | Disabled when only one energy source is detected. For Diesel/Electric: 900 W / 1800 W, available only in Electric and Hybrid operation. Gas/Electric retains standalone Off / 900 W / 1800 W control where offered by the panel |
| Gas | `binary_sensor` | Whether the heater is drawing on gas. Read-only — the heater moves this itself. Only where it burns gas |
| Fan level | `number` | 0–10 |
| Live mode duration | `number` | Whole minutes from 0 through 999 |
| Sync now / start live mode | `button` | Connect immediately, refresh all values and stay connected for the selected duration. `0` performs one refresh and disconnects normally |
| End live mode | `button` | Ends a timed live session early without interrupting a command already being sent |
| Flame | `binary_sensor` | Burner currently firing |
| BLE Truma connection | `binary_sensor` | Is the connection between Home Assistant and the Truma panel currently connected? State: connected or disconnected |
| BLE transmitter connection | `binary_sensor` | Is the connection between Home Assistant and the ESP32 transmitter last used for this panel available? State: connected or disconnected; unknown until the first route has been identified |
| Operation | `sensor` | Ready / Synchronizing / Changing … / Error, independent of burner and connection status |
| Fresh water | `sensor` | % — only where the vehicle has a tank sensor |
| Grey water | `sensor` | % — only where the vehicle has a tank sensor |
| Fresh water pump | `switch` | Only where the vehicle has one |
| Water boost | `switch` | `WaterHeating.BoostMode`. Only where the heater reports it |
| Faster water heating | `switch` | `WaterHeating.FasterHeatingMode`. Only where the heater reports it |
| Faster water heating time | `sensor` | Diagnostic, seconds — the duration beside it. Only where the heater reports it |
| Starter battery | `sensor` | V — only where something reports `VBat.Voltage` |
| Leisure battery | `sensor` | V — only where something reports `L1Bat.Voltage` |
| Flame status | `sensor` | Diagnostic, disabled by default — the raw `System.FlameStatus` value |

The climate entity's mode list and the water/electric-output options are not fixed. The
panel enumerates each parameter for the vehicle it is installed in — a van with no air conditioner
does not list a cooling mode, and a heater without the electric element does
not list 1800 W — so the entities offer what the panel offers, falling back to
the full list where it describes nothing. The panel's own names for the values
are never shown: they arrive in the panel's display language, and the labels
here stay translatable.

Everything marked "only where" is created the first time the hardware behind it
reports a value, rather than up front: vehicles differ far more than the
protocol does — a Combi D has no electric element and its panel never mentions
the parameter, a gas/electric Combi has no diesel burner, most vans have no
tanks and no electrical block — and an entity that is permanently unknown
because the hardware does not exist looks exactly like one that is unknown
because the integration is broken.

Gas is deliberately a sensor and not a switch. `EnergySrc.GasLevel` is
writable and the write does go through, but the heater writes it too: on a
gas/electric Combi, switching the electric element off was measured turning the
gas source on by itself. A control over something the appliance also drives
would fight it and flap, so the reading reflects the heater's choice rather
than pretending to make it.

The two water-priority switches are the panel's way of putting the burner's
whole output into the boiler. Which of them the panel's own button writes is
not known: the reverse-engineered schema behind this integration lists
`WaterHeating.BoostMode` and `WaterHeating.FasterHeatingMode` as separate
parameters, both 0/1, the second with a duration in seconds beside it, and no
dump from a vehicle has shown either yet. Both are therefore offered and each
waits for its own parameter, so a heater that reports neither is given neither.
If yours shows one of them, a diagnostics download naming it would settle the
question — see issue #7.

Nothing published defines `System.FlameStatus`, but it has been checked on the
vehicle against real shore-power draw: 0 is off, 1 is firing and 2 is standby.
The binary sensor is therefore on only for 1; the raw value remains available
as a diagnostic sensor that is disabled by default.

The water select's options changed in 0.7.1b4, from `40 °C / 60 °C / 70 °C` to
`Eco (40 °C) / Comfort (60 °C) / Hot (70 °C)`, so that the name matches what the
panel writes on the vehicle and the temperature says what the name means. An
automation or script that calls `select.select_option` with one of the old
strings has to be updated; the values on the wire are unchanged.

On a vehicle that already had the electric select or the diesel switch before
they became conditional, Home Assistant keeps the old entity in its registry
and shows it as unavailable. Deleting it once from the device page is the only
cleanup; the integration does not remove entities by itself, because a
parameter that has not been reported *yet* is not the same as hardware that
does not exist.

Updates are pushed as the panel sends them (roughly 25 frames/minute), not
polled. The tank levels are the exception. A tank sensor answers with the
level it measured when it was last *asked*, and nothing on the bus asks it
except the panel, when its water screen is opened — so a tank emptied by hand
would otherwise keep reporting its old level indefinitely. The integration
asks for a fresh measurement once per connect and every 60 s while the link is
held, addressed to whichever device reported the tank.

With a non-zero poll interval, the manual controls can temporarily override
the wait without changing the configured interval. Set **Live mode duration**
to a whole number from 0 through 999 and press **Sync now / start live mode**.
The timer starts only after the BLE startup handshake has completed. If the
link drops during that period, the integration retries with its short backoff.
Press **End live mode** to release the link early. Permanent-connect mode
(`poll_interval_seconds: 0`) keeps its existing behaviour.

The panel drives its own fan while heating and has no setpoint at all while
venting, so exactly one of the two controls is meaningful at any time. The
climate entity reflects that: `supported_features` follows the mode rather than
advertising both at once. Off keeps the setpoint, the way every other
thermostat in Home Assistant does — it is the resting target you come back to.
The `number` entity exposes the fan level in every mode, for automations.

## Dashboard card

The integration ships a thermostat card whose dial follows the mode: it sets
the temperature while heating and the fan speed while venting, and is disabled
while off. Home Assistant's own climate dial is bound to temperature and
humidity only, and climate fan modes are arbitrary strings rather than a
numeric range, so core cannot put fan speed on an arc.

**There is nothing to install.** The integration serves the card at
`/truma_inetx/truma-climate-dial-card.js` and registers it with the frontend,
so it arrives and updates with the integration. Just add it to a dashboard:

```yaml
type: custom:truma-climate-dial-card
entity: climate.truma_inetx_ffb4d1
name: Heating          # optional
```

A HACS repository belongs to exactly one category, so this repository cannot
also be published as a HACS *plugin*. Serving the card from the integration
avoids a second repository to version and tag. The integration version is
appended to the URL as a query string, because the frontend service worker
caches assets for weeks and a browser hard-refresh does not bypass it — without
a changing URL an updated card would never reach the browser.

The trade-off: `add_extra_js_url` loads the module on every page load for every
user, not only when the card is on screen. It is about 19 KB.

The card does not reimplement the dial — it instantiates Home Assistant's own
`ha-control-circular-slider` and `ha-outlined-icon-button` and reuses the
frontend's layout CSS, so it inherits upstream's appearance and behaviour.
Those are internal frontend components with no stability guarantee: upstream
restyling arrives for free, an upstream rename breaks the card (it then renders
an explicit error naming the missing component).

A complete copy-and-paste control area with live mode, space heating, hot
water, energy sources and temperatures is available under
[`examples/lovelace/`](examples/lovelace/). Its README lists the requirements
and explains how to replace the device-specific entity IDs.

## Installation

### HACS (custom repository)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rpodgorny&repository=hass-truma-inetx&category=integration)

Or manually:

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/rpodgorny/hass-truma-inetx`, category **Integration**
3. Install **Truma iNet X (BLE)**, then restart Home Assistant
4. Settings → Devices & Services → the panel should be discovered; see
   [Pairing](#pairing)

### Icon

The integration ships its own artwork in `custom_components/truma_inetx/brand/`
(`icon.png` 256×256, `icon@2x.png` 512×512) — the Truma iNet X system mark,
with the "iNet X" wordmark removed and the mark re-centred. It is **Truma's
trademark, not covered by this repository's GPL-3.0 licence**; see
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md)
for the source, what was changed and the trademark notice. Since
[Home Assistant 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
these are served straight from the integration through HA's brands proxy and
take priority over the brands CDN — no submission to
[home-assistant/brands](https://github.com/home-assistant/brands) and no
manifest entry required.

On Home Assistant older than 2026.3 the UI falls back to a default icon. The
HACS store listing may also still show a placeholder, since it fetches icons
from the HACS CDN rather than from the repository
([hacs/integration#5223](https://github.com/hacs/integration/issues/5223)).

### Manual

Copy `custom_components/truma_inetx/` into your Home Assistant `config/custom_components/`
directory and restart.

## Pairing

The panel uses **Just Works** pairing (no passkey is shown) and only bonds while
it is actively in add-device mode. It is genuinely finicky — these rules matter:

1. Put the panel **freshly** into add-device mode (Truma iNet X app, or on the
   panel itself) so its pairing screen is up.
2. In Home Assistant the panel should appear as a discovered device. Otherwise
   Settings → Devices & Services → **+ Add Integration** → *Truma iNet X (BLE)*.
3. Press **Submit once.** Repeated submits against a panel that is not cleanly
   ready make it show "something went wrong" and it then needs re-arming.

Pairing normally completes in a few seconds.

### If pairing fails

Work through these in order — always re-entering add-device mode before each
attempt, since the panel only accepts a bond while its pairing screen is up:

1. **Clear the panel's saved Bluetooth device list.** It stores only ~4 devices
   and silently rejects new bonds once full. Clear it in the Truma iNet X app,
   re-arm add-device mode, and try again.
2. **If clearing the list did not help, power-cycle the panel and start over.**
   Switch it off and on, put it back into add-device mode, and repeat the whole
   pairing step. This drops "ghost" connections that hold one of the panel's
   connection slots, and makes it advertise a fresh Bluetooth address that
   pairs cleanly. It resolves most stubborn cases.

You do **not** need to clear any bonds on the Bluetooth proxy. If the proxy
still holds a bond the panel has forgotten, the panel rejects it on that one
address only (`error: 97`), and the integration rotates to the panel's next
address, which pairs normally.

To re-pair later, use **Reconfigure** on the device.

## Diagnostics

The device page's ⋮ → **Download diagnostics** dumps the config entry, whether
the last update succeeded, and the full decoded panel state — including
`seen_devices`, every bus address the integration has heard from, which is the
evidence for what is actually on a given vehicle's bus.

The state also carries `param_meta`: what the panel says each parameter *is*,
as opposed to what it currently reads — its range, whether it can be written,
and for an enum the panel's own name for every value, with the ones this
vehicle cannot produce marked. Truma documents none of the protocol, but the
panel describes it in every frame, so a download answers "what does this value
mean" without anyone having to watch their heater and write it down. The same
descriptions are logged, once each, at debug level.

The BLE address, the panel's name and the persisted app identity (`muid` /
`uuid`) are redacted: the address is a private address that still pins the panel
to a location, and the identity is what the panel bonds against. The panel state
itself carries nothing identifying.

## Known limitations

- **Reconnects can wedge.** If the link drops, reconnecting to the same address
  sometimes fails repeatedly with `ESP_GATT_CONN_FAIL_ESTABLISH` (0x3e). The
  integration backs off and rotates between the panel's advertised addresses,
  which usually recovers it; occasionally a panel power-cycle is needed. Under
  investigation.
- **Duplicate entries in the panel's device list.** Each pairing can leave an
  extra record. Harmless so far, but it consumes the panel's ~4 slots.
- Only the local name / service UUID are used for discovery; the stored address
  is treated as volatile because it rotates.

## Development

The checks in `tests/` are self-contained. They stub Home Assistant, bleak and
dbus, so they need neither an HA install nor hardware, and each file is a
script — run one directly, or all of them:

```bash
python3 tests/test_pairing_rotation.py            # pairing address rotation
python3 tests/test_pairing_transport_dispatch.py  # bonding uses the transport it has
python3 tests/test_device_from_bluez.py           # BLEDevice built from BlueZ's object
python3 tests/test_no_proxy_issue.py              # the "nothing can reach it" repair
python3 tests/test_water_entities.py              # water entities and write addressing
python3 tests/test_energy_entities.py             # energy sources, batteries, raw flame value
```

The remaining five drive real code that imports a library, so they need it
installed — `voluptuous` for the config flow's schema, `cbor2` for the four
that reach the protocol module, whether to build real frames and parse them back
or by way of the coordinator that imports it:

```bash
pip install voluptuous
python3 tests/test_panel2_discovery.py            # a renamed panel is still offered

pip install cbor2==5.6.5
python3 tests/test_param_discovery.py             # startup registration + discovery
python3 tests/test_measure_request.py             # asking the tanks to measure
python3 tests/test_param_meta.py                  # what the panel says a value means
python3 tests/test_panel_declared_options.py      # offering what the panel says exists
```

## Credits and licensing

The Home Assistant integration — coordinator, BLE transport, pairing, config
flow and all entity platforms — is original work in this repository and is
licensed under **GPL-3.0** (see [LICENSE](LICENSE)).

The wire protocol implementation in `custom_components/truma_inetx/truma/`
(`protocol.py`, `state.py`, `const.py`) is **vendored from
[daaaaan/truma-inetx-ble](https://github.com/daaaaan/truma-inetx-ble)**, whose
reverse-engineering of the iNet X protocol made this integration possible.
That project publishes no licence, so its author retains all rights and the
GPL-3.0 above does **not** apply to those files. They are isolated in their own
subpackage so the boundary stays visible; if upstream adds a licence and ships
an installable package, that subpackage will be replaced by a dependency.

`protocol.py` is vendored unchanged. `state.py` and `const.py` carry local
additions on top of the vendored code: the fresh-water pump and the two tank
levels, `seen_devices` and `topic_source` (which bus device reported a topic),
the extra device seeds and topics that parameter discovery walks, and the
measure-request constants. Those additions are original work in this
repository, but they sit inside files whose base is not, so the licence
position above governs the files as a whole.

The integration icon is the Truma iNet X system mark, used descriptively to
identify the device this integration talks to — see
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md).
It is excluded from the GPL-3.0 licence above.

"Truma" and the Truma iNet X mark are trademarks of Truma Gerätetechnik GmbH &
Co. KG. This project is not affiliated with, endorsed, sponsored by or
supported by Truma.
