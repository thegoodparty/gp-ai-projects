import os
import sys

if os.environ.get("BROKER_URL"):
    try:
        from pmf_engine.runner.pmf_runtime.egress_guard import install

        install()
    except Exception as e:
        print(
            f"SANDBOX EGRESS GUARD FAILED TO INSTALL ({type(e).__name__}: {e}) "
            "— container is running WITHOUT socket-level egress protection",
            file=sys.stderr,
            flush=True,
        )
