"""A write is not believed because OpenMRS returned 200.

The deployment this runs against has a documented endpoint that accepts a change and silently
discards it: a FHIR PUT replacing an existing telecom or name returns 200 and alters nothing,
because fhir2 1.2.2 maps each incoming entry to a new object that Hibernate's Set drops as
already-present. The write path was moved off FHIR for that reason - but until now the assistant
still reported success from the status code, so the *class* of failure was one it could not see.

These tests pin the three outcomes: the value is there, the value is not there, and the record
could not be re-read. The middle one is the one that used to read "c'est enregistre".
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


def say(client, mint, prompt, cid="verify"):
    return client.post(
        "/chat",
        json={"conversation_id": cid, "prompt": prompt,
              "delegated_token": mint(username="dr.benali", may_write=True, conversation_id=cid),
              "context": {"locale": "fr"}},
        headers={"X-Agent-Channel-Key": CHANNEL_SECRET},
    ).json()


@pytest.fixture
def patient(openmrs_server):
    return seed_patient(openmrs_server["app"], "El", ["Fateh", "Mohammed"], "1995-09-22", identifier="10008D")


def test_a_write_that_really_lands_is_reported_as_saved(client, mint, patient):
    say(client, mint, "mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = say(client, mint, "oui")

    assert body["state"] == "answered"
    assert "enregistre" in body["reply"].lower()


def test_a_write_accepted_but_not_applied_is_reported_as_a_failure(client, mint, patient, monkeypatch):
    """The exact shape of the fhir2 defect: 200 back, nothing changed."""
    import app.openmrs_client as openmrs_client

    original = openmrs_client.OpenmrsClient.call

    async def swallow_writes(self, method, path, body=None):
        if method.upper() == "GET":
            return await original(self, method, path, body)
        # Accepted, and quietly dropped - never reaches the record.
        return openmrs_client.ApiResult(status=200, body={"uuid": "whatever"})

    monkeypatch.setattr(openmrs_client.OpenmrsClient, "call", swallow_writes)

    say(client, mint, "mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = say(client, mint, "oui")

    assert body["state"] == "failed", "a silently-dropped write was reported as saved"
    assert "echec" in body["reply"].lower()
    assert "telephone" in body["reply"].lower()


def test_a_name_change_that_does_not_land_is_caught_too(client, mint, patient, monkeypatch):
    import app.openmrs_client as openmrs_client

    original = openmrs_client.OpenmrsClient.call

    async def swallow_writes(self, method, path, body=None):
        if method.upper() == "GET":
            return await original(self, method, path, body)
        return openmrs_client.ApiResult(status=200, body={"uuid": "whatever"})

    monkeypatch.setattr(openmrs_client.OpenmrsClient, "call", swallow_writes)

    say(client, mint, "modifie le patient Fateh Mohammed El")
    say(client, mint, "le nom")
    say(client, mint, "Walter Black")
    body = say(client, mint, "oui")

    assert body["state"] == "failed"
    assert "nom" in body["reply"].lower()


def test_an_unreadable_record_neither_claims_success_nor_invents_a_failure(client, mint, patient, monkeypatch):
    """The write may well have worked. Saying "c'est enregistre" and saying "echec" are both lies."""
    import app.openmrs_client as openmrs_client

    original = openmrs_client.OpenmrsClient.call
    seen = {"writes": 0}

    async def fail_the_readback(self, method, path, body=None):
        if method.upper() != "GET":
            seen["writes"] += 1
            return await original(self, method, path, body)
        if seen["writes"] and "Patient/" in path:
            raise openmrs_client.OpenmrsUnavailable("timeout")
        return await original(self, method, path, body)

    monkeypatch.setattr(openmrs_client.OpenmrsClient, "call", fail_the_readback)

    say(client, mint, "mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = say(client, mint, "oui")

    assert body["state"] == "answered"
    assert "n'ai pas pu relire" in body["reply"]
    assert "echec" not in body["reply"].lower()


def test_the_readback_reads_a_different_route_than_the_write(client, mint, patient, mock_state):
    """Re-reading through the endpoint that did the writing would only echo it back."""
    say(client, mint, "mets a jour le telephone de Fateh Mohammed El a 0555123456")
    say(client, mint, "oui")

    writes = [call for call in mock_state["calls"] if call["method"] == "POST"]
    readbacks = [call for call in mock_state["calls"]
                 if call["method"] == "GET" and "/ws/fhir2/R4/Patient/" in call["path"]]

    assert writes, "no write was issued"
    assert readbacks, "no read-back was issued"
    assert all("/ws/rest/v1/person/" in call["path"] for call in writes)


# --------------------------------------------------------------------------- creates


def test_a_created_record_holding_a_different_birthdate_is_flagged_not_hidden(client, mint, monkeypatch):
    """The shape of the timezone defect: the patient is created, with the wrong date of birth.

    `timezone.conversions` is false on this OpenMRS (Finding 36), so dates cross the REST layer as
    naive local timestamps. A patient created with 2008-09-20 was observed listed afterwards as
    2008-09-19. Nothing in the chain looked at the stored value, so the assistant reported a clean
    success and the record kept the wrong date.
    """
    import app.openmrs_client as openmrs_client

    original = openmrs_client.OpenmrsClient.call

    async def shift_the_stored_birthdate(self, method, path, body=None):
        result = await original(self, method, path, body)
        if method.upper() == "GET" and "/ws/fhir2/R4/Patient/" in path and isinstance(result.body, dict):
            if result.body.get("birthDate") == "2008-09-20":
                shifted = dict(result.body)
                shifted["birthDate"] = "2008-09-19"
                return openmrs_client.ApiResult(status=result.status, body=shifted)
        return result

    monkeypatch.setattr(openmrs_client.OpenmrsClient, "call", shift_the_stored_birthdate)

    say(client, mint, 'cree un patient nomme "Ahmed Mustafa"', cid="create-tz")
    say(client, mint, "masculin", cid="create-tz")
    say(client, mint, "20/09/2008", cid="create-tz")
    body = say(client, mint, "oui", cid="create-tz")

    # Created, so not a failure - but the discrepancy is named.
    assert body["state"] == "answered"
    assert "2008-09-19" in body["reply"]
    assert "2008-09-20" in body["reply"]
    assert "ne le recreez pas" in body["reply"].lower(), "a clinician could be led to create a duplicate"


def test_a_correctly_created_record_is_reported_without_a_warning(client, mint):
    say(client, mint, 'cree un patient nomme "Ahmed Mustafa"', cid="create-ok")
    say(client, mint, "masculin", cid="create-ok")
    say(client, mint, "20/09/2008", cid="create-ok")
    body = say(client, mint, "oui", cid="create-ok")

    assert body["state"] == "answered"
    assert "ATTENTION" not in body["reply"]
    assert "cree" in body["reply"].lower()
