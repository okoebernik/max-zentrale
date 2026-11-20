# Entwicklung

## Aufbau

```
max_server.py   HTTP-Server, Funk-Thread (SerialWorker), API
max_logger.py   Telegramme parsen/dekodieren, SQLite-Schema, einfacher Mitschnitt (CLI)
max_send.py     Telegramme bauen (SetTemperature, Zeit, Wochenprofil), einfaches Sende-CLI
max_auth.py     Benutzer, Rollen, Sitzungen, Einstellungs-Tabelle
max_mqtt.py     Minimaler MQTT-3.1.1-Client und die Brücke (State, Befehle, Discovery)
max_backup.py   Online-Backups der Datenbank, Wiederherstellung, Automatik
web/            Single-Page-Oberfläche (index.html, logo.svg), kein Build-Schritt
tools/          Simulator, Demo-Datenbank, Screenshot-Skript
tests/          Unit-Tests (unittest)
deploy/         systemd-Dienstdatei
```

Es gibt **keine Abhängigkeiten außer `pyserial`**. Der HTTP-Server ist `ThreadingHTTPServer` aus
der Standardbibliothek, die Oberfläche reines HTML/CSS/JavaScript ohne Framework.

### Threads

| Thread | Aufgabe |
|---|---|
| `SerialWorker` | besitzt den Funk-Port: liest Zeilen, speichert/dekodiert Telegramme, arbeitet die Warteschlange der Sendeaufträge ab (Kontingent → senden → Ack) |
| HTTP-Server | ein Thread je Anfrage; spricht mit dem Worker nur über Warteschlangen (`jobs`, `prio`) |
| `Bridge` (MQTT) | verbindet sich zum Broker, veröffentlicht Zustände, führt Befehle in eigenen Threads aus |
| `AutoBackup` | prüft minütlich, ob das Tagesbackup fällig ist |

Der Worker ist die einzige Stelle, die auf den Funk-Port schreibt. Aufträge (`Job`) tragen den
Typ (`set`, `time`, `raw`), und das Ergebnis kommt über ein `threading.Event` zurück.
Pairing-Antworten laufen über die Prioritätswarteschlange und unterbrechen wartende Aufträge.

### Datenbank (SQLite)

| Tabelle | Inhalt |
|---|---|
| `telegrams` | jedes empfangene Telegramm (roh und dekodiert) |
| `devices` | bekannte Geräte: Adresse, Typ, Seriennummer, zuletzt gesehen, Batterie |
| `readings` | Messwerte über die Zeit (Ist, Soll, Ventil, Modus, Fenster) |
| `names`, `rooms`, `device_room` | Namen und Räume |
| `profiles`, `device_config` | zuletzt gesendete Wochenprofile und Konfiguration |
| `users`, `sessions` | Benutzerverwaltung |
| `settings` | Schlüssel/Wert (MQTT, Backup) |

Das Schema wird beim Start per `CREATE TABLE IF NOT EXISTS` angelegt.

## HTTP-API

Alle Endpunkte außer den ersten drei verlangen eine Anmeldung (Cookie). Antworten sind JSON.
`POST` verlangt `Content-Type: application/json`. Rollen: **V** ab *Nur lesen*, **B** ab
*Bediener*, **A** nur *Administrator*.

