"""Standalone GUI entrypoint for the packaged HS-Coach Desktop Uploader."""

from __future__ import annotations

import argparse
import sys

from hs_coach import __version__
from hs_coach.desktop import run_desktop_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="HS-Coach-Uploader",
        description="HS-Coach 100% Post-Game Hearthstone Desktop Uploader.",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    parser.add_argument("--config", default=None, help="Desktop config JSON path.")
    parser.add_argument("--logs-dir", default=None, help="Hearthstone Logs directory.")
    parser.add_argument("--server", default=None, help="HS-Coach web server URL.")
    parser.add_argument("--cards", default=None, help="Path to cards.json.")
    parser.add_argument("--token", default=None, help="Personal upload Bearer token.")
    parser.add_argument("--interval", type=float, default=None, help="Poll interval seconds.")
    parser.add_argument("--auto-coach", action="store_true", help="Auto-coach uploaded games.")
    parser.add_argument("--model", default=None, help="Model override when --auto-coach is set.")
    parser.add_argument("--window", action="store_true", help="Force tkinter status window.")
    args = parser.parse_args(argv)

    if args.version:
        print(f"HS-Coach-Uploader {__version__}")
        return 0

    run_desktop_app(
        config_path=args.config,
        logs_dir=args.logs_dir,
        server_url=args.server,
        cards_path=args.cards,
        upload_token=args.token,
        poll_interval=args.interval,
        auto_coach=args.auto_coach if args.auto_coach else None,
        model=args.model,
        force_window=args.window,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
