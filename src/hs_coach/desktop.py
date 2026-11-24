"""Thin 100% post-game desktop uploader app (Windows system tray + tkinter fallback)."""

from __future__ import annotations

import configparser
import json
import os
import sys
import tempfile
import threading
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hs_coach.cards import CardDB, resolve_cards_path, sync_cards_json
from hs_coach.uploader import (
    DEFAULT_LOGS_DIR,
    PowerLogWatcher,
    UploadScanResult,
    validate_server_url,
)


def default_app_data_dir() -> Path:
    """Return %LOCALAPPDATA%/HS-Coach when frozen as an exe, else ./data."""
    if getattr(sys, "frozen", False):
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "HS-Coach"
        return Path.home() / ".hs_coach"
    return Path("data")


STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "HS-Coach-Uploader"
_POWER_LOG_SETTINGS: tuple[tuple[str, str], ...] = (
    ("LogLevel", "1"),
    ("FilePrinting", "True"),
    ("ConsolePrinting", "False"),
    ("ScreenPrinting", "False"),
    ("Verbose", "True"),
)


def default_hearthstone_log_config_path() -> Path:
    """Return the default Hearthstone log.config path for the current OS."""
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
        return base / "Blizzard" / "Hearthstone" / "log.config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "Blizzard" / "Hearthstone" / "log.config"
    return Path.home() / ".config" / "Blizzard" / "Hearthstone" / "log.config"


def _read_ini(target: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]
    if target.is_file():
        parser.read(target, encoding="utf-8-sig")
    return parser


def is_hearthstone_log_config_ready(config_path: str | Path | None = None) -> bool:
    """Return True if Hearthstone log.config already enables [Power] FilePrinting and Verbose."""
    target = Path(config_path) if config_path is not None else default_hearthstone_log_config_path()
    if not target.is_file():
        return False
    parser = _read_ini(target)
    if not parser.has_section("Power"):
        return False
    vals = {k.lower(): v.strip() for k, v in parser["Power"].items()}
    return (
        vals.get("loglevel") == "1"
        and vals.get("fileprinting", "").lower() == "true"
        and vals.get("verbose", "").lower() == "true"
    )


def ensure_hearthstone_log_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Create or update [Power] logging in Hearthstone's log.config while preserving other sections."""
    target = Path(config_path) if config_path is not None else default_hearthstone_log_config_path()
    existed = target.is_file()
    parser = _read_ini(target)

    if existed and parser.has_section("Power"):
        existing_lower = {k.lower(): v.strip() for k, v in parser["Power"].items()}
        if all(
            (
                existing_lower.get(k.lower(), "") == val
                if k == "LogLevel"
                else existing_lower.get(k.lower(), "").lower() == val.lower()
            )
            for k, val in _POWER_LOG_SETTINGS
        ):
            return {"path": str(target), "created": False, "updated": False}

    if not parser.has_section("Power"):
        parser.add_section("Power")

    required_lower = {k.lower() for k, _ in _POWER_LOG_SETTINGS}
    for existing_key in list(parser["Power"].keys()):
        if existing_key.lower() in required_lower and existing_key not in dict(_POWER_LOG_SETTINGS):
            del parser["Power"][existing_key]
    for key, val in _POWER_LOG_SETTINGS:
        parser["Power"][key] = val

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".log_config_", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            parser.write(fh, space_around_delimiters=False)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return {"path": str(target), "created": not existed, "updated": True}


def build_startup_command() -> str:
    """Return the executable command string for Windows Run startup registration."""
    exe = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return f'"{exe}"'
    return f'"{exe}" -m hs_coach tray'


def _resolve_winreg(_winreg: Any) -> Any:
    if _winreg is not None:
        return _winreg
    if os.name != "nt":
        return None
    import winreg

    return winreg


