#!/usr/bin/env python3
"""Parse a Claude Code conversation.jsonl into a readable summary.

Usage:
  python3 parse_conversation.py <conversation.jsonl>
  python3 parse_conversation.py outputs/20260407-walking-plan/conversation.jsonl
  python3 parse_conversation.py conversation.jsonl --verbose  # include tool results
"""
import json
import sys


def parse(path, verbose=False):
    turn = 0
    total_cost = 0
    total_turns = 0
    tool_counts = {}
    errors = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = d.get("type")

            if msg_type == "assistant":
                turn += 1
                for c in d.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        name = c["name"]
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                        inp = c.get("input", {})

                        if name == "Bash":
                            detail = inp.get("command", "")[:120]
                        elif name in ("Read", "Write", "Edit"):
                            detail = inp.get("file_path", "")
                        elif name in ("WebFetch", "WebSearch"):
                            detail = inp.get("url", inp.get("query", ""))[:120]
                        elif name in ("Glob", "Grep"):
                            detail = inp.get("pattern", "")[:80]
                        else:
                            detail = str(inp)[:100]

                        print(f"[{turn:3d}] {name:12s} | {detail}")

                    elif c.get("type") == "text":
                        text = c.get("text", "").strip()
                        if len(text) > 30:
                            print(f"[{turn:3d}] {'THINKING':12s} | {text[:150]}")

            elif msg_type == "tool_result" and verbose:
                content = d.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                content = str(content).strip()
                if content:
                    preview = content[:200].replace("\n", " ")
                    is_error = d.get("is_error", False)
                    marker = "ERROR" if is_error else "ok"
                    print(f"      {'':12s} └─ ({marker}) {preview}")
                    if is_error:
                        errors.append(preview)

            elif msg_type == "result":
                total_cost = d.get("total_cost_usd", 0)
                total_turns = d.get("num_turns", 0)

    print()
    print("=" * 60)
    print(f"Turns: {total_turns}  |  Cost: ${total_cost:.2f}")
    print(f"Tool calls: {sum(tool_counts.values())}")
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"  {name:12s}: {count}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  - {e[:120]}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <conversation.jsonl> [--verbose]")
        sys.exit(2)

    path = sys.argv[1]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    parse(path, verbose=verbose)


if __name__ == "__main__":
    main()
