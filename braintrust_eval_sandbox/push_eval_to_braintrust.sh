#!/usr/bin/env bash
# Push braintrust_eval_sandbox/evals.py to Braintrust as a sandbox eval.
#
# Why the Python wrapper? The vanilla `braintrust push` CLI walks the import
# graph of the eval file to decide which local source files to ship. Two
# things break that walker for us:
#   1. uv adds the project root to sys.path[1:] (in addition to leaving an
#      empty string at [0]), so the bundler's `_under_rest` check defers
#      every submodule.
#   2. The push CLI imports the eval with _set_lazy_load(True), which
#      short-circuits Eval() so the task body (and thus its transitive
#      imports) never runs — only the eval module's top-level imports are
#      observed, and even those get filtered out by problem #1.
# Net effect: only `braintrust_eval_sandbox/__init__.py` and
# `shared/__init__.py` end up in the zip, the sandbox crashes with `No
# module named 'braintrust_eval_sandbox.evals'`, and we lose hours figuring
# it out.
#
# Workaround: sanitize sys.path AND explicitly enumerate every .py file
# under our two top-level packages so the bundler ships them regardless of
# what the auto-walker missed.
#
# Run from project root:
#   ./braintrust_eval_sandbox/push_eval_to_braintrust.sh
#
# Requires BRAINTRUST_API_KEY in env. The dev dep `braintrust[cli]` must be
# installed (uv sync handles this).
#
# Sandbox runtime configuration (one-time, in Braintrust UI):
#   - GEMINI_API_KEY: set in Project Settings → Environment Variables.
#   - ENVIRONMENT=eval: also in Environment Variables. Stamps every span
#     emitted from sandbox runs with `metadata.environment="eval"` so the
#     Logs view can filter eval rows separately from prod.

set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

uv run python -c "
import sys, os
print('[push_eval] wrapper starting', file=sys.stderr, flush=True)
# Load .env so BRAINTRUST_API_KEY (and friends) are available to the push CLI.
# shared/braintrust.py loads .env when imported, but the braintrust CLI doesn't
# go through that module, so we have to do it explicitly here.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
cwd = os.path.abspath(os.getcwd())
sys.path[:] = [cwd] + [p for p in sys.path if p and os.path.abspath(p) != cwd]
print(f'[push_eval] cleaned sys.path[0]={sys.path[0]!r}', file=sys.stderr, flush=True)
print(f'[push_eval] sys.path[1:]={sys.path[1:]!r}', file=sys.stderr, flush=True)

# Explicit list of local source files the eval depends on. Add an entry when
# evals.py or its callees gain a new local import. Allow-list (not os.walk)
# so build artifacts under .build/, tests/, and unrelated shared modules
# don't get sucked into the bundle.
EVAL_SOURCE_FILES = [
    'braintrust_eval_sandbox/__init__.py',
    'braintrust_eval_sandbox/evals.py',
    'shared/__init__.py',
    'shared/braintrust.py',
    'shared/llm_gemini_3.py',
    'shared/logger.py',
]

import braintrust.cli.push as _push_mod
print(f'[push_eval] patching {_push_mod.__file__}', file=sys.stderr, flush=True)
_orig_import = _push_mod._import_module
def _augmented_import(name, path):
    sources = _orig_import(name, path)
    seen = {os.path.abspath(s) for s in sources}
    for rel in EVAL_SOURCE_FILES:
        abs_path = os.path.abspath(rel)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f'EVAL_SOURCE_FILES entry missing: {rel}')
        if abs_path not in seen:
            sources.append(abs_path)
            seen.add(abs_path)
    return sources
_push_mod._import_module = _augmented_import

_orig_upload = _push_mod._upload_bundle
def _logged_upload(entry_module_name, sources, requirements):
    print(f'[push_eval] entry: {entry_module_name}', file=sys.stderr, flush=True)
    print(f'[push_eval] {len(sources)} sources in bundle:', file=sys.stderr, flush=True)
    for s in sorted(sources):
        try:
            rel = os.path.relpath(s)
        except ValueError:
            rel = s
        print(f'  {rel}', file=sys.stderr, flush=True)
    return _orig_upload(entry_module_name, sources, requirements)
_push_mod._upload_bundle = _logged_upload
print('[push_eval] patches applied; invoking main()', file=sys.stderr, flush=True)

from braintrust.cli.__main__ import main
sys.argv = [
    'braintrust', 'push',
    'braintrust_eval_sandbox/evals.py',
    '--requirements', 'braintrust_eval_sandbox/evals.requirements.txt',
    '--if-exists', 'replace',
]
sys.exit(main())
"