def get_windows_startup_enabled(*, _winreg: Any = None) -> bool:
    """Return True if HS-Coach-Uploader is registered in HKCU Run startup."""
    wr = _resolve_winreg(_winreg)
    if wr is None:
        return False
    try:
        with wr.OpenKey(
            wr.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, wr.KEY_READ
        ) as key:
            val, _ = wr.QueryValueEx(key, STARTUP_VALUE_NAME)
            return bool(str(val).strip())
    except OSError:
        return False


def set_windows_startup_enabled(
    enabled: bool,
    *,
    command: str | None = None,
    _winreg: Any = None,
) -> bool:
    """Enable or disable HKCU Run startup registration for the uploader."""
    wr = _resolve_winreg(_winreg)
    if wr is None:
        raise RuntimeError("Windows Startup toggle is only supported on Windows.")
    if enabled:
        cmd = command or build_startup_command()
        with wr.CreateKeyEx(
            wr.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, wr.KEY_SET_VALUE
        ) as key:
            wr.SetValueEx(key, STARTUP_VALUE_NAME, 0, wr.REG_SZ, cmd)
        return True
    try:
        with wr.OpenKey(
            wr.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, wr.KEY_SET_VALUE
        ) as key:
            wr.DeleteValue(key, STARTUP_VALUE_NAME)
    except FileNotFoundError:
        pass
    return False


@dataclass
class DesktopConfig:
    logs_dir: str = str(DEFAULT_LOGS_DIR)
    server_url: str = "http://127.0.0.1:8000"
    upload_token: str = ""
    cards_path: str = field(default_factory=lambda: str(default_app_data_dir() / "cards.json"))
    state_path: str = field(default_factory=lambda: str(default_app_data_dir() / "uploader_state.json"))
    poll_interval: float = 5.0
    auto_coach: bool = False
    model: str = ""


def load_desktop_config(path: str | Path) -> DesktopConfig:
    """Load desktop uploader settings from JSON, falling back to defaults."""
    p = Path(path)
    defaults = DesktopConfig()
    if not p.is_file():
        return defaults
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    return DesktopConfig(
        logs_dir=str(data.get("logs_dir", defaults.logs_dir)),
        server_url=str(data.get("server_url", defaults.server_url)).rstrip("/"),
        upload_token=str(data.get("upload_token", "")),
        cards_path=str(data.get("cards_path", defaults.cards_path)),
        state_path=str(data.get("state_path", defaults.state_path)),
        poll_interval=float(data.get("poll_interval", 5.0)),
        auto_coach=bool(data.get("auto_coach", False)),
        model=str(data.get("model", "")),
    )


def save_desktop_config(path: str | Path, cfg: DesktopConfig) -> None:
    """Validate and atomically write desktop uploader settings to JSON."""
    cfg.server_url = validate_server_url(cfg.server_url)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".desktop_cfg_", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, p)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


@dataclass
class WatcherStatus:
    running: bool = False
    paused: bool = False
    scans_completed: int = 0
    total_uploaded: int = 0
    last_uploaded_ids: list[str] = field(default_factory=list)
    last_scan_at: str | None = None
    last_error: str | None = None


