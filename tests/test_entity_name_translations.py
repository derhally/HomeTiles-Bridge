"""Entity names follow the Home Assistant language, not hardcoded German.

An English Home Assistant showed "Display Helligkeit", "MQTT Verbindung" and
"Auto-Sleep Netzteil"; these entities now use translation keys with English
and German names.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl"
PLATFORMS = ("binary_sensor", "light", "number", "select", "sensor", "switch", "camera")
EXPECTED = {
    ("binary_sensor", "mqtt_connection"): ("MQTT Connection", "MQTT Verbindung"),
    ("binary_sensor", "encryption"): ("Encryption", "Verschl\u00fcsselung"),
    ("light", "display_brightness"): ("Display Brightness", "Display Helligkeit"),
    ("light", "screensaver_brightness"): ("Screensaver Brightness", "Screensaver Helligkeit"),
    ("number", "display_brightness"): ("Display Brightness", "Display Helligkeit"),
    ("select", "sleep_mains"): ("Auto-Sleep Power", "Auto-Sleep Netzteil"),
    ("select", "sleep_battery"): ("Auto-Sleep Battery", "Auto-Sleep Batterie"),
    ("sensor", "battery_soc"): ("Battery SoC", "Batterie SoC"),
    ("sensor", "external_temperature"): ("External Temperature", "Externe Temperatur"),
    ("sensor", "battery_voltage"): ("Battery Voltage", "Batteriespannung"),
    ("sensor", "wifi_rssi"): ("Wi-Fi Signal", "WLAN-Signal"),
    ("sensor", "last_restart"): ("Last Restart", "Letzter Neustart"),
    ("sensor", "reset_reason"): ("Restart Reason", "Neustartgrund"),
    ("sensor", "free_internal_memory"): ("Free Internal RAM", "Freier interner RAM"),
    ("sensor", "free_psram"): ("Free PSRAM", "Freier PSRAM"),
    ("sensor", "chip_temperature"): ("Chip Temperature", "Chiptemperatur"),
    ("sensor", "ip_address"): ("IP Address", "IP-Adresse"),
    ("sensor", "firmware_version"): ("Firmware Version", "Firmware-Version"),
    ("sensor", "mac_address"): ("MAC Address", "MAC-Adresse"),
}


def _load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))["entity"]


class EntityNameTranslationTests(unittest.TestCase):
    def test_names_are_translated(self):
        en = _load("translations/en.json")
        de = _load("translations/de.json")
        source = _load("strings.json")
        for (platform, key), (english, german) in EXPECTED.items():
            self.assertEqual(en[platform][key]["name"], english)
            self.assertEqual(source[platform][key]["name"], english)
            self.assertEqual(de[platform][key]["name"], german)

    def test_every_translation_key_has_both_languages(self):
        en = _load("translations/en.json")
        de = _load("translations/de.json")
        for platform in PLATFORMS:
            text = (ROOT / f"{platform}.py").read_text(encoding="utf-8")
            keys = set(re.findall(r'_attr_translation_key = "([a-z0-9_]+)"', text))
            if platform == "select":
                keys.update(("sleep_mains", "sleep_battery"))
            for key in keys:
                self.assertIn(key, en.get(platform, {}), f"{platform}.{key} en")
                self.assertIn(key, de.get(platform, {}), f"{platform}.{key} de")

    def test_panel_diagnostic_keys_and_reset_reasons_are_translated(self):
        en = _load("translations/en.json")
        de = _load("translations/de.json")
        text = (ROOT / "sensor.py").read_text(encoding="utf-8")
        # Diagnostic sensors pass their translation key to the shared base class.
        keys = set(re.findall(r'super\(\).__init__\(entry, base_topic, "([a-z_]+)"', text))
        keys.update(re.findall(r'Sensor\(entry, base_topic, "([a-z_]+)"', text))
        self.assertGreaterEqual(len(keys), 9)
        for key in keys:
            self.assertIn(key, en["sensor"], key)
            self.assertIn(key, de["sensor"], key)
        reasons = re.search(r"RESET_REASONS = \[(.*?)\]", text, re.S).group(1)
        for reason in re.findall(r'"([a-z_]+)"', reasons):
            self.assertIn(reason, en["sensor"]["reset_reason"]["state"], reason)
            self.assertIn(reason, de["sensor"]["reset_reason"]["state"], reason)

    def test_no_hardcoded_german_names(self):
        for platform in PLATFORMS:
            text = (ROOT / f"{platform}.py").read_text(encoding="utf-8")
            for word in ("Helligkeit", "Verbindung", "Netzteil", "Batterie", "Externe Temperatur"):
                self.assertNotIn(word, text, f"{platform}.py: {word}")


if __name__ == "__main__":
    unittest.main()
