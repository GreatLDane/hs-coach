"""Thin post-game local Power.log watcher that strips BattleTags locally and uploads anonymized GameReplay JSON."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from hs_coach.cards import CardDB
from hs_coach.config import is_loopback_host
from hs_coach.parser import parse_power_log

DEFAULT_LOGS_DIR = Path(r"D:\Hearthstone\Logs")


def validate_server_url(server_url: str) -> str:
    """Require https:// for non-loopback uploader destinations; allow http:// only for loopback."""
    cleaned = server_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip()
    if scheme not in ("http", "https") or not host:
        raise ValueError(f"Invalid HS-Coach server URL: {server_url!r}")
    if scheme == "http" and not is_loopback_host(host):
        raise ValueError(
            f"Non-loopback server URL {cleaned!r} must use https:// to protect upload tokens."
        )
    return cleaned


class UploaderState:
    """Persistent store of already-uploaded anonymized game_ids."""

    def __init__(self, state_path: str | Path) -> None:
        self.path = Path(state_path)
        self.uploaded_game_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ids = data.get("uploaded_game_ids", [])
            if isinstance(ids, list):
                self.uploaded_game_ids = {str(x) for x in ids if x}
        except Exception:
            self.uploaded_game_ids = set()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"uploaded_game_ids": sorted(self.uploaded_game_ids)},
            indent=2,
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=".uploader_state_", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.path)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def mark_uploaded(self, game_id: str) -> None:
        self.uploaded_game_ids.add(game_id)
        self.save()

    def reset(self) -> None:
        """Clear all recorded uploaded game IDs and persist the empty state."""
        self.uploaded_game_ids.clear()
        self.save()


def find_power_logs(logs_dir: str | Path, *, max_files: int = 3) -> list[Path]:
    """Return newest Hearthstone Power.log path(s) from a file, session dir, or Logs root."""
    root = Path(logs_dir)
    if not root.exists():
        return []
    if root.is_file():
        return [root]

    candidates: list[Path] = []
    direct = root / "Power.log"
    if direct.is_file():
        candidates.append(direct)

    for sub in root.glob("Hearthstone_*"):
        plog = sub / "Power.log"
        if plog.is_file():
            candidates.append(plog)

    candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    return candidates[:max_files]


@dataclass
class UploadScanResult:
    files_scanned: int = 0
    new_games_uploaded: int = 0
    skipped_already_uploaded: int = 0
    errors: list[str] = field(default_factory=list)
    uploaded_ids: list[str] = field(default_factory=list)


class PowerLogWatcher:
    """Polls Hearthstone Power.log files and uploads completed anonymized Standard replays."""

    def __init__(
        self,
        logs_dir: str | Path,
        card_db: CardDB,
        server_url: str,
        state_path: str | Path,
        *,
        token: str | None = None,
        auto_coach: bool = False,
        model: str | None = None,
        max_files: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.card_db = card_db
        self.server_url = validate_server_url(server_url)
        self.state = UploaderState(state_path)
        self.token = (token or os.environ.get("HS_COACH_UPLOAD_TOKEN", "")).strip() or None
        self.auto_coach = auto_coach
        self.model = model
        self.max_files = max_files
        self._client = client
        self._file_fingerprints: dict[Path, tuple[int, int]] = {}

    def update_target(
        self,
        *,
        server_url: str | None = None,
        logs_dir: str | Path | None = None,
        token: str | None = None,
    ) -> bool:
        """Update watcher target settings and reset cached uploads if server_url changes.

        Returns True when a Server URL change cleared the uploaded-games cache.
        """
        server_changed = False
        changed_any = False
        if server_url is not None:
            new_url = validate_server_url(server_url)
            server_changed = new_url != self.server_url
            self.server_url = new_url
            changed_any = True
        if logs_dir is not None:
            self.logs_dir = Path(logs_dir)
            changed_any = True
        if token is not None:
            self.token = token.strip() or None
            changed_any = True
        if changed_any:
            self._file_fingerprints.clear()
        if server_changed:
            self.state.reset()
        return server_changed

    def scan_once(self) -> UploadScanResult:
        """Scan changed Power.log files once and upload newly completed Standard games."""
        result = UploadScanResult()
        log_files = find_power_logs(self.logs_dir, max_files=self.max_files)
        if not log_files:
            return result

        owns_client = self._client is None
        http = self._client or httpx.Client(timeout=120.0)
        try:
            for path in log_files:
                try:
                    st = path.stat()
                except OSError as exc:
                    result.errors.append(f"{path}: {exc}")
                    continue

                fp = (st.st_mtime_ns, st.st_size)
                if self._file_fingerprints.get(path) == fp:
                    continue

                result.files_scanned += 1
                parsed = parse_power_log(path, self.card_db, standard_only=True)
                file_had_error = False

                for replay in parsed.replays:
                    gid = replay.metadata.game_id
                    if gid in self.state.uploaded_game_ids:
                        result.skipped_already_uploaded += 1
                        continue

                    headers: dict[str, str] = {"content-type": "application/json"}
                    if self.token:
                        headers["Authorization"] = f"Bearer {self.token}"

                    params: dict[str, str] = {}
                    if self.auto_coach:
                        params["auto_coach"] = "true"
                    if self.model:
                        params["model"] = self.model

                    try:
                        resp = http.post(
                            f"{self.server_url}/api/replays",
                            content=replay.model_dump_json(),
                            headers=headers,
                            params=params,
                        )
                        resp.raise_for_status()
                        self.state.mark_uploaded(gid)
                        result.new_games_uploaded += 1
                        result.uploaded_ids.append(gid)
                    except Exception as exc:
                        file_had_error = True
                        result.errors.append(f"Upload failed for {gid}: {exc}")

                # Only cache file fingerprint if all newly completed games uploaded cleanly
                if not file_had_error:
                    self._file_fingerprints[path] = fp
        finally:
            if owns_client:
                http.close()

        return result

    def run_forever(self, interval_seconds: float = 5.0) -> None:
        """Poll logs periodically until interrupted."""
        while True:
            res = self.scan_once()
            if res.new_games_uploaded or res.errors:
                print(
                    f"[hs-coach watch] scanned={res.files_scanned} "
                    f"uploaded={res.new_games_uploaded} "
                    f"skipped={res.skipped_already_uploaded} "
                    f"ids={res.uploaded_ids}"
                )
                for err in res.errors:
                    print(f"  ERROR: {err}")
            time.sleep(interval_seconds)
