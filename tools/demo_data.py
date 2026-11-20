"""Fictitious demo installation shared by the simulator, the demo-database
builder and the screenshot script.  No real addresses or names."""

# kind: heat = radiator thermostat, wall = wall thermostat, window = contact
DEVICES = [
    dict(addr="1A0001", kind="heat", name="Heizkörper Wohnzimmer", room="Wohnzimmer", setpoint=21.0, measured=20.6, valve=34),
    dict(addr="1A0002", kind="wall", name="Wandthermostat Wohnzimmer", room="Wohnzimmer", setpoint=21.0, measured=20.8, valve=None),
    dict(addr="1A0003", kind="window", name="Fenster Wohnzimmer", room="Wohnzimmer", open=False),
    dict(addr="1A0004", kind="heat", name="Heizkörper Küche", room="Küche", setpoint=19.5, measured=19.9, valve=0),
    dict(addr="1A0005", kind="window", name="Fenster Küche", room="Küche", open=False),
    dict(addr="1A0006", kind="heat", name="Heizkörper Schlafzimmer", room="Schlafzimmer", setpoint=17.0, measured=17.6, valve=0),
    dict(addr="1A0007", kind="wall", name="Wandthermostat Schlafzimmer", room="Schlafzimmer", setpoint=17.0, measured=17.5, valve=None),
    dict(addr="1A0008", kind="window", name="Fenster Schlafzimmer", room="Schlafzimmer", open=False),
    dict(addr="1A0009", kind="heat", name="Heizkörper Bad", room="Badezimmer", setpoint=22.0, measured=21.4, valve=48),
    dict(addr="1A000A", kind="heat", name="Heizkörper Büro", room="Büro", setpoint=20.0, measured=18.9, valve=62),
    dict(addr="1A000B", kind="wall", name="Wandthermostat Büro", room="Büro", setpoint=20.0, measured=19.1, valve=None),
    dict(addr="1A000C", kind="window", name="Fenster Büro", room="Büro", open=True, battery_low=True),
]
ROOMS = ["Wohnzimmer", "Küche", "Schlafzimmer", "Badezimmer", "Büro"]
SRC = "10B240"          # RF address of the "cube" (same value the real software uses)
