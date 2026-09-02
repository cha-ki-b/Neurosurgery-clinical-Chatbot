"""The counters exist because the audit trail cannot see the failures.

`agentgateway_operation_log` records the calls the agent made to OpenMRS. A turn the assistant did
not understand makes none - it is answered from the interpreter and returns before any client is
built. Measured: four misunderstood turns in a row produce zero rows. So "how often does the
assistant fail to understand a clinician", the single most useful question about this system, was
unanswerable from the system of record, and remains unanswerable anywhere except here.

These tests pin what is counted and, just as importantly, what is not: no prompt, no name, no
value ever reaches a counter key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CHANNEL_SECRET
from tests.mock_openmrs import seed_patient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fresh_counters():
    from app.telemetry import telemetry

    telemetry.reset()
    yield
    telemetry.reset()


def say(client, mint, prompt, cid="tm"):
    return client.post(
        "/chat",
        json={"conversation_id": cid, "prompt": prompt,
              "delegated_token": mint(username="dr.benali", may_write=True, conversation_id=cid),
              "context": {"locale": "fr"}},
        headers={"X-Agent-Channel-Key": CHANNEL_SECRET},
    ).json()


def counters(client):
    response = client.get("/metrics", headers={"X-Agent-Channel-Key": CHANNEL_SECRET})
    assert response.status_code == 200
    return response.json()["counters"]


def test_a_turn_nobody_understood_is_counted_even_though_openmrs_never_hears_about_it(
    client, mint, mock_state
):
    say(client, mint, "commande une pizza")

    assert mock_state["calls"] == [], "precondition: this turn must not reach OpenMRS"
    assert counters(client)["turns.state.unsupported"] == 1


def test_states_and_tasks_are_counted_separately(client, mint, openmrs_server):
    seed_patient(openmrs_server["app"], "Benali", ["Amine"], "1978-04-03")

    say(client, mint, "cherche le patient Benali", cid="a")
    say(client, mint, "cree un patient", cid="b")

    found = counters(client)
    assert found["turns.total"] == 2
    assert found["turns.state.answered"] == 1
    assert found["turns.state.awaiting_clarification"] == 1
    assert found["turns.task.search_patient"] == 1
    assert found["turns.task.create_patient"] == 1


def test_an_abandoned_frame_is_counted_by_the_task_it_lost(client, mint):
    say(client, mint, 'cree un patient nomme "Test Un"', cid="c")
    say(client, mint, "commande une pizza", cid="c")

    found = counters(client)
    assert found["frame.abandoned"] == 1
    assert found["frame.abandoned.create_patient"] == 1


def test_a_rejected_value_is_counted_by_the_slot_that_rejected_it(client, mint):
    say(client, mint, 'cree un patient nomme "Ahmed Mustafa"', cid="d")
    say(client, mint, "masculin", cid="d")
    say(client, mint, "20/09/2099", cid="d")

    assert counters(client)["slot.rejected.birthdate"] == 1


def test_no_counter_key_can_contain_patient_data(client, mint, openmrs_server):
    """A counter name is written to logs and read by whoever runs /metrics."""
    seed_patient(openmrs_server["app"], "Belkacemi", ["Zoubir"], "1961-02-14")

    say(client, mint, 'cree un patient nomme "Zoubir Belkacemi"', cid="e")
    say(client, mint, "masculin", cid="e")
    say(client, mint, "commande une pizza", cid="e")
    say(client, mint, "cherche le patient Belkacemi", cid="f")

    keys = " ".join(counters(client))
    for secret in ("Zoubir", "Belkacemi", "zoubir", "belkacemi", "1961"):
        assert secret not in keys, f"{secret!r} leaked into a counter name"


def test_latency_is_bucketed_not_recorded_per_turn(client, mint):
    say(client, mint, "bonjour", cid="g")

    latency = [key for key in counters(client) if key.startswith("turns.latency.")]
    assert len(latency) == 1


def test_the_counters_need_the_channel_key(client, mint):
    say(client, mint, "bonjour", cid="h")

    assert client.get("/metrics").status_code == 403
