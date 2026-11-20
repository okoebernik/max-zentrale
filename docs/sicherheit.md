# Sicherheit & Betrieb

## Anmeldung und Rollen

* Jede Seite und jede API-Antwort außer der Anmeldeseite, dem Logo und dem Status „Einrichtung
  nötig?“ verlangt eine Anmeldung.
* Passwörter werden mit **scrypt** (n=2¹⁴, r=8, p=1, 16 Byte Salt) gespeichert, nie im Klartext.
* Die Sitzung ist ein zufälliger 256-Bit-Token im Cookie `max_session` (`HttpOnly`,
  `SameSite=Strict`, 30 Tage). In der Datenbank liegt nur dessen SHA-256-Hash. Beim Abmelden und
  bei einer Passwortänderung werden die Sitzungen gelöscht (bei der Änderung des eigenen
  Passworts bleibt nur die aktuelle).
* Nach 5 Fehlversuchen von einer Adresse wird die Anmeldung 5 Minuten gesperrt. Auch bei
  unbekanntem Benutzernamen wird der Hash berechnet, damit sich Benutzer nicht über die
  Antwortzeit erraten lassen.
* **Rollen:** Administrator, Bediener, Nur lesen (siehe [Bedienung](bedienung.md#benutzer)). Der
  Server prüft die Rolle bei jeder Anfrage, nicht nur in der Oberfläche.
* **Erste Einrichtung:** Solange es keinen Benutzer gibt, kann *jeder*, der die Seite erreicht,
  den ersten Administrator anlegen. Lege ihn sofort nach dem Start an.

## CSRF und Header

Schreibende Anfragen (`POST`) müssen `Content-Type: application/json` tragen und – falls der
Browser einen `Origin` mitschickt – auf denselben Host zeigen. Antworten tragen
`X-Content-Type-Options: nosniff` und `X-Frame-Options: DENY`.

## Netzwerk

* **Kein TLS:** Der eingebaute Server spricht nur HTTP. Passwörter und Sitzungs-Cookie laufen
  also unverschlüsselt durchs Netz. Betreibe die Zentrale nur in einem vertrauenswürdigen LAN
  oder stelle einen Reverse Proxy mit TLS davor (nginx, Caddy, Traefik).
* **Nicht ins Internet stellen**, ohne vorher TLS und ein zusätzliches Schutzverfahren
  (VPN, Reverse Proxy mit Zusatz-Login) einzurichten. Die Zentrale kann Heizungen steuern.
* Der Server bindet standardmäßig nur an `127.0.0.1`. Für den Zugriff aus dem LAN `--host 0.0.0.0`
  bzw. `MAX_HOST=0.0.0.0` setzen.
* Beispiel Caddy (TLS, automatisch):

  ```
  heizung.example.org {
      reverse_proxy 127.0.0.1:8080
  }
  ```

  Mit TLS lohnt es sich, im Cookie das Attribut `Secure` zu ergänzen
  (`cookie()` in `max_server.py`).

## Funk

Die 868-MHz-Telegramme der MAX!-Geräte sind **nicht verschlüsselt und nicht authentifiziert**.
Jeder in Funkreichweite kann sie mitlesen und – mit Kenntnis der Adressen – Befehle senden.
Das ist eine Eigenschaft des Protokolls und lässt sich in dieser Software nicht beheben.

## Geheimnisse in der Datenbank

* Passwort-Hashes der Benutzer,
* das **MQTT-Passwort im Klartext**.

Die Datenbank und ihre Backups gehören deshalb nur dem Dienstbenutzer (Backups werden mit
Rechten `600` angelegt). Gib Backups nicht weiter, ohne das MQTT-Passwort zu erneuern.

## Backups und Wiederherstellung

* Die Software sichert ihre Datenbank selbst ([Bedienung → Backup](bedienung.md#backup)).
* Für den Container/Rechner zusätzlich eine Sicherung auf Systemebene einrichten
  ([Proxmox-Beispiel](installation.md#proxmox)).
* **Wiederherstellen von Hand** (Dienst vorher stoppen):

  ```bash
  sudo systemctl stop max
  sudo cp /var/lib/max/backups/max-20261120-030001.db /var/lib/max/max.db
  sudo chown max: /var/lib/max/max.db
  sudo systemctl start max
  ```

* **Passwort vergessen (kein Administrator mehr):** Dienst stoppen und die Benutzertabelle leeren –
  danach erscheint beim nächsten Aufruf wieder die Einrichtung:

  ```bash
  sudo systemctl stop max
  sudo sqlite3 /var/lib/max/max.db "DELETE FROM sessions; DELETE FROM users;"
  sudo systemctl start max
  ```

## Aktualisieren

1. Backup erstellen.
2. Neue Dateien nach `/opt/max` kopieren (bzw. `docker compose up -d --build`).
3. Dienst neu starten. Neue Tabellen und Spalten legt die Software beim Start selbst an
   (`CREATE TABLE IF NOT EXISTS`).

## Fehlersuche

| Beobachtung | Ursache / Abhilfe |
|---|---|
| Status „getrennt“ mit Fehlermeldung zum Port | Port-Name falsch, Gerät nicht angeschlossen, oder ein anderes Programm hält den Port. `ls -l /dev/ttyACM*`, Berechtigung (`dialout`) prüfen. |
| Verbunden, aber nie Telegramme | culfw im Empfangsmodus? Gerät neu stecken (Netzteil/USB abziehen), Dienst neu starten. Bei `tcp://` auf USB wechseln. |
| „not enough send credit (duty cycle)“ | Das 1-%-Limit ist erreicht. Das Kontingent steigt um 1 Einheit pro Sekunde (eine Sendung kostet ca. 106). Warten oder weniger gleichzeitig senden. |
| „no acknowledgement from thermostat“ / „thermostat rejected the command“ | Gerät außer Reichweite, schwache Batterie oder falsche `--src`-Adresse (das Gerät ist an einen anderen Cube gekoppelt). |
| Gerät wechselt zwischen Typen | Behoben: Der Typ wird nur aus eigenen Telegrammen des Geräts abgeleitet. Alte Fehleinträge verschwinden nach neuen Telegrammen. |