class BackgroundWatcherService:
    """Thread-safe background controller around PowerLogWatcher."""

    def __init__(
        self,
        watcher: PowerLogWatcher,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        self.watcher = watcher
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = WatcherStatus()

    def status(self) -> WatcherStatus:
        with self._lock:
            return WatcherStatus(
                running=self._status.running,
                paused=self._status.paused,
                scans_completed=self._status.scans_completed,
                total_uploaded=self._status.total_uploaded,
                last_uploaded_ids=list(self._status.last_uploaded_ids),
                last_scan_at=self._status.last_scan_at,
                last_error=self._status.last_error,
            )

    def pause(self) -> None:
        with self._lock:
            self._status.paused = True

    def resume(self) -> None:
        with self._lock:
            self._status.paused = False
        self._wake_event.set()

    def scan_now(self) -> UploadScanResult:
        with self._scan_lock:
            res = self.watcher.scan_once()
        now_iso = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        with self._lock:
            self._status.scans_completed += 1
            self._status.last_scan_at = now_iso
            self._status.total_uploaded += res.new_games_uploaded
            if res.uploaded_ids:
                self._status.last_uploaded_ids = list(res.uploaded_ids)
            self._status.last_error = "; ".join(res.errors) if res.errors else None
        return res

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.status().paused:
                try:
                    self.scan_now()
                except Exception as exc:
                    with self._lock:
                        self._status.last_error = str(exc)
            self._wake_event.wait(timeout=self.interval_seconds)
            self._wake_event.clear()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._status.running = True
            self._thread = threading.Thread(
                target=self._loop, name="hs-coach-watcher", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        with self._lock:
            self._status.running = False


def create_tray_icon_image(size: int = 64) -> Any:
    """Generate a dark-slate & gold 'HS' RGBA icon image using Pillow."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (15, 20, 31, 255))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    draw.rounded_rectangle(
        (pad, pad, size - pad - 1, size - pad - 1),
        radius=max(6, size // 5),
        fill=(23, 31, 46, 255),
        outline=(240, 185, 73, 255),
        width=max(2, size // 16),
    )
    # Minimal geometric 'HS' badge centered cleanly without font-file dependencies
    cx = size // 2
    cy = size // 2
    draw.ellipse(
        (cx - size // 5, cy - size // 5, cx + size // 5, cy + size // 5),
        fill=(240, 185, 73, 255),
    )
    draw.ellipse(
        (cx - size // 10, cy - size // 10, cx + size // 10, cy + size // 10),
        fill=(15, 20, 31, 255),
    )
    return img


def _load_or_sync_cards(cards_path: Path) -> CardDB:
    if not cards_path.exists():
        sync_cards_json(cards_path)
    return CardDB.from_file(cards_path)


def _build_service(cfg: DesktopConfig) -> BackgroundWatcherService:
    cards_p = resolve_cards_path(cfg.cards_path)
    db = _load_or_sync_cards(cards_p)
    watcher = PowerLogWatcher(
        logs_dir=cfg.logs_dir,
        card_db=db,
        server_url=cfg.server_url,
        state_path=cfg.state_path,
        token=cfg.upload_token or None,
        auto_coach=cfg.auto_coach,
        model=cfg.model or None,
    )
    return BackgroundWatcherService(watcher, interval_seconds=cfg.poll_interval)


def _run_tkinter_window(cfg_path: Path, cfg: DesktopConfig, service: BackgroundWatcherService) -> None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.title("HS-Coach — Post-Game Uploader")
    root.geometry("560x415")
    root.configure(bg="#0f141f")
    root.resizable(False, False)

    fg = "#e6ecf5"
    muted = "#9aa7bd"
    surface = "#171f2e"
    accent = "#f0b949"

    header = tk.Label(
        root,
        text="HS-Coach · Post-Game Auto-Uploader",
        bg="#0f141f",
        fg=accent,
        font=("Segoe UI", 13, "bold"),
    )
    header.pack(anchor="w", padx=16, pady=(14, 2))

    sub = tk.Label(
        root,
        text="100% Post-Game Only · BattleTags stripped locally before upload",
        bg="#0f141f",
        fg=muted,
        font=("Segoe UI", 9),
    )
    sub.pack(anchor="w", padx=16, pady=(0, 10))

    form = tk.Frame(root, bg=surface, bd=1, relief="solid")
    form.pack(fill="x", padx=16, pady=4)

    server_var = tk.StringVar(value=cfg.server_url)
    logs_var = tk.StringVar(value=cfg.logs_dir)
    token_var = tk.StringVar(value=cfg.upload_token)

    def _row(r: int, label: str, var: tk.StringVar, *, show: str = "") -> tk.Entry:
        tk.Label(form, text=label, bg=surface, fg=muted, font=("Segoe UI", 9)).grid(
            row=r, column=0, sticky="w", padx=10, pady=6
        )
        ent = tk.Entry(
            form,
            textvariable=var,
            width=44,
            bg="#0f141f",
            fg=fg,
            insertbackground=fg,
            show=show,
        )
        ent.grid(row=r, column=1, sticky="ew", padx=(4, 10), pady=6)
        return ent

    _row(0, "Server URL:", server_var)
    _row(1, "Logs Folder:", logs_var)
    _row(2, "Upload Token:", token_var, show="*")

    def _browse_logs() -> None:
        chosen = filedialog.askdirectory(initialdir=logs_var.get() or "D:\\")
        if chosen:
            logs_var.set(chosen)

    tk.Button(
        form,
        text="Browse",
        command=_browse_logs,
        bg="#1e283b",
        fg=fg,
        relief="flat",
    ).grid(row=1, column=2, padx=(0, 10))

    status_var = tk.StringVar(value="Starting watcher...")
    status_lbl = tk.Label(
        root,
        textvariable=status_var,
        bg="#0f141f",
        fg="#38bdf8",
        font=("Segoe UI", 9),
        justify="left",
    )
    status_lbl.pack(anchor="w", padx=16, pady=(10, 8))

    btns = tk.Frame(root, bg="#0f141f")
    btns.pack(fill="x", padx=16, pady=4)

    pause_btn_var = tk.StringVar(value="Pause")

    def _apply_settings() -> None:
        candidate_url = server_var.get().strip().rstrip("/") or cfg.server_url
        try:
            validated_url = validate_server_url(candidate_url)
        except ValueError as exc:
            status_var.set(f"Error: {exc}")
            return
        cfg.server_url = validated_url
        cfg.logs_dir = logs_var.get().strip() or cfg.logs_dir
        cfg.upload_token = token_var.get().strip()
        save_desktop_config(cfg_path, cfg)
        service.watcher.server_url = cfg.server_url
        service.watcher.logs_dir = Path(cfg.logs_dir)
        service.watcher.token = cfg.upload_token or None
        status_var.set("Settings saved.")

    def _scan_now() -> None:
        _apply_settings()
        threading.Thread(target=service.scan_now, daemon=True).start()

    def _toggle_pause() -> None:
        if service.status().paused:
            service.resume()
            pause_btn_var.set("Pause")
        else:
            service.pause()
            pause_btn_var.set("Resume")

    def _open_dash() -> None:
        webbrowser.open(server_var.get().strip() or cfg.server_url)

    tk.Button(btns, text="Scan Now", command=_scan_now, bg=accent, fg="#111827", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
    tk.Button(btns, textvariable=pause_btn_var, command=_toggle_pause, bg="#1e283b", fg=fg).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="Save Settings", command=_apply_settings, bg="#1e283b", fg=fg).pack(side="left", padx=(0, 8))
    tk.Button(btns, text="Open Dashboard", command=_open_dash, bg="#1e283b", fg="#38bdf8").pack(side="right")

    setup_row = tk.Frame(root, bg="#0f141f")
    setup_row.pack(fill="x", padx=16, pady=(8, 0))

    log_btn_var = tk.StringVar(
        value="Power.log Enabled" if is_hearthstone_log_config_ready() else "Enable Power.log"
    )

    def _enable_power_log() -> None:
        try:
            ensure_hearthstone_log_config()
            log_btn_var.set("Power.log Enabled")
            status_var.set("Hearthstone log.config enabled (restart Hearthstone if running).")
        except Exception as exc:
            status_var.set(f"log.config error: {exc}")

    tk.Button(
        setup_row,
        textvariable=log_btn_var,
        command=_enable_power_log,
        bg="#1e283b",
        fg="#27c07d",
    ).pack(side="left", padx=(0, 12))

    if os.name == "nt":
        startup_var = tk.BooleanVar(value=get_windows_startup_enabled())

        def _toggle_startup() -> None:
            try:
                enabled = set_windows_startup_enabled(bool(startup_var.get()))
                status_var.set(
                    "Start with Windows enabled." if enabled else "Start with Windows disabled."
                )
            except Exception as exc:
                status_var.set(f"Startup setting error: {exc}")

        tk.Checkbutton(
            setup_row,
            text="Start with Windows",
            variable=startup_var,
            command=_toggle_startup,
            bg="#0f141f",
            fg=fg,
            selectcolor="#1e283b",
            activebackground="#0f141f",
            activeforeground=fg,
        ).pack(side="left")

    def _poll_ui() -> None:
        st = service.status()
        state_lbl = "PAUSED" if st.paused else ("RUNNING" if st.running else "STOPPED")
        err_s = f" | Err: {st.last_error}" if st.last_error else ""
        status_var.set(
            f"State: {state_lbl} | Scans: {st.scans_completed} | Uploaded: {st.total_uploaded} | Last scan: {st.last_scan_at or '-'}{err_s}"
        )
        root.after(1000, _poll_ui)

    def _on_close() -> None:
        service.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    service.start()
    _poll_ui()
    root.mainloop()


def run_desktop_app(
    config_path: str | Path | None = None,
    *,
    logs_dir: str | None = None,
    server_url: str | None = None,
    cards_path: str | None = None,
    upload_token: str | None = None,
    poll_interval: float | None = None,
    auto_coach: bool | None = None,
    model: str | None = None,
    force_window: bool = False,
) -> None:
    """Launch the post-game desktop uploader (native system tray if pystray available, else tkinter window)."""
    cfg_p = Path(config_path) if config_path is not None else (default_app_data_dir() / "desktop_config.json")
    cfg = load_desktop_config(cfg_p)
    if logs_dir is not None:
        cfg.logs_dir = logs_dir
    if server_url is not None:
        cfg.server_url = server_url.rstrip("/")
    if cards_path is not None:
        cfg.cards_path = cards_path
    if upload_token is not None:
        cfg.upload_token = upload_token
    if poll_interval is not None:
        cfg.poll_interval = poll_interval
    if auto_coach is not None:
        cfg.auto_coach = auto_coach
    if model is not None:
        cfg.model = model

    save_desktop_config(cfg_p, cfg)
    service = _build_service(cfg)

    if not force_window:
        try:
            import pystray  # type: ignore[import-not-found]
        except ImportError:
            pystray = None
        if pystray is not None:
            def _status_label(_: Any) -> str:
                st = service.status()
                return f"Uploaded: {st.total_uploaded} ({'Paused' if st.paused else 'Watching'})"

            def _toggle(_: Any, __: Any) -> None:
                if service.status().paused:
                    service.resume()
                else:
                    service.pause()

            def _quit(icon: Any, _: Any) -> None:
                service.stop()
                icon.stop()

            menu = pystray.Menu(
                pystray.MenuItem(_status_label, None, enabled=False),
                pystray.MenuItem("Scan Now", lambda _, __: service.scan_now()),
                pystray.MenuItem(
                    "Enable Power.log",
                    lambda _, __: ensure_hearthstone_log_config(),
                ),
                pystray.MenuItem(
                    lambda _: "Resume" if service.status().paused else "Pause",
                    _toggle,
                ),
                pystray.MenuItem(
                    "Open Dashboard",
                    lambda _, __: webbrowser.open(cfg.server_url),
                ),
                pystray.MenuItem("Quit", _quit),
            )
            icon = pystray.Icon(
                "hs_coach",
                create_tray_icon_image(64),
                "HS-Coach Post-Game Uploader",
                menu,
            )
            service.start()
            icon.run()
            return

    _run_tkinter_window(cfg_p, cfg, service)
