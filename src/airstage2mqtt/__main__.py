"""AirStage2MQTT command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .config import ConfigurationError, load_config
from .service import BridgeService, healthcheck


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Fujitsu AirStage to MQTT bridge")
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="run the bridge (default)")
    run.add_argument("--config", type=Path, help="path to YAML configuration")
    check = subparsers.add_parser("healthcheck", help="check bridge liveness")
    check.add_argument("--maximum-age", type=float, default=90)
    return parser


async def _run(config_path: Path | None) -> None:
    config = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = BridgeService(config)
    loop = asyncio.get_running_loop()
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected_signal, service.stop)
        except NotImplementedError:  # Windows development environments
            signal.signal(selected_signal, lambda *_: service.stop())
    await service.run()


def main() -> None:
    if sys.platform == "win32":  # aiomqtt requires add_reader support on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = _parser().parse_args()
    if args.command == "healthcheck":
        raise SystemExit(0 if healthcheck(maximum_age=args.maximum_age) else 1)
    try:
        asyncio.run(_run(getattr(args, "config", None)))
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
        logging.error("Configuration error: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
