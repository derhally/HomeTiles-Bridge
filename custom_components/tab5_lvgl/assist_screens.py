"""Assist screens from Home Assistant: screen files kept in the HA config.

The module has no Home Assistant imports so the scanning, validation and
topic contract can be tested without a Home Assistant install. The panel side
is ``src/ui/assist/assist_screen_store.*`` and ``src/audio/assist_contract.h``
in the firmware; the screen file format is
``tools/assist-screen-lab/FORMAT.md`` there.

Users put screen files (``*.json``) in ``<config>/hometiles/assist_screens/``
and run the ``reload_assist_screens`` service (or restart Home Assistant).

MQTT contract (base = the panel base topic), on the direct link and MQTT:
- ``{base}/assist/screens``: Bridge to panel, retained JSON
  ``{"v": 1, "screens": [{"id": "robot", "name": "Robot"}, ...]}``: the
  screen files offered, at most ``MAX_SCREENS``. Empty when there are none.
- ``{base}/assist/screen/<id>``: Bridge to panel, retained, the file itself
  (at most ``MAX_SCREEN_BYTES``). The panel follows only the chosen one and
  keeps a copy; a withdrawn file gets an empty retained message.
- The panel lists the offered ids after its built-in styles in the Assist
  status ``"screen_styles"``, so the Assist screen select offers them, and
  reports ``"screen_error"`` when the chosen file was refused.

The Bridge only checks what it needs to offer a file (size, JSON, id, name);
the panel checks every limit of the format itself before it draws one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

ASSIST_SCREENS_DIR = ("hometiles", "assist_screens")
MAX_SCREEN_BYTES = 24 * 1024
MAX_SCREENS = 16
MAX_NAME_BYTES = 32
CATALOG_VERSION = 1
# The panel's built-in styles (firmware assist_contract.h kScreenStyleIds):
# a file cannot take their ids.
BUILT_IN_STYLES = ("atom", "reactor", "dot_halo", "voice_pills", "sentinel",
                   "phosphor", "light_disc", "lens", "iris")
_ID = re.compile(r"[a-z0-9_]{1,32}")


def screens_topic(base: str) -> str:
    return f"{base}/assist/screens"


def screen_topic(base: str, screen_id: str) -> str:
    return f"{base}/assist/screen/{screen_id}"


def valid_screen_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _short_name(name: str) -> str:
    """At most MAX_NAME_BYTES of UTF-8, never cutting a character."""
    name = "".join(" " if ord(ch) < 0x20 else ch for ch in name).strip()
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    return name


class ScreenFile(NamedTuple):
    id: str
    name: str
    text: str
    digest: str = ""


def read_screen(filename: str, raw: bytes) -> Tuple[Optional[ScreenFile], Optional[str]]:
    """A screen file, or None and why it is not offered (English, for the log)."""
    if len(raw) > MAX_SCREEN_BYTES:
        return None, f"{filename}: {len(raw)} bytes (limit {MAX_SCREEN_BYTES})"
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as err:
        return None, f"{filename}: not a JSON screen file ({err})"
    if not isinstance(data, dict):
        return None, f"{filename}: not a JSON object"
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt < 1:
        return None, f"{filename}: \"format\" is missing"
    screen_id = data.get("id")
    if not valid_screen_id(screen_id):
        return None, f"{filename}: \"id\" must be a-z, 0-9 and _, up to 32"
    if screen_id in BUILT_IN_STYLES:
        return None, f"{filename}: \"{screen_id}\" is a built-in style; give the file its own id"
    name = data.get("name")
    name = _short_name(name) if isinstance(name, str) else ""
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return ScreenFile(screen_id, name or screen_id, text, digest), None


def scan_screens(directory: str) -> Tuple[List[ScreenFile], List[str]]:
    """The screen files in directory (created when missing), sorted by name.

    Blocking (file I/O): run it in the executor. Returns the files offered and
    a log line for each file left out.
    """
    os.makedirs(directory, exist_ok=True)
    screens: List[ScreenFile] = []
    problems: List[str] = []
    seen = set()
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SCREEN_BYTES + 1)
        screen, problem = read_screen(filename, raw)
        if screen is None:
            problems.append(problem)
            continue
        if screen.id in seen:
            problems.append(f"{filename}: id \"{screen.id}\" is already used by another file")
            continue
        seen.add(screen.id)
        screens.append(screen)
    screens.sort(key=lambda s: (s.name.casefold(), s.id))
    if len(screens) > MAX_SCREENS:
        problems.extend(f"{s.id}: more than {MAX_SCREENS} screen files" for s in screens[MAX_SCREENS:])
        screens = screens[:MAX_SCREENS]
    return screens, problems


def build_catalog(screens: Iterable[ScreenFile]) -> str:
    return json.dumps({"v": CATALOG_VERSION,
                       "screens": [{"id": s.id, "name": s.name} for s in screens]},
                      separators=(",", ":"), ensure_ascii=False)


Publish = Callable[[str, str, bool], Awaitable[None]]


class AssistScreenCatalog:
    """The offered screen files and what each panel was sent.

    publish(topic, payload, retain) is the integration's MQTT publish (link
    and MQTT). Each panel gets the catalog and every file retained; files a
    panel no longer gets are withdrawn with an empty retained message, and
    unchanged files are not sent again.
    """

    def __init__(self, publish: Publish) -> None:
        self._publish = publish
        self._screens: List[ScreenFile] = []
        self._sent: Dict[str, Dict[str, str]] = {}  # base -> id -> digest

    @property
    def screens(self) -> List[ScreenFile]:
        return list(self._screens)

    def set_screens(self, screens: List[ScreenFile]) -> None:
        self._screens = list(screens)

    async def async_publish(self, base: str) -> None:
        sent = self._sent.setdefault(base, {})
        current = {s.id: s for s in self._screens}
        for screen_id in [i for i in sent if i not in current]:
            await self._publish(screen_topic(base, screen_id), "", True)
            del sent[screen_id]
        for screen in self._screens:
            if sent.get(screen.id) != screen.digest:
                await self._publish(screen_topic(base, screen.id), screen.text, True)
                sent[screen.id] = screen.digest
        await self._publish(screens_topic(base), build_catalog(self._screens), True)

    def forget(self, base: str) -> None:
        """A panel left (entry unloaded): send everything again next time."""
        self._sent.pop(base, None)