| Methode / Pfad | Rolle | Zweck |
|---|---|---|
| `GET /api/auth` | – | `{setup, user}`: Einrichtung nötig? wer ist angemeldet? |
| `POST /api/setup` | – | ersten Administrator anlegen (nur wenn es noch keinen Benutzer gibt) |
| `POST /api/login` | – | Anmeldung `{username, password}` (setzt Cookie) |
| `POST /api/logout`, `/api/password` | V | Abmelden, eigenes Passwort ändern `{old, new}` |
| `GET /api/state` | V | Geräte (mit letzten Werten), Räume, Pairing, Status |
| `GET /api/history?addr=&hours=` | V | Messwerte eines Geräts |
| `GET /api/log` | V | letzte 60 Telegramme |
| `GET /api/console?since=N` | V | Konsoleneinträge mit laufender Nummer > N (max. 400) |
| `GET /api/profile?addr=`, `/api/config?addr=` | V | gespeichertes Wochenprofil / Konfiguration |
| `GET /api/find?since=` | V | Kandidaten für „Gerät finden“ |
| `POST /api/set` | B | `{addr, temp}` oder `{addr, auto:true}` |
| `POST /api/room_set` | B | `{room_id, temp}` oder `{room_id, auto:true}` |
| `POST /api/room`, `/api/name`, `/api/device_room` | B | Räume und Namen |
| `POST /api/profile` | B | `{addr, profile:{Mon:[{temp,until},…]}, send}` |
| `POST /api/config`, `/api/preset` | B | Komfort/Eco-Konfiguration senden; `{preset: comfort\|eco}` |
| `POST /api/display`, `/api/time` | B | Anzeige Ist/Soll, Uhrzeit stellen |
| `POST /api/pairing` | B | `{enable: true\|false}` |
| `GET/POST /api/users` | A | Benutzer verwalten |
| `GET/POST /api/mqtt` | A | MQTT-Einstellungen und Status |
| `GET /api/backups`, `GET /api/backup/download?name=`, `POST /api/backup` | A | Backups |

Ein Sendebefehl wartet, bis das Gerät bestätigt hat (bis zu 30 s); Fehler kommen als HTTP
502/504 mit `{ok:false, error}`.

## Simulator und Demo-Daten

Zum Entwickeln und für die Screenshots braucht man keine Hardware:

| Skript | Zweck |
|---|---|
| `tools/sim_culfw.py` | Fake-culfw per TCP (`--port 2323`): liefert Telegramme einer erfundenen Wohnung und bestätigt `SetTemperature` mit einem Ack |
| `tools/make_demo_db.py` | Datenbank mit Räumen, Namen, 3 Tagen Verlauf und einem Wochenprofil |
| `tools/sim_broker.py` | MQTT-Gegenstelle, die alles bestätigt und Veröffentlichungen ausgibt |
| `tools/demo_data.py` | die erfundenen Geräte (Adressen `1A0001` …) |

```bash
python tools/make_demo_db.py demo.db
python tools/sim_culfw.py --port 2323 &
MAX_PORT=tcp://127.0.0.1:2323 python max_server.py --db demo.db --http 8080
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Die Tests decken Telegramm-Parser, Paketbau (inkl. Zeit-, Profil- und Konfigurations-Nutzlast),
Benutzerverwaltung (Anmeldung, Rollen, letzter Administrator, Sperre) und Backup/Wiederherstellung ab.
Auf GitHub laufen sie bei jedem Push (Actions).

## Screenshots neu erzeugen

```bash
pip install playwright pyserial
python tools/screenshots.py            # schreibt nach docs/screenshots/
```

Das Skript startet Simulator, Broker und Server mit der Demo-Datenbank, legt einen Demo-
Administrator an und fotografiert alle Seiten mit einem installierten Edge oder Chrome
(Dunkel, zwei Seiten hell, eine Handy-Ansicht). Nach Änderungen an der Oberfläche einmal ausführen
und die Bilder einchecken.

## Nützliche Skripte ohne Server

* `python max_logger.py --port /dev/ttyACM0 --duration 60` – Telegramme mitschneiden (nur Empfang).
* `python max_send.py 1A0001 21.5 [--dry-run]` bzw. `--auto` – einzelne Sollwert-Befehle senden.

Beide brauchen den exklusiven Zugriff auf den Port – den Server vorher stoppen.

## Weiterentwicklung (Ideen)

* Fensterkontakt-/Thermostat-Verknüpfung (Telegramme `20`/`21`),
* Zeitplan für Räume („Urlaub“, Abwesenheit),
* HTTPS direkt im Server,
* Wochenprofil von mehreren Geräten gleichzeitig senden,
* Alarme (Mail/Push) bei Batterie schwach oder Fenster lange offen.
