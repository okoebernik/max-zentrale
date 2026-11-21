# Funkprotokoll (MAX! / „Moritz“)

Notizen zum Protokoll, wie es diese Software verwendet. Quellen: Beobachtung des echten
Funkverkehrs, das FHEM-Modul `CUL_MAX` und die Community-Beschreibung „Moritz“. Alle Zahlen
hexadezimal, wenn nicht anders angegeben.

## culfw-Befehle

Das Funkgerät (CUL mit culfw) wird über eine Textschnittstelle gesteuert (38400 Baud, 8N1 bzw.
TCP-Port 2323). Verwendet werden:

| Befehl | Wirkung |
|---|---|
| `V` | Firmware-Version |
| `X` | Sendekontingent in Einheiten zu 10 ms: Antwort `21  <Zahl>` |
| `Zr` | MAX!-Empfang (Moritz-Modus) einschalten |
| `Zx` | Empfang ausschalten |
| `Zs<hex>` | Telegramm senden (Hex ohne `Z`-Präfix, mit Längenbyte) |

Empfangen wird je Telegramm eine Zeile `Z<hex>`.

## Telegramm-Aufbau

```
Z  LL  CC  FF  TT  SSSSSS  DDDDDD  GG  <Nutzlast…>  [RR]
   │   │   │   │   │       │       │                  └ Empfangsstärke (nur beim Empfang)
   │   │   │   │   │       │       └ Gruppen-ID
   │   │   │   │   │       └ Empfänger-Adresse (3 Byte)
   │   │   │   │   └ Absender-Adresse (3 Byte)
   │   │   │   └ Telegrammtyp
   │   │   └ Flags
   │   └ Laufende Nummer
   └ Länge ab CC (ohne Längenbyte und ohne RR)
```

Die Empfangsstärke in dBm ist `(RR ≥ 128 ? RR − 256 : RR) / 2 − 74`.

## Telegrammtypen

| Typ | Name | Richtung | Nutzlast |
|---|---|---|---|
| `00` | PairPing | Gerät → alle | Firmware, Gerätetyp, Selbsttest, 10 Zeichen Seriennummer (ASCII) |
| `01` | PairPong | Zentrale → Gerät | `00` |
| `02` | Ack | Gerät → Zentrale | `01` = ok (sonst abgelehnt), Flags, Ventil, Soll (bei Heizkörpern) |
| `03` | TimeInformation | beide | 5 Byte: Jahr−2000, Tag, Stunde, Minute (+ Monats-Hi-Bits), Sekunde (+ Monats-Lo-Bits) |
| `10` | ConfigWeekProfile | Zentrale → Gerät | Wochenprofil-Teil (siehe unten) |
| `11` | ConfigTemperatures | Zentrale → Gerät | Komfort, Eco, Max, Min, Offset, Fenster-Temperatur, Fenster-Dauer |
| `30` | ShutterContactState | Kontakt → Zentrale | Bit 1 = offen, Bit 6 RF-Fehler, Bit 7 Batterie |
| `40` | SetTemperature | Zentrale/Wandthermostat → Heizkörper | `Modus<<6 \| Temp·2` |
| `42` | WallThermostatControl | Wandthermostat → Heizkörper | Soll (7 Bit) + Bit 8 der Ist-Temperatur, Ist-Temperatur (niederes Byte, in ⅒ °C) |
| `43`/`44` | SetComfort / SetEco | Zentrale → Gerät | keine |
| `50` | PushButtonState | Taster | Tasterzustand |
| `60` | ThermostatState | Heizkörper → alle | Flags (Modus, Fehler, Batterie), Ventil %, Soll, Ist-Temperatur (⅒ °C) |
| `70` | WallThermostatState | Wandthermostat | Flags, Anzeige-Byte (`0x04` = Ist-Temperatur), Soll, Ist (2 Byte) |
| `82` | SetDisplayActualTemperature | Zentrale → Wandthermostat | `04` = Ist-Temperatur, `00` = Soll |

Modi: `0` auto, `1` manuell, `2` Urlaub, `3` Boost.

### Gerätetypen erkennen

Den Typ eines Geräts darf man **nur aus seinen eigenen Telegrammtypen** ableiten (`60` →
Heizkörper, `42`/`70` → Wandthermostat, `30` → Fensterkontakt, `50` → Taster oder aus dem
Gerätetyp im PairPing). **Acks und SetTemperature sind dafür ungeeignet**, denn Heizkörper *und*
Wandthermostate senden sie. Ein früher Fehler war, dass Wandthermostate dadurch zwischen „Ventil“
und „Wandthermostat“ wechselten.

## Wochenprofil

