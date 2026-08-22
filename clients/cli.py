#!/usr/bin/env python3
"""Minimal httpx chat client for Felix.

Usage:
    uv run python clients/cli.py [--base http://localhost:8080] [--manifest quick]
    uv run python clients/cli.py --thread demo --model claude-haiku-4 --stream
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Felix HTTP chat client")
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--manifest", default="quick")
    parser.add_argument("--token", default="", help="Bearer token (optional)")
    parser.add_argument("--thread", default="", help="Thread id suffix (optional)")
    parser.add_argument("--model", default="", help="Model override (allowlisted)")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    headers: dict[str, str] = {"content-type": "application/json"}
    if args.token:
        headers["authorization"] = f"Bearer {args.token}"

    print(f"felix chat → {args.base}  manifest={args.manifest}")
    if args.thread:
        print(f"thread={args.thread}")
    if args.model:
        print(f"model={args.model}")
    print("Type a message (or 'exit').\n")

    with httpx.Client(base_url=args.base, headers=headers, timeout=120.0) as client:
        health = client.get("/health")
        health.raise_for_status()
        print(f"health: {health.json()}\n")

        while True:
            try:
                line = input("you> ").strip()
            except EOFError, KeyboardInterrupt:
                print("\nbye")
                break
            if line in {"exit", "quit"}:
                break
            if not line:
                continue

            body: dict = {
                "manifest": args.manifest,
                "messages": [{"role": "user", "content": line}],
            }
            if args.thread:
                body["thread_id"] = args.thread
            if args.model:
                body["model"] = args.model
            if args.stream:
                with client.stream("POST", "/chat/stream", json=body) as resp:
                    resp.raise_for_status()
                    print("agent> ", end="", flush=True)
                    for raw in resp.iter_lines():
                        if not raw or not raw.startswith("data: "):
                            continue
                        data = raw[6:]
                        if data == "[DONE]":
                            print()
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        text = (
                            event.get("text")
                            or event.get("delta")
                            or (event.get("data") or {}).get("delta")
                            or ""
                        )
                        if not text and event.get("event") in {
                            "text_delta",
                            "on_chat_model_stream",
                        }:
                            chunk = (event.get("data") or {}).get("chunk") or {}
                            text = chunk.get("content") or ""
                        if text:
                            print(text, end="", flush=True)
                    print()
            else:
                resp = client.post("/chat", json=body)
                resp.raise_for_status()
                payload = resp.json()
                final = payload.get("final") or {}
                content = final.get("content") if isinstance(final, dict) else final
                print(f"agent> {content}\n")


if __name__ == "__main__":
    main()
    sys.exit(0)
