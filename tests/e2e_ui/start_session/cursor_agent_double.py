#!/usr/bin/env python3
"""Signed-in ``cursor-agent`` double: version, status, a fixed model list, and a TUI."""

import json
import os
import sys
import time

ARGS = sys.argv[1:]
VERSION = "2026.09.26-dd393fe"

log_path = os.environ.get("CURSOR_AGENT_DOUBLE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": ARGS, "cwd": os.getcwd(), "time": time.time()}) + "\n")

if "--version" in ARGS or "-v" in ARGS:
    print(VERSION)
    raise SystemExit(0)
if ARGS[:1] == ["status"]:
    status = {
        "status": "authenticated",
        "isAuthenticated": True,
        "hasAccessToken": True,
        "hasRefreshToken": True,
        "version": VERSION,
    }
    print(json.dumps(status))
    raise SystemExit(0)
if ARGS[:1] == ["models"]:
    with open(os.environ["CURSOR_AGENT_DOUBLE_MODELS_FILE"], encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    raise SystemExit(0)
if ARGS[:1] in (["login"], ["logout"]):
    print("stub cursor-agent: " + ARGS[0] + " ok")
    raise SystemExit(0)

# A launched TUI stays attached until stdin closes.
print("stub cursor-agent TUI started with: " + " ".join(ARGS))
sys.stdout.flush()
try:
    while sys.stdin.readline():
        pass
except KeyboardInterrupt:
    pass
