"""OpenClaw content -> v1 display blocks. Opaque reasoning signatures never leave the adapter."""
import hashlib
from ....core.store import canonical


def message_dto(raw, *, thread_id, segment_id, projection, register_resource):
    from ....segment_reader import millis
    native_id = raw.get("id") or raw.get("__openclaw", {}).get("id")
    identifier = str(native_id or hashlib.sha256(canonical(raw).encode()).hexdigest())
    blocks = raw.get("content") or []
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    content = []
    for index, block in enumerate(blocks):
        if isinstance(block, str):
            block = {"type": "text", "text": block}
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        common = {"blockId": f"{identifier}:{index}"}
        if kind in ("text", "thinking", "reasoning"):
            text = block.get("text") if kind == "text" else block.get("thinking", block.get("text", ""))
            if text and (kind == "text" or not projection["omitThinking"]):
                content.append({**common, "type": "text" if kind == "text" else "reasoning", "text": text})
        elif kind in ("toolCall", "tool_call", "tool_use", "functionCall"):
            from ....segment_reader import preview_arguments
            content.append({**common, "type": "tool_call", "toolCallId": block.get("id"),
                "name": block.get("name"), "summary": block.get("summary") or preview_arguments(block.get("arguments", block.get("input"))),
                "status": block.get("status", "unknown")})
        elif kind in ("image", "image_url", "audio", "video"):
            content.append({**common, "type": "notice", "code": "MEDIA_REFERENCE", "text": "See message resources"})
        else:
            content.append({**common, "type": "notice", "code": "NATIVE_BLOCK", "text": str(kind or "unknown")})
    if raw.get("thinking") and not projection["omitThinking"]:
        content.append({"blockId": identifier + ":thinking", "type": "reasoning", "text": raw["thinking"]})
    if raw.get("role") in ("toolResult", "tool", "function"):
        content = [{"blockId": identifier + ":result", "type": "tool_result", "toolCallId": raw.get("toolCallId"),
                    "name": raw.get("toolName"), "summary": raw.get("toolSummary") or content,
                    "status": "failed" if raw.get("isError") is True else "completed" if raw.get("isError") is False else "unknown"}]
    resources = [register_resource(ref) for ref in raw.get("mediaRefs", [])]
    for resource_id in resources:
        if isinstance(resource_id, str):
            content.append({"blockId": identifier + ":" + resource_id, "type": "resource", "resourceId": resource_id})
        else:
            content.append({"blockId": identifier + ":media-pending:" + str(len(content)), "type": "notice",
                            "code": "MEDIA_PENDING_HISTORY", "text": "Media requires an authorized history reference"})
    result = {"id": identifier, "threadId": thread_id, "segmentId": segment_id,
              "role": "tool" if raw.get("role") in ("toolResult", "function") else raw.get("role", "system"),
              "timestamp": millis(raw.get("timestamp")), "revision": hashlib.sha256(canonical(raw).encode()).hexdigest(),
              "content": content, "projection": projection, "generationState": "unknown",
              "completeness": "reference" if raw.get("fullMessageRef") else "complete"}
    if raw.get("fullMessageRef"):
        result["fullMessageRef"] = raw["fullMessageRef"]
    return result
