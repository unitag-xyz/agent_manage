"""Account routing operations shared by the channel-specific managers."""

from __future__ import annotations


def remove_account_bindings(config: dict, channel: str, account_id: str) -> int:
    bindings = list(config.get("bindings", []))
    retained = [item for item in bindings
                if not (item.get("match", {}).get("channel") == channel
                        and item.get("match", {}).get("accountId") == account_id)]
    config["bindings"] = retained
    return len(bindings) - len(retained)


def replace_account_binding(config: dict, channel: str, account_id: str, agent_id: str) -> int:
    removed = remove_account_bindings(config, channel, account_id)
    config["bindings"].append({"agentId": agent_id,
                               "match": {"channel": channel, "accountId": account_id}})
    return removed


def count_channel_bindings(config: dict, channel: str) -> dict[str, int]:
    counts = {}
    for item in config.get("bindings", []):
        match = item.get("match", {})
        if match.get("channel") == channel and match.get("accountId"):
            account_id = match["accountId"]
            counts[account_id] = counts.get(account_id, 0) + 1
    return counts
