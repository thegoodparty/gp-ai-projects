"""CloudWatch metric helper.

Absorbs the dozen-plus `try: cw.put_metric_data(...) except: logger.warning(...)`
wrappers scattered across pmf_engine. Metric emission must never kill the
calling code, so exceptions are swallowed and logged at debug level.
"""

from __future__ import annotations

from shared.aws_clients import get_client
from shared.logger import get_logger

logger = get_logger(__name__)


def emit_metric(
    *,
    namespace: str,
    name: str,
    dimensions: dict[str, str],
    value: float = 1,
    unit: str = "Count",
) -> None:
    try:
        cw = get_client("cloudwatch")
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": [
                        {"Name": k, "Value": v} for k, v in dimensions.items()
                    ],
                }
            ],
        )
    except Exception as e:
        logger.debug(f"Failed to emit metric {namespace}/{name}: {e}")
