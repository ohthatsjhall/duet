#!/usr/bin/env python3
"""Reads an agent's streamed JSON events on stdin and prints the readable parts.

Handles both shapes: Codex `exec --json` thread items, and Claude's stream JSON.
"""
import sys, json

DIM, BOLD, CYAN, RED, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[31m", "\033[33m", "\033[0m"

def out(s):
    print(s, flush=True)

def item(it):
    kind = it.get("type")
    if kind == "agent_message":
        text = (it.get("text") or "").strip()
        if text: out(f"{BOLD}{text}{RESET}\n")
    elif kind == "reasoning":
        text = (it.get("text") or it.get("summary") or "").strip()
        if text: out(f"{DIM}{text}{RESET}")
    elif kind == "command_execution":
        cmd = (it.get("command") or "").replace("/bin/zsh -lc ", "").strip()
        code = it.get("exit_code")
        if code is None:
            out(f"{CYAN}$ {cmd[:200]}{RESET}")
        elif code != 0:
            out(f"{RED}  exit {code}{RESET}")
    elif kind == "file_change":
        for c in it.get("changes", []) or []:
            out(f"{YELLOW}  [{c.get('kind','edit')}] {c.get('path','')}{RESET}")
    elif kind == "error":
        out(f"{RED}  {it.get('message','')[:300]}{RESET}")
    elif kind == "todo_list":
        for t in it.get("items", []) or []:
            mark = "x" if t.get("completed") else " "
            out(f"{DIM}  [{mark}] {t.get('text','')}{RESET}")

seen = set()
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        e = json.loads(line)
    except Exception:
        continue
    t = e.get("type")
    if t in ("item.started", "item.completed"):
        it = e.get("item", {})
        key = (it.get("id"), it.get("type"), t)
        # a started command prints its line; the completed one only prints failure
        if key in seen:
            continue
        seen.add(key)
        item(it)
    elif t == "turn.completed":
        u = e.get("usage", {})
        out(f"{DIM}— turn done ({u.get('input_tokens','?')} in / {u.get('output_tokens','?')} out){RESET}")
    elif t == "turn.failed":
        out(f"{RED}— turn failed: {json.dumps(e.get('error'))[:200]}{RESET}")
