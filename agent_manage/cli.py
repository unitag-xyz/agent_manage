from __future__ import annotations

import time

_CLI_IMPORT_STARTED = time.perf_counter()

import json
import os
import sys
from typing import List, Optional

from . import __version__
from .adapters.cli_binding import cli_driver
from .adapters.execution import LocalExecution, LocalOptions
from .segment_timing import CURRENT, ReadTiming
from .models import (
    AddAgentRequest,
    AddAgentsRequest,
    AddSkillRequest,
    AddFeishuBotRequest,
    AddTelegramBotRequest,
    AddWeixinBotRequest,
    CreateInstanceRequest,
    DeleteFeishuBotRequest,
    DeleteTelegramBotRequest,
    DeleteWeixinBotRequest,
    SetModelRequest,
    RefreshAgentRequest,
)
from .settings import DEFAULT_AI_SHOP, DEFAULT_IMAGE_QUALITY, DEFAULT_MODEL_ENV, IMAGE_QUALITY_CHOICES, MODEL_GATEWAYS
from .core.errors import APIError
from .response import (
    CliArgumentError,
    JsonArgumentParser,
    TYPE_CODE_ACCEPTED,
    build_error_response,
    build_success_response,
    print_json,
    redact_sensitive_values,
)

MODEL_ENV_CHOICES = sorted(MODEL_GATEWAYS)
SIMPLE_COMMAND_HANDLERS = {
    "check-server-status": "check_server_status",
    "tg-bot-status": "get_tg_bot_status",
    "feishu-bot-status": "get_feishu_bot_status",
    "weixin-bot-status": "get_weixin_bot_status",
    "agents-list": "list_agents",
    "agents-start": "start_agent_services",
    "current-model": "get_current_model",
    "models": "get_supported_models",
    "update-model": "update_model_catalog",
    "current-gateway-token": "get_current_gateway_token",
}

CLI_OPERATIONS = {
    **SIMPLE_COMMAND_HANDLERS,
    "create-instance": "create_instance", "configure-instance": "configure_instance",
    "activate-instance": "activate_instance", "agent-install": "install_agent_service",
    "add-agent": "add_agent", "add-agents": "add_agents", "refresh-agent": "refresh_agent",
    "add-skill": "add_skill", "set-model": "set_model", "codex-login": "codex_login",
    "codex-status": "codex_status", "codex-logout": "codex_logout",
    "sessions-segments-history": "segment_history", "sessions-segments-message-get": "segment_message_get",
    "sessions-segments-media-get": "segment_media_get",
    "add-tg-bot": "add_tg_bot", "delete-tg-bot": "delete_tg_bot",
    "add-feishu-bot": "add_feishu_bot", "delete-feishu-bot": "delete_feishu_bot",
    "add-weixin-bot": "add_weixin_bot", "delete-weixin-bot": "delete_weixin_bot",
}


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"sessions-segments-history", "sessions-segments-message-get", "sessions-segments-media-get"}
    if not commands.intersection(args):
        return _main(args)
    timing = ReadTiming()
    timing.import_ms = _CLI_IMPORT_MS
    token = CURRENT.set(timing)
    try:
        return _main(args)
    finally:
        CURRENT.reset(token)


