"""Project-root pytest config.

The autouse fixture below blocks every test in the repo from emitting
real Braintrust telemetry. Several test suites (campaign_plan_lambda,
pmf_engine smoke tests, hubspot_ddhq_match) call into application code
that in turn calls `init_braintrust(...)` / `BraintrustClient.init(...)`
without mocking. With `BRAINTRUST_API_KEY` present in the local `.env`,
those tests would otherwise authenticate to Braintrust and pollute
whichever project ended up first to grab the singleton.

Clearing the API key keeps `BraintrustClient` in its disabled state for
the duration of each test — `traced_span`, `traced_call`, and
`load_prompt` all become no-ops. Resetting the singleton around each
test prevents state leaking between tests if any of them set the key
locally for their own assertions.
"""
import pytest

from shared.braintrust import BraintrustClient


@pytest.fixture(autouse=True)
def disable_braintrust(monkeypatch):
    monkeypatch.setenv("BRAINTRUST_API_KEY", "")
    BraintrustClient.reset_instance()
    yield
    BraintrustClient.reset_instance()
