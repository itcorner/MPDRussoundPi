import unittest

from web.zone import Zone


class ZoneTests(unittest.TestCase):
    def test_zone_capsules_state_and_address(self):
        zone = Zone(name="Living Room", controller=2, zone_number=3)

        self.assertEqual(zone.name, "Living Room")
        self.assertEqual(zone.address, (2, 3))
        self.assertEqual(zone.zone, 3)
        self.assertFalse(zone.power)

    def test_zone_serializes_to_dict(self):
        zone = Zone(name="Patio", power=True, source=1, volume=45, controller=1, zone_number=4)

        self.assertEqual(
            zone.to_dict(),
            {
                "name": "Patio",
                "power": True,
                "source": 1,
                "volume": 45,
                "controller": 1,
                "zone": 4,
            },
        )

    def test_zone_can_be_created_from_dict(self):
        zone = Zone.from_dict({"name": "Kitchen", "power": True, "source": 1, "volume": 30, "controller": 3, "zone": 2})

        self.assertEqual(zone.power, True)
        self.assertEqual(zone.source, 1)
        self.assertEqual(zone.controller, 3)
        self.assertEqual(zone.zone_number, 2)


if __name__ == "__main__":
    unittest.main()
