"""Filtering a patient list by date, and being honest about which date.

The screenshot that started this asked "how many patients got created today" and got the entire
patient list back, unqualified - which reads as an answer. Two separate things were wrong: no date
filter was ever sent, and nothing in the reply said so.

The deployed fhir2's CapabilityStatement advertises `_lastUpdated` and `birthdate` on Patient, and
nothing resembling a creation date. So the filter is real now, and it filters on *modification*.
Answering a question about creation with a count of modifications is fine only if the reply says
that is what it did.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CHANNEL_SECRET
from tests.mock_openmrs import seed_patient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def say(client, mint, prompt, cid="dates"):
    return client.post(
        "/chat",
        json={"conversation_id": cid, "prompt": prompt,
              "delegated_token": mint(username="dr.benali", may_write=True, conversation_id=cid),
              "context": {"locale": "fr"}},
        headers={"X-Agent-Channel-Key": CHANNEL_SECRET},
    ).json()


@pytest.fixture
def two_eras(openmrs_server):
    """One patient touched today, one touched long ago."""
    today = dt.date.today().isoformat()
    seed_patient(openmrs_server["app"], "Recent", ["Rania"], "1990-01-01",
                 identifier="1000RE", last_updated=f"{today}T08:00:00.000+01:00")
    seed_patient(openmrs_server["app"], "Ancien", ["Amine"], "1970-01-01",
                 identifier="1000AN", last_updated="2020-01-01T08:00:00.000+01:00")


def test_today_narrows_the_list_instead_of_returning_everyone(client, mint, two_eras):
    body = say(client, mint, "liste les patients d'aujourd'hui")

    assert body["state"] == "answered"
    assert "Rania" in body["reply"]
    assert "Amine" not in body["reply"], "the date filter was not applied"


def test_the_filter_actually_reaches_openmrs(client, mint, two_eras, mock_state):
    """Guards against a filter that looks right and is silently dropped by the server."""
    say(client, mint, "liste les patients d'aujourd'hui")

    searches = [call for call in mock_state["calls"] if call["method"] == "GET" and "Patient" in call["path"]]
    assert any("_lastUpdated=ge" in call["query"] for call in searches), \
        f"no date filter was sent: {[c['query'] for c in searches]}"


def test_the_reply_says_it_filtered_on_modification_not_creation(client, mint, two_eras):
    """The honesty half. OpenMRS cannot filter on creation date; the answer must not pretend."""
    body = say(client, mint, "combien de patients ont ete crees aujourd'hui")

    assert "modifies depuis" in body["reply"]
    assert "date de creation" in body["reply"], "the approximation was not disclosed"


def test_an_unfiltered_list_still_says_it_is_unfiltered(client, mint, two_eras):
    body = say(client, mint, "liste tous les patients")

    assert "aucun filtre" in body["reply"]
    assert "Rania" in body["reply"] and "Amine" in body["reply"]


def test_a_week_window_starts_on_monday(client, mint, openmrs_server):
    """A rolling seven days answers a different question on a Wednesday than "this week" does."""
    monday = dt.date.today() - dt.timedelta(days=dt.date.today().weekday())
    seed_patient(openmrs_server["app"], "Lundi", ["Leila"], "1990-01-01",
                 last_updated=f"{monday.isoformat()}T08:00:00.000+01:00")
    seed_patient(openmrs_server["app"], "Avant", ["Ali"], "1990-01-01",
                 last_updated=f"{(monday - dt.timedelta(days=1)).isoformat()}T08:00:00.000+01:00")

    body = say(client, mint, "liste les patients de cette semaine")

    assert "Leila" in body["reply"]
    assert "Ali" not in body["reply"]


def test_gender_and_date_compose(client, mint, openmrs_server):
    today = dt.date.today().isoformat()
    seed_patient(openmrs_server["app"], "Homme", ["Hakim"], "1990-01-01",
                 last_updated=f"{today}T08:00:00.000+01:00")

    body = say(client, mint, "liste tous les patients de sexe masculin d'aujourd'hui")

    assert body["state"] == "answered"
    assert "sexe masculin" in body["reply"]
    assert "modifies depuis" in body["reply"]
