from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .local import LocalRunner
from .response import (
    JsonArgumentParser,
    build_error_response,
    build_success_response,
    print_json,
)

from .models import (
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    SetModelRequest,
    SUPPORTED_MODEL_REFS,
)
from .orchestrator import InstanceManagerV2


def main(argv: Optional[List[str]] = None) -> int:
    parser = JsonArgumentParser(prog="agent-manage")
    parser.add_argument("--openclaw-bin", default="openclaw")
    parser.add_argument("--project-dir")
    parser.add_argument("--template-root")
    parser.add_argument("--config-path")
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_instance = subparsers.add_parser("create-instance")
    create_instance.add_argument("--template-name", required=True)
    create_instance.add_argument("--model-key", required=True)
    create_instance.add_argument("--model")
    create_instance.add_argument("--workspace-root", default="~/data")
    create_instance.add_argument("--no-rollback", action="store_true")

    add_tg_bot = subparsers.add_parser("add-tg-bot")
    add_tg_bot.add_argument("--agent", required=True)
    add_tg_bot.add_argument("--tg-token", required=True)
    add_tg_bot.add_argument("--bot-name")

    add_weixin_bot = subparsers.add_parser("add-weixin-bot")
    add_weixin_bot.add_argument("--agent", required=True)
    add_weixin_bot.add_argument("--ilink-bot-id", required=True)
    add_weixin_bot.add_argument("--bot-token", required=True)
    add_weixin_bot.add_argument("--baseurl")
    add_weixin_bot.add_argument("--ilink-user-id")
    add_weixin_bot.add_argument("--bot-name")
    add_weixin_bot.add_argument("--route-tag")
    add_weixin_bot.add_argument("--cdn-base-url")

    check_server_status = subparsers.add_parser("check-server-status")

    tg_bot_status = subparsers.add_parser("tg-bot-status")

    weixin_bot_status = subparsers.add_parser("weixin-bot-status")

    delete_tg_bot = subparsers.add_parser("delete-tg-bot")
    delete_tg_bot.add_argument("--bot-name", required=True)

    delete_weixin_bot = subparsers.add_parser("delete-weixin-bot")
    delete_weixin_bot.add_argument("--ilink-bot-id", required=True)

    agents_list = subparsers.add_parser("agents-list")

    set_model = subparsers.add_parser("set-model")
    set_model.add_argument("--model", required=True, choices=sorted(SUPPORTED_MODEL_REFS))

    current_model = subparsers.add_parser("current-model")

    try:
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
            result = client.create_instance(
                CreateInstanceRequest(
                    template_name=args.template_name,
                    model_key=args.model_key,
                    model=args.model,
                    workspace_root=args.workspace_root,
                    rollback_on_fail=not args.no_rollback,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "add-tg-bot":
            result = client.add_tg_bot(
                AddTelegramBotRequest(
                    agent_name=args.agent,
                    bot_token=args.tg_token,
                    bot_name=args.bot_name,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "add-weixin-bot":
            result = client.add_weixin_bot(
                AddWeixinBotRequest(
                    agent_name=args.agent,
                    ilink_bot_id=args.ilink_bot_id,
                    bot_token=args.bot_token,
                    baseurl=args.baseurl,
                    ilink_user_id=args.ilink_user_id,
                    bot_name=args.bot_name,
                    route_tag=args.route_tag,
                    cdn_base_url=args.cdn_base_url,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "check-server-status":
            result = client.check_server_status()
            print_json(build_success_response(result))
            return 0
        if args.command == "tg-bot-status":
            result = client.get_tg_bot_status()
            print_json(build_success_response(result))
            return 0
        if args.command == "weixin-bot-status":
            result = client.get_weixin_bot_status()
            print_json(build_success_response(result))
            return 0
        if args.command == "delete-tg-bot":
            result = client.delete_tg_bot(
                DeleteTelegramBotRequest(
                    bot_name=args.bot_name,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "delete-weixin-bot":
            result = client.delete_weixin_bot(
                DeleteWeixinBotRequest(
                    ilink_bot_id=args.ilink_bot_id,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "agents-list":
            result = client.list_agents()
            print_json(build_success_response(result))
            return 0
        if args.command == "set-model":
            result = client.set_model(
                SetModelRequest(
                    model_ref=args.model,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "current-model":
            result = client.get_current_model()
            print_json(build_success_response(result))
            return 0

        parser.print_help(sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception as exc:
        print_json(build_error_response(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
