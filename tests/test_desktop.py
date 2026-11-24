"""Unit tests for hs_coach.desktop."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from fastapi.testclient import TestClient

from hs_coach.cards import CardDB
from hs_coach.desktop import (
    STARTUP_RUN_KEY,
    STARTUP_VALUE_NAME,
    BackgroundWatcherService,
    DesktopConfig,
    create_tray_icon_image,
    ensure_hearthstone_log_config,
    get_windows_startup_enabled,
    is_hearthstone_log_config_ready,
    load_desktop_config,
    save_desktop_config,
    set_windows_startup_enabled,
)
from hs_coach.storage import GameStore
from hs_coach.uploader import PowerLogWatcher
from hs_coach.web import create_app


def _test_db() -> CardDB:
    return CardDB.from_records(
        [
            {"id": "HERO_08", "dbfId": 1, "name": "Jaina Proudmoore", "type": "HERO", "cardClass": "MAGE", "health": 30},
            {"id": "HERO_01", "dbfId": 2, "name": "Garrosh Hellscream", "type": "HERO", "cardClass": "WARRIOR", "health": 30},
        ]
    )


class TestDesktop(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_config_roundtrip_and_icon_image(self) -> None:
        cfg_path = self.root / "desktop_config.json"
        orig = DesktopConfig(
            logs_dir=r"D:\Custom\Logs",
            server_url="https://coach.example.com",
            upload_token="hsc_abc123",
            poll_interval=10.0,
            auto_coach=True,
            model="google/gemini-3.7-flash",
        )
        save_desktop_config(cfg_path, orig)
        loaded = load_desktop_config(cfg_path)
        self.assertEqual(loaded, orig)

        img = create_tray_icon_image(64)
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.mode, "RGBA")

    def test_background_watcher_service_lifecycle(self) -> None:
        db_path = self.root / "coach.db"
        store = GameStore(db_path)
        app = create_app(_test_db(), store)
        tc = TestClient(app)

        log_dir = self.root / "Logs" / "Hearthstone_2026_11_23_16_00_00"
        log_dir.mkdir(parents=True)
        (log_dir / "Power.log").write_text(
            "\n".join(
                [
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - CREATE_GAME",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - GameEntity EntityID=1",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=2]",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=FIRST_PLAYER value=1",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - Player EntityID=3 PlayerID=2 GameAccountId=[hi=1 lo=3]",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=HERO_08",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=1",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=HERO_01",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CONTROLLER value=2",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=ZONE value=PLAY",
                    "D 10:05:00.0000000 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO",
                    "D 10:05:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
                    "D 10:05:00.0000000 GameState.DebugPrintGame() - FormatType=FT_STANDARD",
                    "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName=SecretUser#9999",
                    "D 10:05:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=1",
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1",
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
                    "D 10:05:01.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
                    "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=SecretUser#9999 tag=PLAYSTATE value=WON",
                    "D 10:05:02.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST",
                ]
            ),
            encoding="utf-8",
        )

        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda req: (
                    lambda r: httpx.Response(
                        r.status_code, content=r.content, headers=dict(r.headers)
                    )
                )(
                    tc.request(
                        req.method,
                        req.url.path,
                        content=req.content,
                        headers=dict(req.headers),
                    )
                )
            )
        )
        watcher = PowerLogWatcher(
            self.root / "Logs",
            _test_db(),
            "http://127.0.0.1:8000",
            self.root / "state.json",
            client=client,
        )
        svc = BackgroundWatcherService(watcher, interval_seconds=60.0)
        try:
            svc.start()
            self.assertTrue(svc.status().running)
            svc.pause()
            self.assertTrue(svc.status().paused)
            svc.resume()
            self.assertFalse(svc.status().paused)

            svc.scan_now()
            st = svc.status()
            self.assertGreaterEqual(st.scans_completed, 1)
            self.assertEqual(st.total_uploaded, 1)
            self.assertIsNone(st.last_error)
            self.assertNotIn(b"SecretUser", db_path.read_bytes())
        finally:
            svc.stop()
            self.assertFalse(svc.status().running)
            client.close()
            store.close()

    def test_ensure_hearthstone_log_config_creates_and_preserves_sections(self) -> None:
        cfg_path = self.root / "Blizzard" / "Hearthstone" / "log.config"
        self.assertFalse(is_hearthstone_log_config_ready(cfg_path))

        res1 = ensure_hearthstone_log_config(cfg_path)
        self.assertEqual(res1, {"path": str(cfg_path), "created": True, "updated": True})
        self.assertTrue(is_hearthstone_log_config_ready(cfg_path))
        txt1 = cfg_path.read_text(encoding="utf-8")
        self.assertIn("[Power]", txt1)
        self.assertIn("LogLevel=1", txt1)
        self.assertIn("FilePrinting=True", txt1)
        self.assertIn("Verbose=True", txt1)

        # Idempotent when already configured
        res2 = ensure_hearthstone_log_config(cfg_path)
        self.assertEqual(res2, {"path": str(cfg_path), "created": False, "updated": False})

        # Preserves unrelated sections while fixing partial/disabled [Power] section
        cfg_path.write_text(
            "[Zone]\nLogLevel=1\nFilePrinting=True\n\n[Power]\nLogLevel=1\nFilePrinting=True\nVerbose=False\n",
            encoding="utf-8",
        )
        self.assertFalse(is_hearthstone_log_config_ready(cfg_path))
        res3 = ensure_hearthstone_log_config(cfg_path)
        self.assertEqual(res3, {"path": str(cfg_path), "created": False, "updated": True})
        self.assertTrue(is_hearthstone_log_config_ready(cfg_path))
        updated_txt = cfg_path.read_text(encoding="utf-8")
        self.assertIn("[Zone]", updated_txt)
        self.assertIn("Verbose=True", updated_txt)

    def test_windows_startup_toggle_with_mock_registry(self) -> None:
        registry_store: dict[tuple[str, str], dict[str, tuple[int, str]]] = {}

        class _FakeKey:
            def __init__(self, root: str, subkey: str) -> None:
                self.root = root
                self.subkey = subkey

            def __enter__(self) -> _FakeKey:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        class _FakeWinreg:
            HKEY_CURRENT_USER = "HKCU"
            KEY_READ = 1
            KEY_SET_VALUE = 2
            REG_SZ = 1

            @staticmethod
            def OpenKey(root: str, subkey: str, _res: int, _access: int) -> _FakeKey:
                if (root, subkey) not in registry_store:
                    raise FileNotFoundError(subkey)
                return _FakeKey(root, subkey)

            @staticmethod
            def CreateKeyEx(root: str, subkey: str, _res: int, _access: int) -> _FakeKey:
                registry_store.setdefault((root, subkey), {})
                return _FakeKey(root, subkey)

            @staticmethod
            def QueryValueEx(key: _FakeKey, name: str) -> tuple[str, int]:
                bucket = registry_store.get((key.root, key.subkey), {})
                if name not in bucket:
                    raise FileNotFoundError(name)
                reg_type, val = bucket[name]
                return val, reg_type

            @staticmethod
            def SetValueEx(key: _FakeKey, name: str, _res: int, reg_type: int, value: str) -> None:
                registry_store[(key.root, key.subkey)][name] = (reg_type, value)

            @staticmethod
            def DeleteValue(key: _FakeKey, name: str) -> None:
                bucket = registry_store.get((key.root, key.subkey), {})
                if name not in bucket:
                    raise FileNotFoundError(name)
                del bucket[name]

        fake_wr = _FakeWinreg()
        self.assertFalse(get_windows_startup_enabled(_winreg=fake_wr))

        enabled = set_windows_startup_enabled(
            True, command='"C:\\HS-Coach\\HS-Coach-Uploader.exe"', _winreg=fake_wr
        )
        self.assertTrue(enabled)
        self.assertTrue(get_windows_startup_enabled(_winreg=fake_wr))
        self.assertEqual(
            registry_store[("HKCU", STARTUP_RUN_KEY)][STARTUP_VALUE_NAME][1],
            '"C:\\HS-Coach\\HS-Coach-Uploader.exe"',
        )

        disabled = set_windows_startup_enabled(False, _winreg=fake_wr)
        self.assertFalse(disabled)
        self.assertFalse(get_windows_startup_enabled(_winreg=fake_wr))
        # Disabling again when already absent stays idempotent
        self.assertFalse(set_windows_startup_enabled(False, _winreg=fake_wr))

    def test_update_config_resets_cache_only_on_server_change(self) -> None:
        state_path = self.root / "uploader_state.json"
        watcher = PowerLogWatcher(
            self.root / "Logs",
            _test_db(),
            "http://127.0.0.1:8000",
            state_path,
            token="hsc_old",
        )
        watcher.state.mark_uploaded("game_1")
        watcher._file_fingerprints[self.root / "Logs" / "Power.log"] = (123, 456)
        svc = BackgroundWatcherService(watcher)

        # Updating only upload_token preserves uploaded_game_ids but clears file fingerprints
        token_only_cfg = DesktopConfig(
            logs_dir=str(self.root / "Logs"),
            server_url="http://127.0.0.1:8000",
            upload_token="hsc_new",
            state_path=str(state_path),
        )
        self.assertFalse(svc.update_config(token_only_cfg))
        self.assertEqual(watcher.token, "hsc_new")
        self.assertEqual(watcher.state.uploaded_game_ids, {"game_1"})
        self.assertEqual(watcher._file_fingerprints, {})

        # Changing server_url resets uploaded_game_ids (in memory & on disk) and file fingerprints
        watcher._file_fingerprints[self.root / "Logs" / "Power.log"] = (123, 456)
        new_server_cfg = DesktopConfig(
            logs_dir=str(self.root / "Logs"),
            server_url="https://hs-coach.onrender.com",
            upload_token="hsc_new",
            state_path=str(state_path),
        )
        self.assertTrue(svc.update_config(new_server_cfg))
        self.assertEqual(watcher.server_url, "https://hs-coach.onrender.com")
        self.assertEqual(watcher.state.uploaded_game_ids, set())
        self.assertEqual(watcher._file_fingerprints, {})

        reloaded = PowerLogWatcher(
            self.root / "Logs",
            _test_db(),
            "https://hs-coach.onrender.com",
            state_path,
        )
        self.assertEqual(reloaded.state.uploaded_game_ids, set())


if __name__ == "__main__":
    unittest.main()
