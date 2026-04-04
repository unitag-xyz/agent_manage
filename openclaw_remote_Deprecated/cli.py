from __future__ import annotations

"""Deprecated v1 CLI. Only kept for history/reference."""

import argparse
import sys
from typing import List, Optional

from .local import LocalRunner
from .models import (
    CreateAgentRequest,
    DeleteAgentRequest,
    TelegramAccountConfig,
)
from .orchestrator import OpenClawManager
from .response import (
    JsonArgumentParser,
    build_error_response,
    build_success_response,
    print_json,
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = JsonArgumentParser(prog="openclaw-remote")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--project-dir")
    parser.add_argument("--config-path")
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--tg-id", required=True)
    create.add_argument("--agent-name")
    create.add_argument("--model")
    create.add_argument("--agent-dir")
    create.add_argument("--tg-bot")
    create.add_argument("--tg-bot-token")
    create.add_argument("--no-rollback", action="store_true")

    delete = subparsers.add_parser("delete")
    delete.add_argument("--tg-id", required=True)
    delete.add_argument("--agent-name")
    delete.add_argument("--no-force", action="store_true")
    delete.add_argument("--purge-workspace", action="store_true")

    try:
        args = parser.parse_args(argv)
        client = OpenClawManager(
            LocalRunner(
                openclaw_bin=args.openclaw_bin,
                project_dir=args.project_dir,
                dry_run=args.dry_run,
            ),
            config_path=args.config_path,
        )

        if args.command == "create":
            result = client.create(
                CreateAgentRequest(
                    tg_id=args.tg_id,
                    agent_name=args.agent_name,
                    model=args.model,
                    agent_dir=args.agent_dir,
                    rollback_on_fail=not args.no_rollback,
                    telegram=telegram_from_args(args),
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "delete":
            result = client.delete(
                DeleteAgentRequest(
                    tg_id=args.tg_id,
                    agent_name=args.agent_name,
                    force=not args.no_force,
                    purge_workspace=args.purge_workspace,
                )
            )
            print_json(build_success_response(result))
            return 0

        parser.print_help(sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception as exc:
        print_json(build_error_response(exc))
        return 1


def telegram_from_args(args: argparse.Namespace) -> Optional[TelegramAccountConfig]:
    if not args.tg_bot:
        return None
    return TelegramAccountConfig(
        account_id=args.tg_bot,
        bot_token=getattr(args, "tg_bot_token", None),
    )


if __name__ == "__main__":
    raise SystemExit(main())
