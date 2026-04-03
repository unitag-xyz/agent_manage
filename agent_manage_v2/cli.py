from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from openclaw_remote.local import LocalRunner

from .models import AddTelegramBotRequest, CreateInstanceRequest
from .orchestrator import InstanceManagerV2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-manage-v2")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--project-dir")
    parser.add_argument("--template-root")
    parser.add_argument("--config-path")
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_instance = subparsers.add_parser("create-instance")
    create_instance.add_argument("--template-name", required=True)
    create_instance.add_argument("--model")
    create_instance.add_argument("--workspace-root", default="~/data")
    create_instance.add_argument("--no-rollback", action="store_true")

    add_tg_bot = subparsers.add_parser("add-tg-bot")
    add_tg_bot.add_argument("--agent", required=True)
    add_tg_bot.add_argument("--tg-token", required=True)
    add_tg_bot.add_argument("--bot-name")

    args = parser.parse_args(argv)
    client = InstanceManagerV2(
        LocalRunner(
            openclaw_bin=args.openclaw_bin,
            project_dir=args.project_dir,
            dry_run=args.dry_run,
        ),
        template_root=args.template_root,
        config_path=args.config_path,
    )

    if args.command == "create-instance":
        print_json(
            client.create_instance(
                CreateInstanceRequest(
                    template_name=args.template_name,
                    model=args.model,
                    workspace_root=args.workspace_root,
                    rollback_on_fail=not args.no_rollback,
                )
            )
        )
        return 0
    if args.command == "add-tg-bot":
        print_json(
            client.add_tg_bot(
                AddTelegramBotRequest(
                    agent_name=args.agent,
                    bot_token=args.tg_token,
                    bot_name=args.bot_name,
                )
            )
        )
        return 0

    parser.print_help(sys.stderr)
    return 1


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
