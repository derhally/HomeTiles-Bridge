"""Assist screens from Home Assistant (assist_screens.py) and their wiring."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from test_view_navigation import ROOT, load_module

SCREENS = load_module("assist_screens")
ASSIST = load_module("assist")
BASE = "hometiles/panel"


def screen(**fields):
    data = {"format": 1, "id": "robot", "name": "Robot", "items": []}
    data.update(fields)
    return json.dumps(data).encode()


class ReadScreenTest(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(SCREENS.screens_topic(BASE), f"{BASE}/assist/screens")
        self.assertEqual(SCREENS.screen_topic(BASE, "robot"), f"{BASE}/assist/screen/robot")

    def test_a_good_file(self):
        found, problem = SCREENS.read_screen("robot.json", screen())
        self.assertIsNone(problem)
        self.assertEqual((found.id, found.name), ("robot", "Robot"))
        self.assertEqual(json.loads(found.text)["id"], "robot")
        self.assertEqual(len(found.digest), 16)

    def test_refusals_name_the_problem(self):
        cases = {
            b"x" * (SCREENS.MAX_SCREEN_BYTES + 1): "bytes",
            b"not json": "not a JSON",
            b"[1]": "not a JSON object",
            screen(format=None): "format",
            screen(format=True): "format",
            screen(id="Robot!"): "id",
            screen(id="atom"): "built-in",
        }
        for raw, words in cases.items():
            found, problem = SCREENS.read_screen("f.json", raw)
            self.assertIsNone(found)
            self.assertIn(words, problem)

    def test_names(self):
        self.assertEqual(SCREENS.read_screen("f.json", screen(name=None))[0].name, "robot")
        self.assertEqual(SCREENS.read_screen("f.json", screen(name="  "))[0].name, "robot")
        long = SCREENS.read_screen("f.json", screen(name="é" * 20))[0].name
        self.assertEqual(long, "é" * 16)  # 32 bytes of UTF-8, never half a character
        self.assertEqual(SCREENS.read_screen("f.json", screen(name="a\nb"))[0].name, "a b")


class ScanTest(unittest.TestCase):
    def test_scan_creates_sorts_and_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "hometiles", "assist_screens")
            self.assertEqual(SCREENS.scan_screens(folder), ([], []))
            self.assertTrue(os.path.isdir(folder))
            files = {
                "b.json": screen(id="retro_robot", name="Retro Robot"),
                "a.json": screen(id="robot", name="Robot"),
                "c.json": screen(id="robot", name="Copy"),
                "d.json": b"{",
                "notes.txt": b"ignored",
            }
            for name, raw in files.items():
                with open(os.path.join(folder, name), "wb") as handle:
                    handle.write(raw)
            found, problems = SCREENS.scan_screens(folder)
            self.assertEqual([s.id for s in found], ["retro_robot", "robot"])
            self.assertEqual(len(problems), 2)
            self.assertTrue(any("already used" in p for p in problems))

    def test_at_most_sixteen(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(SCREENS.MAX_SCREENS + 2):
                with open(os.path.join(tmp, f"s{i:02d}.json"), "wb") as handle:
                    handle.write(screen(id=f"s{i:02d}", name=f"S{i:02d}"))
            found, problems = SCREENS.scan_screens(tmp)
            self.assertEqual(len(found), SCREENS.MAX_SCREENS)
            self.assertEqual(len(problems), 2)

    def test_catalog(self):
        found = [SCREENS.read_screen("a", screen())[0]]
        self.assertEqual(json.loads(SCREENS.build_catalog(found)),
                         {"v": 1, "screens": [{"id": "robot", "name": "Robot"}]})
        self.assertEqual(json.loads(SCREENS.build_catalog([])), {"v": 1, "screens": []})


class CatalogPublishTest(unittest.IsolatedAsyncioTestCase):
    async def test_sends_changes_only_and_withdraws(self):
        sent = []

        async def publish(topic, payload, retain):
            sent.append((topic, payload, retain))

        catalog = SCREENS.AssistScreenCatalog(publish)
        robot = SCREENS.read_screen("a", screen())[0]
        retro = SCREENS.read_screen("b", screen(id="retro_robot", name="Retro"))[0]
        catalog.set_screens([robot, retro])
        await catalog.async_publish(BASE)
        self.assertEqual([t for t, _, _ in sent],
                         [SCREENS.screen_topic(BASE, "robot"), SCREENS.screen_topic(BASE, "retro_robot"),
                          SCREENS.screens_topic(BASE)])
        self.assertTrue(all(retain for _, _, retain in sent))

        # Unchanged files are not sent again; the catalog always is.
        sent.clear()
        await catalog.async_publish(BASE)
        self.assertEqual([t for t, _, _ in sent], [SCREENS.screens_topic(BASE)])

        # A withdrawn file gets an empty retained message.
        sent.clear()
        catalog.set_screens([robot])
        await catalog.async_publish(BASE)
        self.assertIn((SCREENS.screen_topic(BASE, "retro_robot"), "", True), sent)
        self.assertEqual(json.loads(sent[-1][1])["screens"], [{"id": "robot", "name": "Robot"}])

        # A panel that comes back gets everything again.
        sent.clear()
        catalog.forget(BASE)
        await catalog.async_publish(BASE)
        self.assertIn(SCREENS.screen_topic(BASE, "robot"), [t for t, _, _ in sent])


class StatusTest(unittest.TestCase):
    def test_file_ids_and_error(self):
        styles = ["atom", "reactor", "robot", "retro_robot"] + [f"f{i}" for i in range(14)]
        status = ASSIST.parse_status(json.dumps({"v": 1, "state": "idle", "screen_style": "robot",
                                                 "screen_styles": styles,
                                                 "screen_error": "line 3: a disc needs 'radius'"}))
        self.assertEqual(status["screen_styles"], styles)  # more than 16 ids fit
        self.assertEqual(status["screen_style"], "robot")
        self.assertEqual(status["screen_error"], "line 3: a disc needs 'radius'")
        status = ASSIST.parse_status(json.dumps({"v": 1, "state": "idle", "screen_style": "atom",
                                                 "screen_styles": styles, "screen_error": 3}))
        self.assertIsNone(status["screen_error"])


class WiringTest(unittest.TestCase):
    def test_setup_service_and_entries(self):
        init = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("await _async_setup_assist_screens(hass, domain_data)", init)
        self.assertIn("SERVICE_RELOAD_ASSIST_SCREENS, handle_reload", init)
        self.assertIn("await catalog.async_publish(bridge.base_topic)", init)
        self.assertIn("catalog.forget(bridge.base_topic)", init)
        services = (ROOT / "services.yaml").read_text(encoding="utf-8")
        self.assertIn("reload_assist_screens:", services)


if __name__ == "__main__":
    unittest.main()
