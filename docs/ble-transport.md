# BLE-Transport / BLE transport

## Deutsch

Die physische BLE-Verbindung allein bedeutet nicht, dass sämtliche Gerätewerte
bereits empfangen wurden. Besonders beim Start werden viele Parameter übertragen.

Eine eingehende Nachricht wird auf dem CMD-Kanal mit `83 LL HH` angekündigt.
`LL HH` ist die Nachrichtenlänge (Little Endian), keine Befehlsbestätigung.
Die Integration antwortet mit `03 00`, sammelt die DATA-R-Teile bis zur
angekündigten Länge und bestätigt erst die vollständige Nachricht mit `F0 01`.
Beispiel aus einer Geräteaufzeichnung: `83 00 01` kündigt 256 Byte an;
die Lieferung erfolgt in zwei Teilen mit 248 und 8 Byte.

Ein ausgehender Befehl benötigt zuerst `81 00` (Ready) und danach `F0 01`.
Eine gleichzeitig eingehende Nachrichtenankündigung darf diese Wartephasen
nicht abschließen. Die Transportbestätigung ist außerdem noch keine Bestätigung
des eingestellten Heizungswertes; dafür ist dessen Rückmeldung auszuwerten.

Ready und DataAck enthalten keine eindeutige Übertragungskennung. Nach Abbruch,
Zeitüberschreitung oder einem anderen Übertragungsfehler wird die Sitzung daher
ungültig und getrennt, bevor ein weiterer Befehl gesendet werden kann.
Eine verspätete Bestätigung kann so nicht den folgenden Transfer bestätigen.

Regressionstests: `python3 tests/test_transport_ack_order.py`.

## English

A physical BLE connection does not establish that all device values have arrived.
Startup in particular transfers many parameters.

An incoming message is announced on CMD as `83 LL HH`, with a little-endian
message length, not an acknowledgement of an outgoing command. Reply with
`03 00`, accumulate DATA-R fragments to the announced length, then acknowledge
the complete message once with `F0 01`. A captured example announces 256 bytes
(`83 00 01`) and delivers them as 248 + 8 bytes.

Outgoing transfers wait for `81 00` (Ready), then `F0 01` after sending.
An unrelated incoming announcement must not complete either wait. Transport
acknowledgement is distinct from confirmation of a requested heater setting.

Ready and DataAck do not identify a particular transfer. Cancellation, timeout
or another transfer failure therefore invalidates and disconnects the session
before another packet can be sent. A delayed acknowledgement cannot complete
the following transfer.

Regression tests: `python3 tests/test_transport_ack_order.py`.
