"""Stateless public Responses HTTP/SSE adapter for ChatGPT plan usage."""
import json
from http.client import HTTPException
from shellclaude_auth import AuthError, RemoteError, RESOURCE, request

NAMESPACE = "shellclaude"


def build_payload(model, messages, instructions, tools):
    history = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("_responses_output"):
            history.extend(msg["_responses_output"])
            continue
        if role == "tool":
            history.append({"type": "function_call_output", "call_id": msg["tool_call_id"],
                            "output": msg.get("content") or ""})
            continue
        if role not in ("user", "assistant", "developer", "system"):
            raise AuthError("Unsupported conversation role.")
        if msg.get("content"):
            history.append({"role": "developer" if role == "system" else role, "content": msg["content"]})
        for tc in msg.get("tool_calls", []):
            history.append({"type": "function_call", "call_id": tc["id"],
                            "name": tc["function"]["name"], "namespace": NAMESPACE,
                            "arguments": tc["function"].get("arguments", "{}")})
    functions = []
    for tool in tools:
        if tool.get("type") != "function":
            raise AuthError("Only local function tools are supported by this adapter.")
        f = tool["function"]
        functions.append({"type": "function", "name": f["name"],
                          "description": f.get("description", ""),
                          "parameters": f.get("parameters", {}), "strict": False})
    payload = {"model": model, "instructions": instructions, "input": history,
               "store": False, "stream": True, "include": ["reasoning.encrypted_content"]}
    if functions:
        payload["tools"] = [{"type": "namespace", "name": NAMESPACE,
                            "description": "Local shellclaude tools; existing approval rules apply.",
                            "tools": functions}]
    return payload


def sse_events(lines):
    """SSE framing: comments, CRLF, multiline data, strict JSON, bounded events."""
    data = []
    size = 0
    for raw in lines:
        try:
            line = raw.decode("utf-8").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        except UnicodeError:
            raise AuthError("Invalid UTF-8 in Responses stream.") from None
        if not line:
            if data:
                try:
                    event = json.loads("\n".join(data))
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise ValueError()
                except ValueError:
                    raise AuthError("Malformed Responses SSE event; no tools executed.") from None
                yield event
            data, size = [], 0
        elif line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data.append(value)
            size += len(value)
            if size > 8_000_000:
                raise AuthError("Responses SSE event too large.")
    # An event must end with a blank line; a truncated terminal event is failure.
    if data:
        raise AuthError("Interrupted SSE frame; no tools executed.")


def parse_stream(lines, tools, on_text=None, cancelled=None):
    names = {t["function"]["name"] for t in tools}
    terminal = None
    # Accumulate deltas for streaming and validate that a complete terminal snapshot exists.
    calls = {}
    for event in sse_events(lines):
        if cancelled and cancelled():
            raise AuthError("Responses request cancelled; no tools executed.")
        typ = event["type"]
        if typ == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise AuthError("Malformed text delta.")
            if on_text:
                on_text(delta)
        elif typ == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                calls[event.get("output_index")] = {**item, "arguments": item.get("arguments", "")}
        elif typ == "response.function_call_arguments.delta":
            index = event.get("output_index")
            if index not in calls or not isinstance(event.get("delta"), str):
                raise AuthError("Malformed streamed function arguments.")
            calls[index]["arguments"] += event["delta"]
        elif typ == "response.function_call_arguments.done":
            index = event.get("output_index")
            if index not in calls or not isinstance(event.get("arguments"), str):
                raise AuthError("Malformed completed function arguments.")
            calls[index]["arguments"] = event["arguments"]
        elif typ in ("error", "response.failed"):
            response = event.get("response") or event
            error = response.get("error") or {"code": event.get("code", "unknown_error")}
            raise RemoteError(0, {"error": error})
        elif typ == "response.incomplete":
            raise AuthError("Response incomplete; no tools executed. Reduce context or retry explicitly.")
        elif typ == "response.completed":
            terminal = event.get("response")
            break  # completed is the terminal event, not an SSE [DONE] marker.
    if not isinstance(terminal, dict) or terminal.get("status") != "completed" or not isinstance(terminal.get("output"), list):
        raise AuthError("Stream ended without a completed response; no tools executed. Retry explicitly.")
    text, tcalls, outputs = [], [], []
    ids = set()
    for item in terminal["output"]:
        typ = item.get("type")
        if item.get("status", "completed") != "completed":
            raise AuthError("Incomplete output item; no tools executed.")
        if typ == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part["text"])
                elif part.get("type") == "refusal":
                    text.append(part["refusal"])
        elif typ == "function_call":
            name, call_id, args = item.get("name"), item.get("call_id"), item.get("arguments")
            if name not in names or item.get("namespace", NAMESPACE) != NAMESPACE or not isinstance(call_id, str) or not call_id or call_id in ids:
                raise AuthError("Unknown or malformed function call; no tools executed.")
            try:
                if not isinstance(args, str) or not isinstance(json.loads(args), dict):
                    raise ValueError()
            except (ValueError, TypeError):
                raise AuthError("Invalid function arguments; no tools executed.") from None
            ids.add(call_id)
            tcalls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": args}})
        elif typ != "reasoning":
            raise AuthError("Unsupported output item; no tools executed.")
        outputs.append(item)
    # The final snapshot is authoritative. Never execute partial delta accumulators.
    for call in calls.values():
        if call.get("call_id") not in ids:
            raise AuthError("Incomplete function-call snapshot; no tools executed.")
    msg = {"role": "assistant", "content": "".join(text), "_responses_output": outputs}
    if tcalls:
        msg["tool_calls"] = tcalls
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if tcalls else "stop"}],
            "usage": terminal.get("usage") or {}}


class ResponsesProvider:
    def __init__(self, auth):
        self.auth = auth

    def models(self):
        data = request(RESOURCE + "/models", token=self.auth.access_token())
        try:
            models = data["models"]
            if not isinstance(models, list):
                raise ValueError()
            return [{"slug": m["slug"], "display_name": m["display_name"]} for m in models
                    if m.get("visibility") == "list" and isinstance(m.get("slug"), str)
                    and isinstance(m.get("display_name"), str)]
        except (TypeError, KeyError, ValueError, AttributeError):
            raise AuthError("Malformed account model catalog.") from None

    def call(self, model, messages, instructions, tools, on_text=None, cancelled=None):
        payload = build_payload(model, messages, instructions, tools)
        # Refresh only before submitting. Never retry a POST or replay an interrupted stream.
        token = self.auth.access_token()
        with request(RESOURCE + "/responses", payload=payload, token=token) as stream:
            try:
                return parse_stream(stream, tools, on_text, cancelled)
            except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
                raise AuthError("Malformed or interrupted Responses stream; no tools executed. Retry explicitly.") from None
