import unittest

from command_runtime import Command


class BrightnessCommandTests(unittest.TestCase):
    def command(self, **overrides):
        value = {
            "command_id": "HC_001",
            "byte_offset": 16,
            "entity_id": "light.shelly0110dimg3_80b54e32c7e8",
            "action": "brightness_pct",
            "kind": "real",
            "min_value": 0,
            "max_value": 100,
        }
        value.update(overrides)
        return Command.from_dict(value)

    def test_light_brightness_command_is_real_and_bounded_to_percent(self):
        command = self.command()
        self.assertEqual(command.normalize(0), 0)
        self.assertEqual(command.normalize(100), 100)
        with self.assertRaises(ValueError):
            command.normalize(-0.1)
        with self.assertRaises(ValueError):
            command.normalize(100.1)

    def test_brightness_command_rejects_wrong_domain_type_or_range(self):
        for overrides in (
            {"entity_id": "fan.blower"},
            {"kind": "bool"},
            {"min_value": 1},
            {"max_value": 99},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.command(**overrides)

if __name__ == "__main__":
    unittest.main()
