"""
verify_quote — agent-facing CLI for literal-quote verification.

The research atom uses this so it never has to generate Python for a task that
has a deterministic answer. Agent pattern:

    # Agent already fetched a page and wrote it to disk
    python -m pmf_engine.research.verify_quote /tmp/page.html --quote "The tax rate is $0.5551"
    # exit 0, stdout: {"match": true, "similarity": 1.0, "closest_match": "..."}

    # Or with stdin body
    cat /tmp/page.html | python -m pmf_engine.research.verify_quote - --quote "..."

    # Or with the quote in a file (for multi-line quotes)
    python -m pmf_engine.research.verify_quote /tmp/page.html --quote-file /tmp/quote.txt

Exit codes: 0 = match, 1 = no match, 2 = usage/IO error.
stdout is ALWAYS one JSON object (even on no-match), so orchestrators can
parse without caring about exit code.
"""

from __future__ import annotations
import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_quote",
        description="Literal-quote verification against a fetched page body.",
    )
    parser.add_argument(
        "body_path",
        help="Path to the file containing the page body. Use '-' to read from stdin.",
    )
    qgroup = parser.add_mutually_exclusive_group(required=True)
    qgroup.add_argument("--quote", help="Literal quote to look for in the body.")
    qgroup.add_argument("--quote-file", help="Path to a file containing the quote.")
    parser.add_argument(
        "--strict", action="store_true",
        help="Disable aggressive normalization (unicode/punctuation conflation). Default: aggressive."
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on usage errors; keep that contract.
        return int(e.code) if e.code is not None else 2

    try:
        if args.body_path == "-":
            body = sys.stdin.read()
        else:
            with open(args.body_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()

        if args.quote_file:
            with open(args.quote_file, "r", encoding="utf-8", errors="replace") as f:
                quote = f.read()
        else:
            quote = args.quote
    except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2

    from pmf_engine.research import verify

    result = verify.quote_in(body, quote, aggressive=not args.strict)
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