* Tag-IDs: Sa=0, So=1, Mo=2, Di=3, Mi=4, Do=5, Fr=6.
* Pro Tag 13 Schaltpunkte à 2 Byte: `(Temp·2 << 9) | (Minuten ÷ 5)`; jeder Punkt gilt *bis* zu seiner
  Uhrzeit, der letzte endet bei 24:00. Nicht benutzte Punkte werden mit `0x4520` (17 °C bis 24:00)
  aufgefüllt.
* Gesendet wird in zwei Teilen: Teil 0 enthält die Punkte 1–7, Teil 1 die Punkte 8–13; das erste
  Nutzlastbyte ist `(Teil << 4) | Tag-ID`. Teil 1 entfällt, wenn nur bis zu 7 Punkte benutzt werden.

## Sendebegrenzung (Duty Cycle)

Im 868,3-MHz-Band gilt in der EU ein **1-%-Limit**. culfw führt dazu ein Sendekontingent, das mit
`X` abgefragt wird. Eine Sendung braucht rund eine Sekunde (Präambel) plus Nutzdaten – ca. 106
Einheiten – und das Kontingent füllt sich mit einer Einheit pro Sekunde wieder auf. Nach dem
Einschalten stehen ca. 26 Sendungen am Stück zur Verfügung, danach etwa eine alle 106 Sekunden.
Die Software fragt vor jeder Sendung `X` ab, sendet erst bei genügend Kontingent, wartet sonst
und bricht Pairing-Antworten sofort ab (sie sind zeitkritisch).

## Ablauf eines Befehls

1. Auftrag in die Warteschlange (Oberfläche, MQTT, Raumsteuerung).
2. `X` → Kontingent prüfen; reicht es nicht, ca. 5 Sekunden warten und wiederholen (maximal ca.
   90 Sekunden, Wochenprofile länger).
3. `Zs<telegramm>` senden.
4. Auf ein **Ack** des Geräts warten (Absender = Gerät, Empfänger = Zentrale, `01` = ok), bis
   zu 5,5 Sekunden. Ohne Ack bis zu dreimal wiederholen.
5. Ergebnis an die Oberfläche melden.

Bei Pairing-Antworten (PairPong) gibt es Vorrang: sie unterbrechen einen wartenden Auftrag, der
danach fortgesetzt wird.

## Absenderadresse

Alle Telegramme der Zentrale tragen eine feste 3-Byte-Absenderadresse (`--src`). Gekoppelte
Geräte erwarten ihre Befehle von der Adresse ihres Cubes; deshalb nutzt man beim Umzug die Adresse
des alten Cubes weiter. Auf eine fremde Adresse reagiert ein Gerät nicht – außer beim Pairing,
bei dem es sich an den Absender der PairPong-Antwort bindet.

## Verknüpfungen (Link-Partner)

`20` (AddLinkPartner) und `21` (RemoveLinkPartner) tragen als Nutzlast die Adresse des Partners
(3 Byte) und dessen Gerätetyp (1 Byte: 1 Heizkörper, 2 Heizkörper+, 3 Wandthermostat,
4 Fensterkontakt, 5 Taster). Die Zentrale sendet den Befehl an **beide** Geräte, jeweils mit der
Adresse des anderen. Ein Fensterkontakt hört nur ganz kurz nach seinem eigenen Funkspruch zu; deshalb wartet die Zentrale auf dessen nächstes Telegramm und antwortet sofort mit `Zf` (culfw „fast“, ohne die lange Präambel von `Zs`). Danach schickt der Fensterkontakt seine Zustandstelegramme (`30`) auch direkt
an das Thermostat, das bei offenem Fenster selbständig die Fenster-offen-Temperatur einstellt –
unabhängig davon, ob die Zentrale läuft.

## Zeitsynchronisation

Wandthermostate und Heizkörper fragen ihre Uhrzeit per `03` an oder melden sie. Weicht die Zeit
um mehr als 10 Sekunden ab (oder ist sie ungültig), antwortet die Zentrale mit der aktuellen
lokalen Zeit; maximal einmal pro Minute je Gerät, um das Sendekontingent zu schonen.

## Grenzen

* Wochenprofile und Konfiguration lassen sich **nicht auslesen**, nur senden.
* Der Cube kann nicht gleichzeitig als MAX!-Cube und als CUL arbeiten – er wird umgeflasht.
* Eine Verknüpfung (Fensterkontakt ↔ Thermostat) lässt sich nur setzen und lösen, nicht aus den
  Geräten auslesen; die Zentrale merkt sich, was sie selbst gesetzt hat (Tabelle `links`).
* Heizkörperthermostate melden die gemessene Temperatur nicht im Urlaubsmodus und nicht in jedem
  Telegramm; die Oberfläche zeigt dann den zuletzt bekannten Wert.
