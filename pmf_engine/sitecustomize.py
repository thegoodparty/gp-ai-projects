import os

if os.environ.get("BROKER_URL"):
    try:
        from pmf_engine.runner.pmf_runtime.egress_guard import install

        install()
    except Exception:
        pass
