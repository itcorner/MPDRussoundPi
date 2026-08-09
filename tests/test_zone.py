import unittest

from web.zone import Zone


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object, object | None]] = []

    def set_zone_power(self, zone: Zone, power: bool) -> bool:
        self.calls.append(("power", zone, power, None))
        return True

    def set_zone_source(self, zone: Zone, source_id: int, inputs: list[dict[str, object]]) -> bool:
        self.calls.append(("source", zone, source_id, inputs))
        return True

    def set_zone_volume(self, zone: Zone, volume: int) -> bool:
        self.calls.append(("volume", zone, volume, None))
        return True

    def set_zone_bass(self, zone: Zone, bass: int) -> bool:
        self.calls.append(("bass", zone, bass, None))
        return True

    def set_zone_treble(self, zone: Zone, treble: int) -> bool:
        self.calls.append(("treble", zone, treble, None))
        return True

    def set_zone_loudness(self, zone: Zone, loudness: bool) -> bool:
        self.calls.append(("loudness", zone, loudness, None))
        return True

    def set_zone_balance(self, zone: Zone, balance: int) -> bool:
        self.calls.append(("balance", zone, balance, None))
        return True


class ZoneTests(unittest.TestCase):
    def test_zone_capsules_state_and_address(self):
        zone = Zone(name="Living Room", controller=2, zone_number=3)

        self.assertEqual(zone.name, "Living Room")
        self.assertEqual(zone.address, (2, 3))
        self.assertEqual(zone.zone_number, 3)
        self.assertFalse(zone.power)

    def test_zone_serializes_to_dict(self):
        zone = Zone(name="Patio", power=True, source=1, volume=45, bass=-4, treble=2, loudness=True, balance=-3, controller=1, zone_number=4)

        self.assertEqual(
            zone.to_state_payload(),
            {
                "power": True,
                "source": 1,
                "volume": 45,
                "bass": -4,
                "treble": 2,
                "loudness": True,
                "balance": -3,
                "controller": 1,
                "zone": 4,
            },
        )

    def test_zone_can_be_created_from_dict(self):
        zone = Zone.from_dict({"name": "Kitchen", "power": True, "source": 1, "volume": 30, "bass": 5, "treble": -2, "loudness": True, "balance": 4, "controller": 3, "zone": 2})

        self.assertEqual(zone.power, True)
        self.assertEqual(zone.source, 1)
        self.assertEqual(zone.bass, 5)
        self.assertEqual(zone.treble, -2)
        self.assertTrue(zone.loudness)
        self.assertEqual(zone.balance, 4)
        self.assertEqual(zone.controller, 3)
        self.assertEqual(zone.zone_number, 2)

    def test_set_power_applies_directly_to_backend(self):
        zone = Zone(name="Office", power=False, source=1, volume=20, controller=2, zone_number=1)
        backend = RecordingBackend()

        self.assertTrue(zone.set_power(True, backend=backend))
        self.assertEqual(backend.calls[0][0], "power")
        self.assertTrue(backend.calls[0][2])

    def test_apply_to_backend_applies_all_zone_settings(self):
        zone = Zone(name="Patio", power=True, source=2, volume=44, bass=1, treble=-2, loudness=True, balance=3, controller=1, zone_number=4)
        backend = RecordingBackend()
        inputs = [{"id": 2, "name": "TV"}]

        self.assertTrue(zone.apply_to_backend(backend=backend, inputs=inputs))
        self.assertEqual([call[0] for call in backend.calls], ["power", "source", "volume", "bass", "treble", "loudness", "balance"])
        self.assertEqual(backend.calls[1][2], 2)

    def test_zone_exposes_address_without_scope_logic(self):
        zone = Zone(name="Kitchen", controller=2, zone_number=6)

        self.assertEqual(zone.address, (2, 6))

    def test_zone_serializes_config_and_state_payloads_separately(self):
        zone = Zone(name="Patio", power=True, source=2, volume=44, bass=-2, treble=1, loudness=True, balance=-1, controller=3, zone_number=4, enabled=False, visible=True)

        self.assertEqual(
            zone.to_config_payload(),
            {
                "name": "Patio",
                "controller": 3,
                "zone": 4,
                "enabled": False,
                "visible": True,
            },
        )
        self.assertEqual(
            zone.to_state_payload(),
            {
                "power": True,
                "source": 2,
                "volume": 44,
                "bass": -2,
                "treble": 1,
                "loudness": True,
                "balance": -1,
                "controller": 3,
                "zone": 4,
            },
        )
        self.assertEqual(
            zone.to_frontend_payload(),
            {
                "name": "Patio",
                "power": True,
                "source": 2,
                "volume": 44,
                "bass": -2,
                "treble": 1,
                "loudness": True,
                "balance": -1,
                "controller": 3,
                "zone": 4,
            },
        )
        self.assertNotIn("name", zone.to_state_payload())
        self.assertNotIn("enabled", zone.to_state_payload())
        self.assertNotIn("visible", zone.to_state_payload())

    def test_source_is_normalized_to_integer(self):
        zone = Zone(name="Hallway", source=None)

        self.assertIsInstance(zone.source, int)
        self.assertEqual(zone.source, 0)


if __name__ == "__main__":
    unittest.main()
