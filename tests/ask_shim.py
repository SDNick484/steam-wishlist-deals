#!/usr/bin/env python3
"""Run deals_server.py with ask.messages_api replaced by a scripted fake, for manual curl runs and tests.

Test entry point only; never makes a network call.

    python3 tests/ask_shim.py --port 8485 --bind 127.0.0.1 --data DIR --ask-env PATH   (same flags as the server)

Round 1 asks for get_overview and find_deals(scope=all, limit=3); round 2 calls show_results with every app id it
saw in the tool results and the answer "Shim answer.". Each call sleeps ASK_SHIM_DELAY seconds (default 0) and
reports 1000 input and 50 output tokens. The key in ask.env is never used.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ask  # noqa: E402
import deals_server  # noqa: E402

USAGE = {"input_tokens": 1000, "output_tokens": 50}


def _seen_appids(messages):
    seen = []
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if block.get("type") != "tool_result" or block.get("is_error"):
                continue
            try:
                found = set()
                ask.collect_appids(json.loads(block["content"]), found)
            except ValueError:
                continue
            seen += sorted(found - set(seen))
    return seen


def fake_messages_api(body, key, timeout):
    time.sleep(float(os.environ.get("ASK_SHIM_DELAY") or 0))
    messages = body.get("messages") or []
    if not any(isinstance(m.get("content"), list) and m["role"] == "user" for m in messages):
        content = [{"type": "tool_use", "id": "shim_1", "name": "get_overview", "input": {}},
                   {"type": "tool_use", "id": "shim_2", "name": "find_deals",
                    "input": {"scope": "all", "limit": 3}}]
    else:
        content = [{"type": "tool_use", "id": "shim_3", "name": "show_results",
                    "input": {"answer": "Shim answer.", "appids": _seen_appids(messages)}}]
    return {"content": content, "stop_reason": "tool_use", "usage": dict(USAGE)}


ask.messages_api = fake_messages_api

if __name__ == "__main__":
    sys.argv = [str(Path(deals_server.__file__))] + sys.argv[1:]
    deals_server.main()
