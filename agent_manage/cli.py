from __future__ import annotations

import argparse
import json
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
    AddAgentRequest,
    AddAgentsRequest,
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    SetModelRequest,
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

    add_agents = subparsers.add_parser("add-agents")
    add_agents.add_argument("--agents", required=True)
    add_agents.add_argument("--workspace-root", default="~/data")

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
    set_model.add_argument("--model", required=True)

    current_model = subparsers.add_parser("current-model")

    models = subparsers.add_parser("models")

    update_model = subparsers.add_parser("update-model")

    current_gateway_token = subparsers.add_parser("current-gateway-token")

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
        if args.command == "add-agents":
            result = client.add_agents(
                AddAgentsRequest(
                    agents=_parse_add_agents(args.agents),
                    workspace_root=args.workspace_root,
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
        if args.command == "models":
            result = client.get_supported_models()
            print_json(build_success_response(result))
            return 0
        if args.command == "update-model":
            result = client.update_model_catalog()
            print_json(build_success_response(result))
            return 0
        if args.command == "current-gateway-token":
            result = client.get_current_gateway_token()
            print_json(build_success_response(result))
            return 0

        parser.print_help(sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception as exc:
        print_json(build_error_response(exc))
        return 1


def _parse_add_agents(raw: str) -> List[AddAgentRequest]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--agents must be a JSON array") from exc

    if not isinstance(payload, list):
        raise ValueError("--agents must be a JSON array")
    if not payload:
        raise ValueError("--agents must contain at least one item")

    agents: List[AddAgentRequest] = []
    for index, item in enumerate(payload):
        if isinstance(item, str):
            agent_name = item.strip()
            if not agent_name:
                raise ValueError(f"agents[{index}] must not be empty")
            agents.append(AddAgentRequest(agent_name=agent_name))
            continue

        if not isinstance(item, dict):
            raise ValueError(f"agents[{index}] must be a string or object")

        agent_name = item.get("agent_name")
        template_name = item.get("template_name")
        workspace = item.get("workspace")
        model = item.get("model")

        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError(f"agents[{index}].agent_name is required")
        if template_name is not None and not isinstance(template_name, str):
            raise ValueError(f"agents[{index}].template_name must be a string")
        if workspace is not None and not isinstance(workspace, str):
            raise ValueError(f"agents[{index}].workspace must be a string")
        if model is not None and not isinstance(model, str):
            raise ValueError(f"agents[{index}].model must be a string")

        agents.append(
            AddAgentRequest(
                agent_name=agent_name.strip(),
                template_name=template_name.strip() if isinstance(template_name, str) else None,
                workspace=workspace.strip() if isinstance(workspace, str) else None,
                model=model.strip() if isinstance(model, str) else None,
            )
        )
    return agents


if __name__ == "__main__":
    raise SystemExit(main())
