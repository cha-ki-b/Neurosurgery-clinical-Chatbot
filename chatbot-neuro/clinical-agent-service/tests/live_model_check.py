"""End-to-end against the real MedGemma weights and a mock OpenMRS. Run by hand, never in CI.

    docker run --rm --network server2_net -v $PWD:/work -w /work -e PYTHONPATH=/work \
      -e NLU_ENGINE=medgemma -e LLM_BASE_URL=http://vllm:8000/v1 \
      --entrypoint python <image> -m pytest tests/live_model_check.py -q -s

The point is the half the unit tests cannot reach: the deterministic frame is exercised by every
other test with the rules engine, and this checks that the same conversations behave the same way
when a 4B model is the one reading the sentences. No real patient data is touched - OpenMRS is the
same mock the rest of the suite uses.
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


def run(client, mint, turns, cid):
    out = []
    for turn in turns:
        body = client.post(
            "/chat",
            json={"conversation_id": cid, "prompt": turn,
                  "delegated_token": mint(username="dr.x", may_write=True, conversation_id=cid),
                  "context": {"locale": "fr"}},
            headers={"X-Agent-Channel-Key": CHANNEL_SECRET},
        ).json()
        print(f"  USER  {turn}\n  BOT   [{body['state']}/{body['task_type']}] {body['reply']}\n")
        out.append(body)
    return out


def test_the_reported_failure_no_longer_loops(client, mint, openmrs_server):
    seed_patient(openmrs_server["app"], "El", ["Fateh", "Mohammed"], "1995-09-22", identifier="10008D")
    print("\n=== the reported conversation, with the real model ===")
    turns = run(client, mint, [
        "modifie le patient Fateh Mohammed El",
        "the telephone",
        "change it to 06564565",
    ], cid="live-1")
    assert turns[-1]["state"] == "awaiting_confirmation", "still looping"
    assert "06564565" in turns[-1]["reply"]
    assert "Fateh" in turns[-1]["reply"], "the patient was not named in the summary"


def test_english_follow_ups_keep_the_french_frame(client, mint, openmrs_server):
    seed_patient(openmrs_server["app"], "El", ["Fateh", "Mohammed"], "1995-09-22", identifier="10008D")
    print("\n=== mixed English / French ===")
    turns = run(client, mint, [
        "update Fateh Mohammed El",
        "his phone",
        "make it 0777889900",
    ], cid="live-2")
    assert turns[-1]["state"] == "awaiting_confirmation"
    assert "0777889900" in turns[-1]["reply"]


def test_an_invalid_date_is_refused_and_the_create_survives(client, mint):
    print("\n=== invalid date, then correction ===")
    turns = run(client, mint, [
        'cree un patient nomme "Ahmed Mustafa"',
        "male",
        "20-99-2008",
        "20-09-2008",
    ], cid="live-3")
    assert turns[2]["state"] == "awaiting_clarification"
    assert turns[3]["state"] == "awaiting_confirmation"
    assert "Ahmed Mustafa" in turns[3]["reply"], "the name had to be retyped"


def test_a_pronoun_is_not_searched_as_a_patient(client, mint, openmrs_server):
    seed_patient(openmrs_server["app"], "El", ["Fateh", "Mohammed"], "1995-09-22", identifier="10008D")
    print("\n=== pronoun reference ===")
    turns = run(client, mint, [
        "cherche le patient Fateh",
        "generate a report for him",
    ], cid="live-4")
    assert "aucun patient" not in turns[-1]["reply"].lower(), "a pronoun was searched as a name"
