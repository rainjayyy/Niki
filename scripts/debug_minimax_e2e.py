#!/usr/bin/env python3
"""End-to-end smoke test for the Minimax (LLM) wiring.

Usage:
    python scripts/debug_minimax_e2e.py

What it does:
1. Logs in via the mock SMS login flow to get a JWT.
2. POSTs a real Chinese question to /api/v1/encyclopedia/ask.
3. Prints the model's reply.

Requirements:
- uvicorn must be running on http://localhost:8000 with
  MINIMAX_API_KEY set (otherwise you get the stub response).
- The body file approach (--data-binary @file) avoids the Windows
  curl encoding trap that mangles Chinese chars to Latin-1.

Quick check from a separate shell:
    MINIMAX_API_KEY=sk-... python scripts/debug_minimax_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

API = os.environ.get("NIKI_API_BASE", "http://localhost:8000")


def _post_json(path: str, body: dict, *, token: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    phone = os.environ.get("NIKI_TEST_PHONE", "13800130000")

    # 1. Send SMS code (mock provider returns dev_code in response).
    send = _post_json("/api/v1/auth/sms/send", {"phone": phone})
    code = send["dev_code"]
    print(f"[1/3] SMS code: {code}")

    # 2. Login → access token.
    login = _post_json(
        "/api/v1/auth/sms/login", {"phone": phone, "code": code}
    )
    token = login["access_token"]
    print(f"[2/3] Login OK (parent={login['parent_id']})")

    # 3. Ask the encyclopedia agent — this hits the real Minimax LLM
    # if MINIMAX_API_KEY is set, otherwise returns the stub phrase.
    question = "恐龙为什么会灭绝？"
    answer = _post_json(
        "/api/v1/encyclopedia/ask",
        {"question": question, "child_age": 7},
        token=token,
    )
    print(f"[3/3] Question: {question}")
    print(f"      Answer  : {answer['text']}")
    print(f"      Sources : {answer.get('metadata', {}).get('sources', [])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        sys.exit(1)