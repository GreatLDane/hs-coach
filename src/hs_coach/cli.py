"""Command-line interface for HS-Coach."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hs_coach.cards import CardDB, resolve_cards_path, sync_cards_json
from hs_coach.coach import coach_game, dry_run_summary, resolve_coach_config
from hs_coach.parser import parse_power_log
from hs_coach.render import render_html_report
from hs_coach.schemas import CoachReport, GameReplay


def _load_card_db(cards_arg: str | None) -> CardDB:
    cards_path = resolve_cards_path(cards_arg)
    if not cards_path.exists():
        raise FileNotFoundError(
            f"cards.json not found at {cards_path}. Run `hs-coach sync-cards` or pass --cards."
        )
    return CardDB.from_file(cards_path)


def _cmd_sync_cards(args: argparse.Namespace) -> int:
    dest = resolve_cards_path(args.out)
    res = sync_cards_json(dest, force=args.force)
    status = "Updated" if res["updated"] else "Up to date (304 Not Modified)"
    print(f"{status}: {res['path']} ({res['card_count']} cards, etag={res['etag']})")
    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    db = _load_card_db(args.cards)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = parse_power_log(
        args.power_log,
        db,
        standard_only=not args.all_modes,
    )
    print(
        f"Scanned {result.total_games_found} game(s): "
        f"parsed={len(result.replays)}, "
        f"skipped_incomplete={result.skipped_incomplete}, "
        f"skipped_non_standard={result.skipped_non_standard}"
    )
    for w in result.warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    for idx, replay in enumerate(result.replays, 1):
        m = replay.metadata
        ps = replay.pre_check_summary
        r_path = out_dir / f"{m.game_id}.replay.json"
        r_path.write_text(replay.model_dump_json(indent=2), encoding="utf-8")
        html_note = ""
        if args.render_html:
            h_path = out_dir / f"{m.game_id}.replay.html"
            h_path.write_text(render_html_report(replay), encoding="utf-8")
            html_note = f" -> {h_path.name}"
        print(
            f"[{idx}] {m.game_id} | {m.your_class} vs {m.opponent_class} | "
            f"{m.result.upper()} ({m.end_reason}) | {m.total_turns}t | "
            f"coin={'yes' if m.has_coin else 'no'} | "
            f"missed_lethal={ps.missed_lethal_turns or '-'} | "
            f"floated={ps.total_mana_floated} | "
            f"burned={len(ps.your_cards_burned)} disc={len(ps.your_cards_discarded)} | "
            f"{r_path.name}{html_note}"
        )
    return 0


def _load_replays_from_input(
    target: Path, cards_arg: str | None, limit: int | None
) -> list[GameReplay]:
    if target.suffix.lower() == ".json":
        replay = GameReplay.model_validate_json(target.read_text(encoding="utf-8"))
        return [replay]
    db = _load_card_db(cards_arg)
    res = parse_power_log(target, db, standard_only=True)
    replays = res.replays
    if limit is not None and limit > 0:
        replays = replays[-limit:]
    return replays


def _cmd_coach(args: argparse.Namespace) -> int:
    replays = _load_replays_from_input(Path(args.input), args.cards, args.limit)
    if not replays:
        print("No completed Standard Constructed games found.")
        return 1

    if args.dry_run:
        for replay in replays:
            info = dry_run_summary(replay, model=args.model)
            m = replay.metadata
            costs = ", ".join(
                f"{k}=${v:.4f}" if v is not None else f"{k}=n/a"
                for k, v in info["estimated_costs_usd"].items()
            )
            print(
                f"{m.game_id} ({m.your_class} vs {m.opponent_class}, {m.total_turns}t, {m.result}): "
                f"~{info['estimated_input_tokens']} in + {info['assumed_output_tokens']} out tokens "
                f"({info['system_chars'] + info['user_chars']} chars) | {costs}"
            )
        return 0

    cfg = resolve_coach_config(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for replay in replays:
        report = coach_game(replay, cfg)
        c_path = out_dir / f"{replay.metadata.game_id}.coach.json"
        h_path = out_dir / f"{replay.metadata.game_id}.report.html"
        c_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        h_path.write_text(render_html_report(replay, report), encoding="utf-8")
        u = report.usage
        cost_s = (
            f"${u.estimated_cost_usd:.4f}"
            if u and u.estimated_cost_usd is not None
            else "n/a"
        )
        print(
            f"{replay.metadata.game_id} | Grade {report.overall_grade} | "
            f"{report.your_archetype} vs {report.opponent_archetype} | "
            f"tokens={u.input_tokens if u else 0}+{u.output_tokens if u else 0} | "
            f"cost={cost_s} | {h_path}"
        )
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    r_path = Path(args.replay_json)
    replay = GameReplay.model_validate_json(r_path.read_text(encoding="utf-8"))
    report: CoachReport | None = None
    if args.coach:
        report = CoachReport.model_validate_json(
            Path(args.coach).read_text(encoding="utf-8")
        )
    out_path = (
        Path(args.out)
        if args.out
        else r_path.with_suffix("").with_suffix(".report.html")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html_report(replay, report), encoding="utf-8")
    print(f"Rendered HTML report: {out_path}")
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    schemas = {
        "GameReplay.schema.json": GameReplay.model_json_schema(),
        "CoachReport.schema.json": CoachReport.model_json_schema(),
    }
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for fname, doc in schemas.items():
            p = out_dir / fname
            p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            print(f"Wrote {p}")
    else:
        print(json.dumps(schemas, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hs_coach.config import load_app_config
    from hs_coach.storage import GameStore
    from hs_coach.web import create_app

    cfg = load_app_config(mode=args.mode, host=args.host, load_dotenv=True)
    db = _load_card_db(args.cards)
    store = GameStore(args.db)
    app = create_app(db, store, config=cfg)
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        store.close()
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from hs_coach.uploader import PowerLogWatcher

    db = _load_card_db(args.cards)
    watcher = PowerLogWatcher(
        logs_dir=args.logs_dir,
        card_db=db,
        server_url=args.server,
        state_path=args.state,
        token=args.token,
        auto_coach=args.auto_coach,
        model=args.model,
    )
    if args.once:
        res = watcher.scan_once()
        print(
            f"Scanned {res.files_scanned} file(s): "
            f"uploaded={res.new_games_uploaded}, "
            f"skipped_existing={res.skipped_already_uploaded}, "
            f"ids={res.uploaded_ids}"
        )
        for err in res.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1 if res.errors else 0
    try:
        print(f"Watching {args.logs_dir} -> {args.server} every {args.interval}s (Ctrl+C to stop)")
        watcher.run_forever(interval_seconds=args.interval)
    except KeyboardInterrupt:
        print("Stopped watcher.")
    return 0


def _cmd_tray(args: argparse.Namespace) -> int:
    from hs_coach.desktop import run_desktop_app

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hs-coach",
        description="HS-Coach: 100% Post-Game AI Coach for Hearthstone (Standard Constructed).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync-cards", help="Sync HearthstoneJSON cards.json with ETag caching.")
    p_sync.add_argument("--out", default=None, help="Destination path for cards.json (default: data/cards.json).")
    p_sync.add_argument("--force", action="store_true", help="Force re-download ignoring ETag.")
    p_sync.set_defaults(func=_cmd_sync_cards)

    p_parse = sub.add_parser("parse", help="Parse completed games from a Power.log into replay JSON.")
    p_parse.add_argument("power_log", help="Path to Power.log.")
    p_parse.add_argument("--cards", default=None, help="Path to cards.json.")
    p_parse.add_argument("--out-dir", default="local_output", help="Output directory (default: local_output).")
    p_parse.add_argument("--all-modes", action="store_true", help="Include non-Standard / non-Constructed completed games.")
    p_parse.add_argument("--render-html", action="store_true", help="Also write replay-only HTML previews.")
    p_parse.set_defaults(func=_cmd_parse)

    p_coach = sub.add_parser("coach", help="Generate AI coaching JSON + HTML report from a replay JSON or Power.log.")
    p_coach.add_argument("input", help="Path to .replay.json or Power.log.")
    p_coach.add_argument("--cards", default=None, help="Path to cards.json (when input is a Power.log).")
    p_coach.add_argument("--out-dir", default="local_output", help="Output directory (default: local_output).")
    p_coach.add_argument("--limit", type=int, default=3, help="Max recent games when input is Power.log (default: 3).")
    p_coach.add_argument("--dry-run", action="store_true", help="Print token/cost estimates without calling any LLM API.")
    p_coach.add_argument("--provider", default=None, help="Override HS_COACH_PROVIDER.")
    p_coach.add_argument("--model", default=None, help="Override HS_COACH_MODEL.")
    p_coach.add_argument("--base-url", default=None, help="Override HS_COACH_BASE_URL.")
    p_coach.set_defaults(func=_cmd_coach)

    p_render = sub.add_parser("render", help="Render a replay JSON (+ optional coach JSON) to HTML.")
    p_render.add_argument("replay_json", help="Path to .replay.json.")
    p_render.add_argument("--coach", default=None, help="Optional path to .coach.json.")
    p_render.add_argument("--out", default=None, help="Output HTML file path.")
    p_render.set_defaults(func=_cmd_render)

    p_schema = sub.add_parser("schema", help="Print or export JSON Schemas for GameReplay and CoachReport.")
    p_schema.add_argument("--out-dir", default=None, help="Optional directory to write *.schema.json files.")
    p_schema.set_defaults(func=_cmd_schema)

    p_serve = sub.add_parser("serve", help="Run the HS-Coach drag-and-drop web app.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    p_serve.add_argument("--mode", choices=["local", "hosted"], default=None, help="Deployment mode override (default: HS_COACH_MODE or 'local').")
    p_serve.add_argument("--cards", default=None, help="Path to cards.json.")
    p_serve.add_argument("--db", default="data/hs_coach.db", help="Path to SQLite database (default: data/hs_coach.db).")
    p_serve.set_defaults(func=_cmd_serve)

    p_watch = sub.add_parser("watch", help="Watch Power.log locally and upload anonymized completed replays.")
    p_watch.add_argument("--logs-dir", default=r"D:\Hearthstone\Logs", help="Hearthstone Logs directory or Power.log file.")
    p_watch.add_argument("--server", default="http://127.0.0.1:8000", help="HS-Coach web server URL.")
    p_watch.add_argument("--cards", default=None, help="Path to cards.json.")
    p_watch.add_argument("--state", default="data/uploader_state.json", help="Path to uploaded-game state JSON.")
    p_watch.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds (default: 5.0).")
    p_watch.add_argument("--once", action="store_true", help="Scan once and exit.")
    p_watch.add_argument("--auto-coach", action="store_true", help="Automatically generate AI coaching on upload.")
    p_watch.add_argument("--model", default=None, help="Optional model override when --auto-coach is set.")
    p_watch.add_argument("--token", default=None, help="Optional Bearer token (or HS_COACH_UPLOAD_TOKEN env var).")
    p_watch.set_defaults(func=_cmd_watch)

    p_tray = sub.add_parser("tray", help="Launch the post-game desktop uploader tray/status window.")
    p_tray.add_argument("--config", default="data/desktop_config.json", help="Desktop config JSON path.")
    p_tray.add_argument("--logs-dir", default=None, help="Override Hearthstone Logs directory.")
    p_tray.add_argument("--server", default=None, help="Override HS-Coach web server URL.")
    p_tray.add_argument("--cards", default=None, help="Override cards.json path.")
    p_tray.add_argument("--token", default=None, help="Override personal upload Bearer token.")
    p_tray.add_argument("--interval", type=float, default=None, help="Override poll interval in seconds.")
    p_tray.add_argument("--auto-coach", action="store_true", help="Automatically generate AI coaching on upload.")
    p_tray.add_argument("--model", default=None, help="Override model when --auto-coach is set.")
    p_tray.add_argument("--window", action="store_true", help="Force tkinter status window instead of system tray.")
    p_tray.set_defaults(func=_cmd_tray)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