def build_parser():
    parser = JsonArgumentParser(prog="agent-manage")
    parser.add_argument("--openclaw-bin")
    parser.add_argument("--project-dir")
    parser.add_argument("--template-root")
    parser.add_argument("--config-path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_instance = subparsers.add_parser("create-instance")
    _add_instance_arguments(create_instance)

    configure_instance = subparsers.add_parser("configure-instance")
    _add_instance_arguments(configure_instance)

    activate_instance = subparsers.add_parser("activate-instance")
    _add_instance_arguments(activate_instance, activation=True)

    subparsers.add_parser("agents-start")
    install_agent = subparsers.add_parser("agent-install")
    install_agent.add_argument("--agent", required=True)

    add_agent = subparsers.add_parser("add-agent")
    add_agent.add_argument("--template-name", required=True)
    add_agent.add_argument("--agent-name")
    add_agent.add_argument("--workspace-root")
    add_agent.add_argument("--model")
    add_agent.add_argument("--base-url")
    add_agent.add_argument("--template-zip-url")
    add_agent.add_argument("--template-zip-sha256")

    add_agents = subparsers.add_parser("add-agents")
    add_agents.add_argument("--agents", required=True)
    add_agents.add_argument("--workspace-root", default="~/data")
    add_agents.add_argument("--base-url")

    add_skill = subparsers.add_parser("add-skill")
    skill_scope = add_skill.add_mutually_exclusive_group(required=True)
    skill_scope.add_argument("--agent", help="target agent id")
    skill_scope.add_argument("--common", action="store_true", help="install for this OpenClaw environment")
    skill_source = add_skill.add_mutually_exclusive_group(required=True)
    skill_source.add_argument("--skill-dir", help="skill directory containing SKILL.md")
    skill_source.add_argument("--skill-zip", help="ZIP containing one skill")
    add_skill.add_argument("--skill-name", help="destination directory name (defaults to source name)")
    add_skill.add_argument("--replace", action="store_true", help="replace an existing skill directory")

    refresh = subparsers.add_parser("refresh-agent")
    refresh.add_argument("--agent", required=True)
    refresh_source = refresh.add_mutually_exclusive_group()
    refresh_source.add_argument("--template-dir")
    refresh_source.add_argument("--agent-zip")
    refresh_source.add_argument("--template-zip-url")
    refresh.add_argument("--template-version")
    refresh.add_argument("--template-zip-sha256")
    refresh.add_argument("--template-name")
    refresh_scope = refresh.add_mutually_exclusive_group()
    refresh_scope.add_argument("--models-only", action="store_true")
    refresh_scope.add_argument("--template-only", action="store_true")
    refresh.add_argument("--replace-modified", action="store_true", help="back up and replace conflicting template files; memory is always preserved")
    refresh.add_argument("--restart", action="store_true", help="restart the environment gateway and require a successful RPC health check")

    subparsers.add_parser("codex-login")
    subparsers.add_parser("codex-logout")
    subparsers.add_parser("codex-status")

    for command in ("sessions-segments-history", "sessions-segments-message-get", "sessions-segments-media-get"):
        segment = subparsers.add_parser(command)
        segment.add_argument("--session-key", "--session-k", dest="session_key", required=True,
                             help="logical session key; --session-k is a command-channel compatibility alias")
        segment.add_argument("--session-id", required=True)
        segment.add_argument("--cursor")
        if command == "sessions-segments-history":
            segment.add_argument("--omit-thinking", nargs="?", const="true", default=None, type=str.lower,
                                 choices=["true", "false"], help="omit thinking content (optional true/false); cursors inherit when omitted")
            segment.add_argument("--edge", choices=["start", "end"])
            segment.add_argument("--limit", type=int, default=50)
            segment.add_argument("--view", choices=["compact", "full"],
                                 help="default: compact tool commands/results; retain text and thinking")
        elif command == "sessions-segments-message-get":
            segment.add_argument("--message-id", required=True)
            segment.add_argument("--ref", help="snapshot reference returned by history")
        else:
            segment.add_argument("--ref", help="protected media reference returned by history; optional with --cursor")

    add_tg_bot = subparsers.add_parser("add-tg-bot")
    add_tg_bot.add_argument("--agent", required=True)
    add_tg_bot_token = add_tg_bot.add_mutually_exclusive_group(required=True)
    add_tg_bot_token.add_argument("--tg-token")
    add_tg_bot_token.add_argument("--tg-token-stdin", action="store_true")
    add_tg_bot.add_argument("--bot-name")

    add_feishu_bot = subparsers.add_parser("add-feishu-bot")
    add_feishu_bot.add_argument("--agent", required=True)
    add_feishu_bot.add_argument("--domain", choices=["feishu", "lark"], default="feishu")
    add_feishu_bot.add_argument("--account-id", default="main")
    add_feishu_bot.add_argument("--app-id", required=True)
    add_feishu_bot_secret = add_feishu_bot.add_mutually_exclusive_group(required=True)
    add_feishu_bot_secret.add_argument("--app-secret")
    add_feishu_bot_secret.add_argument("--app-secret-stdin", action="store_true")
    add_feishu_bot.add_argument("--bot-name")
    add_feishu_bot.add_argument("--dm-policy", default="open")
    add_feishu_bot.add_argument("--allow-from", action="append")
    add_feishu_bot.add_argument("--bind-lark-cli", action="store_true")
    add_feishu_bot.add_argument(
        "--lark-cli-identity",
        choices=["bot-only", "user-default"],
        default="bot-only",
    )

    add_weixin_bot = subparsers.add_parser("add-weixin-bot")
    add_weixin_bot.add_argument("--agent", required=True)
    add_weixin_bot.add_argument("--ilink-bot-id", required=True)
    add_weixin_bot_token = add_weixin_bot.add_mutually_exclusive_group(required=True)
    add_weixin_bot_token.add_argument("--bot-token")
    add_weixin_bot_token.add_argument("--bot-token-stdin", action="store_true")
    add_weixin_bot.add_argument("--baseurl")
    add_weixin_bot.add_argument("--ilink-user-id")
    add_weixin_bot.add_argument("--bot-name")
    add_weixin_bot.add_argument("--route-tag")
    add_weixin_bot.add_argument("--cdn-base-url")

    subparsers.add_parser("check-server-status")

    subparsers.add_parser("tg-bot-status")

    subparsers.add_parser("feishu-bot-status")

    subparsers.add_parser("weixin-bot-status")

    delete_tg_bot = subparsers.add_parser("delete-tg-bot")
    delete_tg_bot.add_argument("--bot-name", required=True)

    delete_feishu_bot = subparsers.add_parser("delete-feishu-bot")
    delete_feishu_bot.add_argument("--account-id", required=True)

    delete_weixin_bot = subparsers.add_parser("delete-weixin-bot")
    delete_weixin_bot.add_argument("--ilink-bot-id", required=True)

    subparsers.add_parser("agents-list")

    set_model = subparsers.add_parser("set-model")
    set_model.add_argument("--model", required=True)

    subparsers.add_parser("current-model")

    subparsers.add_parser("models")

    subparsers.add_parser("update-model")

    subparsers.add_parser("current-gateway-token")

    return parser


def _main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    sensitive_values: List[str] = []
    try:
        args = parser.parse_args(argv)
        _reject_container_path_overrides(raw_argv)
        driver = cli_driver(args)
        if driver.open_local is None:
            raise APIError("UNSUPPORTED", "Runtime engine does not support local execution")
        client = driver.open_local(LocalOptions(args.command, args.openclaw_bin, args.project_dir, args.dry_run,
                                                args.template_root, args.config_path))
        execute = client.operation(CLI_OPERATIONS[args.command])
        timing = CURRENT.get()
        if timing is not None:
            timing.phases["cliSetupMs"] = (time.perf_counter() - timing.started) * 1000
            timing.calls["cliSetupMs"] = 1

        if args.command == "agent-install":
            print_json(_success_response(execute(args.agent), client))
            return 0

        if args.command in SIMPLE_COMMAND_HANDLERS:
            result = execute()
            if args.command == "agents-start" and not result["ok"]:
                print_json(build_error_response(RuntimeError(json.dumps({
                    "error": "One or more agent start.sh scripts failed",
                    "details": {"returncode": 1, "agents": result["agents"]},
                }))))
                return 1
            print_json(_success_response(result, client))
            return 0

        if args.command in ("create-instance", "configure-instance", "activate-instance"):
            request = CreateInstanceRequest(
                template_name=getattr(args, "template_name", ""),
                model_key=_secret_argument(
                    args.model_key,
                    args.model_key_stdin,
                    sensitive_values,
                    client.register_secret,
                ),
                model_env=args.model_env,
                ai_shop=args.ai_shop,
                model=getattr(args, "model", None),
                image_quality=args.image_quality,
                base_url=args.base_url,
                workspace_root=getattr(args, "workspace_root", None)
                or client.default_workspace(local=getattr(args, "local", False)),
                rollback_on_fail=not getattr(args, "no_rollback", False),
                agent_zip=getattr(args, "agent_zip", None),
                local=getattr(args, "local", False),
            )
            if args.command == "activate-instance":
                sensitive_values.append(request.model_key.strip())
                result = execute(request)
                activation_required = bool(result.pop("activationRequired", False))
                response = _success_response(result, client)
                response["activationRequired"] = activation_required
                print_json(response)
                return 0
            result = execute(request)
            print_json(_success_response(result, client))
            return 0
        if args.command == "add-agent":
            result = execute(
                template_name=args.template_name,
                agent_name=args.agent_name,
                workspace_root=args.workspace_root
                or client.default_workspace(),
                model=args.model,
                base_url=args.base_url,
                template_zip_url=args.template_zip_url,
                template_zip_sha256=args.template_zip_sha256,
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "add-agents":
            result = execute(
                AddAgentsRequest(
                    agents=_parse_add_agents(args.agents),
                    workspace_root=args.workspace_root
                    or client.default_workspace(),
                    base_url=args.base_url,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "codex-login":
            result = execute()
            print_json(build_success_response(result, type_code=TYPE_CODE_ACCEPTED if result.get("status") in ("starting", "pending") else 1))
            return 0
        if args.command == "codex-logout":
            print_json(build_success_response(execute()))
            return 0
        if args.command == "codex-status":
            print_json(build_success_response(execute()))
            return 0
        if args.command == "sessions-segments-history":
            result = execute(args.session_key, args.session_id, edge=args.edge,
                                            limit=args.limit, cursor=args.cursor, view=args.view,
                                            **({"omit_thinking": args.omit_thinking == "true"} if args.omit_thinking is not None else {}))
            print_json(build_success_response(result))
            return 0
        if args.command == "sessions-segments-message-get":
            result = execute(args.session_key, args.session_id, args.message_id,
                                                ref=args.ref, cursor=args.cursor)
            print_json(build_success_response(result))
            return 0
        if args.command == "sessions-segments-media-get":
            result = execute(args.session_key, args.session_id, args.ref, cursor=args.cursor)
            print_json(build_success_response(result))
            return 0
        if args.command == "refresh-agent":
            result = execute(RefreshAgentRequest(
                agent_name=args.agent,
                template_dir=args.template_dir,
                agent_zip=args.agent_zip,
                template_name=args.template_name,
                template_zip_url=args.template_zip_url,
                template_zip_sha256=args.template_zip_sha256,
                template_version=args.template_version,
                models_only=args.models_only,
                template_only=args.template_only,
                replace_modified=args.replace_modified,
                restart=args.restart,
            ))
            print_json(build_success_response(result))
            return 0
        if args.command == "add-skill":
            result = execute(
                AddSkillRequest(
                    agent_name=args.agent,
                    common=args.common,
                    skill_dir=args.skill_dir,
                    skill_zip=args.skill_zip,
                    skill_name=args.skill_name,
                    replace=args.replace,
                )
            )
            print_json(build_success_response(result))
            return 0
        if args.command == "add-tg-bot":
            result = execute(
                AddTelegramBotRequest(
                    agent_name=args.agent,
                    bot_token=_secret_argument(
                        args.tg_token,
                        args.tg_token_stdin,
                        sensitive_values,
                        client.register_secret,
                    ),
                    bot_name=args.bot_name,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "add-feishu-bot":
            result = execute(
                AddFeishuBotRequest(
                    agent_name=args.agent,
                    domain=args.domain,
                    account_id=args.account_id,
                    app_id=args.app_id,
                    app_secret=_secret_argument(
                        args.app_secret,
                        args.app_secret_stdin,
                        sensitive_values,
                        client.register_secret,
                    ),
                    bot_name=args.bot_name,
                    dm_policy=args.dm_policy,
                    allow_from=args.allow_from,
                    bind_lark_cli=args.bind_lark_cli,
                    lark_cli_identity=args.lark_cli_identity,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "add-weixin-bot":
            result = execute(
                AddWeixinBotRequest(
                    agent_name=args.agent,
                    ilink_bot_id=args.ilink_bot_id,
                    bot_token=_secret_argument(
                        args.bot_token,
                        args.bot_token_stdin,
                        sensitive_values,
                        client.register_secret,
                    ),
                    baseurl=args.baseurl,
                    ilink_user_id=args.ilink_user_id,
                    bot_name=args.bot_name,
                    route_tag=args.route_tag,
                    cdn_base_url=args.cdn_base_url,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "delete-tg-bot":
            result = execute(
                DeleteTelegramBotRequest(
                    bot_name=args.bot_name,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "delete-feishu-bot":
            result = execute(
                DeleteFeishuBotRequest(
                    account_id=args.account_id,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "delete-weixin-bot":
            result = execute(
                DeleteWeixinBotRequest(
                    ilink_bot_id=args.ilink_bot_id,
                )
            )
            print_json(_success_response(result, client))
            return 0
        if args.command == "set-model":
            result = execute(
                SetModelRequest(
                    model_ref=args.model,
                )
            )
            print_json(_success_response(result, client))
            return 0

        parser.print_help(sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception as exc:
        print_json(redact_sensitive_values(build_error_response(exc), sensitive_values))
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
        template_zip_url = item.get("template_zip_url")
        template_zip_sha256 = item.get("template_zip_sha256")

        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError(f"agents[{index}].agent_name is required")
        if template_name is not None and not isinstance(template_name, str):
            raise ValueError(f"agents[{index}].template_name must be a string")
        if workspace is not None and not isinstance(workspace, str):
            raise ValueError(f"agents[{index}].workspace must be a string")
        if model is not None and not isinstance(model, str):
            raise ValueError(f"agents[{index}].model must be a string")
        if template_zip_url is not None and not isinstance(template_zip_url, str):
            raise ValueError(f"agents[{index}].template_zip_url must be a string")
        if template_zip_sha256 is not None and not isinstance(template_zip_sha256, str):
            raise ValueError(f"agents[{index}].template_zip_sha256 must be a string")

        agents.append(
            AddAgentRequest(
                agent_name=agent_name.strip(),
                template_name=template_name.strip() if isinstance(template_name, str) else None,
                workspace=workspace.strip() if isinstance(workspace, str) else None,
                model=model.strip() if isinstance(model, str) else None,
                template_zip_url=(
                    template_zip_url.strip()
                    if isinstance(template_zip_url, str)
                    else None
                ),
                template_zip_sha256=(
                    template_zip_sha256.strip()
                    if isinstance(template_zip_sha256, str)
                    else None
                ),
            )
        )
    return agents


def _add_instance_arguments(parser, *, activation: bool = False) -> None:
    if not activation:
        parser.add_argument("--template-name")
        parser.add_argument("--agent-zip")
        parser.add_argument("--local", action="store_true")
    model_key = parser.add_mutually_exclusive_group(required=True)
    if not activation:
        model_key.add_argument("--model-key")
    else:
        parser.set_defaults(model_key=None)
    model_key.add_argument("--model-key-stdin", action="store_true")
    parser.add_argument(
        "--model-env",
        choices=MODEL_ENV_CHOICES,
        default=DEFAULT_MODEL_ENV,
    )
    parser.add_argument(
        "--ai-shop",
        default=DEFAULT_AI_SHOP,
        help=f"model shop path (default: {DEFAULT_AI_SHOP})",
    )
    if not activation:
        parser.add_argument("--model")
    parser.add_argument(
        "--image-quality",
        choices=IMAGE_QUALITY_CHOICES,
        default=DEFAULT_IMAGE_QUALITY,
    )
    parser.add_argument("--base-url")
    if not activation:
        parser.add_argument("--workspace-root")
        parser.add_argument("--no-rollback", action="store_true")


def _read_secret_from_stdin() -> str:
    value = sys.stdin.read()
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\r") or value.endswith("\n"):
        return value[:-1]
    return value


def _secret_argument(
    plaintext: Optional[str],
    from_stdin: bool,
    sensitive_values: List[str],
    register_redaction,
) -> str:
    value = _read_secret_from_stdin() if from_stdin else (plaintext or "")
    if value:
        sensitive_values.append(value)
        register_redaction(value)
        return value
    raise ValueError("Secret value must not be empty")


def _success_response(result: object, client: LocalExecution) -> dict:
    result_restart_required = (
        isinstance(result, dict) and bool(result.get("restart_required", False))
    )
    return build_success_response(
        result,
        restart_required=bool(client.restart_required or result_restart_required),
    )


def _reject_container_path_overrides(argv: List[str]) -> None:
    if os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME") != "container":
        return
    forbidden = ("--openclaw-bin", "--project-dir", "--template-root", "--config-path")
    for option in forbidden:
        if option in argv or any(argument.startswith(f"{option}=") for argument in argv):
            raise CliArgumentError(f"{option} is not allowed in container runtime")


_CLI_IMPORT_MS = (time.perf_counter() - _CLI_IMPORT_STARTED) * 1000


if __name__ == "__main__":
    raise SystemExit(main())
