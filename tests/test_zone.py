import unittest

from web.zone import Zone


class ZoneTests(unittest.TestCase):
    def test_zone_capsules_state_and_address(self):
        zone = Zone(id="living", name="Living Room", controller=2, zone_number=3)

        self.assertEqual(zone.id, "living")
        self.assertEqual(zone.name, "Living Room")
        self.assertEqual(zone.address, (2, 3))
        self.assertEqual(zone.zone, 3)
        self.assertFalse(zone.power)

    def test_zone_serializes_to_dict(self):
        zone = Zone(id="patio", name="Patio", power=True, source=1, volume=45, muted=True, controller=1, zone_number=4)

        self.assertEqual(
            zone.to_dict(),
            {
                "id": "patio",
                "name": "Patio",
                "power": True,
                "source": 1,
                "volume": 45,
                "muted": True,
                "controller": 1,
                "zone": 4,
            },
        )

    def test_zone_can_be_created_from_dict(self):
        zone = Zone.from_dict({"id": "kitchen", "name": "Kitchen", "power": True, "source": 1, "volume": 30, "muted": False, "controller": 3, "zone": 2})

        self.assertEqual(zone.id, "kitchen")
        self.assertEqual(zone.power, True)
        self.assertEqual(zone.source, 1)
        self.assertEqual(zone.controller, 3)
        self.assertEqual(zone.zone_number, 2)

    def test_set_mute_updates_zone_state(self):
        zone = Zone(id="living", name="Living Room", muted=False)

        class DummyBackend:
            def set_zone_mute(self, zone_obj, muted, current_muted=None):
                return True

        zone.set_mute(True, backend=DummyBackend())

        self.assertTrue(zone.muted)


if __name__ == "__main__":
    unittest.main()
