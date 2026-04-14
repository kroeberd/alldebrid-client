import unittest
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *args, **kwargs: None,
        ClientSession=object,
        FormData=object,
        ClientError=Exception,
    )
if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *args, **kwargs: None)
if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(Connection=object, Row=object, connect=lambda *args, **kwargs: None)
if "pydantic" not in sys.modules:
    class _FakeBaseModel:
        model_fields = {}

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            cls.model_fields = dict(getattr(cls, "__annotations__", {}))

        def __init__(self, **kwargs):
            for key, value in self.__class__.__dict__.items():
                if not key.startswith("_") and not callable(value):
                    setattr(self, key, value)
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self):
            return self.__dict__.copy()

        def model_copy(self, update=None):
            data = self.model_dump()
            if update:
                data.update(update)
            return self.__class__(**data)

    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=_FakeBaseModel)

from services.alldebrid import flatten_files
from services.manager_v2 import normalize_provider_state, safe_rel_path


class ManagerV2Tests(unittest.TestCase):
    def test_flatten_files_preserves_nested_path(self):
        nodes = [
            {
                "n": "Season 01",
                "e": [
                    {"n": "Episode 01.mkv", "s": 123, "l": "https://example.invalid/1"},
                ],
            }
        ]

        flat = flatten_files(nodes)

        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["path"], "Season 01/Episode 01.mkv")

    def test_safe_rel_path_sanitizes_segments(self):
        path = safe_rel_path("../Season 01/Bad:Name?.mkv")
        self.assertEqual(str(path).replace("\\", "/"), "Season 01/Bad_Name_.mkv")

    def test_normalize_provider_state_ready(self):
        state = normalize_provider_state({"statusCode": 4, "size": 200, "downloaded": 200, "status": "Ready"})
        self.assertEqual(state["provider_status"], "ready")
        self.assertEqual(state["local_status"], "ready")
        self.assertEqual(int(state["progress"]), 100)

    def test_normalize_provider_state_error(self):
        state = normalize_provider_state({"statusCode": 8, "size": 200, "downloaded": 10, "status": "Error"})
        self.assertEqual(state["provider_status"], "error")
        self.assertEqual(state["local_status"], "error")


if __name__ == "__main__":
    unittest.main()
