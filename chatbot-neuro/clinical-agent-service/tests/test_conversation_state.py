"""The conversational contract: what the assistant must remember, resolve, validate and refuse.

Every scenario here is a whole conversation over real HTTP, because that is the only level at
which the property under test is even expressible. A unit test of the interpreter cannot tell you
whether "change it to 06564565" reaches the right patient's phone number three turns after anybody
last said either word.

The corpus is organised by what it protects:

* **context** - a request survives the turns it takes to state it;
* **reference** - "it", "him", "the same patient" resolve against the frame, never against a guess;
* **validation** - a value a patient record cannot hold is refused before the clinician approves it;
* **honesty** - nothing is claimed that the backend did not confirm, and nothing is invented.

The last group is the one that must never regress. A friction bug wastes a clinician's turn; an
honesty bug puts a false fact in front of someone making a clinical decision.
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


class Chat:
    """One clinician, one conversation, turn after turn - as the browser drives it."""

    def __init__(self, client, mint, conversation_id="conv-state", username="dr.benali", may_write=True):
        self._client = client
        self._mint = mint
        self._id = conversation_id
        self._username = username
        self._may_write = may_write

    def say(self, prompt, patient_uuid=None):
        payload = {
            "conversation_id": self._id,
            "prompt": prompt,
            "delegated_token": self._mint(
                username=self._username, may_write=self._may_write, conversation_id=self._id
            ),
            "context": {"patient_uuid": patient_uuid, "locale": "fr"} if patient_uuid else {"locale": "fr"},
        }
        response = self._client.post("/chat", json=payload, headers={"X-Agent-Channel-Key": CHANNEL_SECRET})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def chat(client, mint):
    return Chat(client, mint)


@pytest.fixture
def fateh(openmrs_server):
    return seed_patient(openmrs_server["app"], "El", ["Fateh", "Mohammed"], "1995-09-22", identifier="10008D")


# ============================================================== 1-4  basic task shapes


def test_01_patient_search_answers_directly(chat, fateh):
    body = chat.say("cherche le patient Fateh")
    assert body["state"] == "answered"
    assert body["task_type"] == "search_patient"
    assert "Fateh" in body["reply"]


def test_02_a_single_search_hit_becomes_the_active_patient(chat, fateh):
    chat.say("cherche le patient Fateh")
    body = chat.say("affiche son dossier")

    assert body["state"] == "answered"
    assert body["task_type"] == "get_patient_summary"
    assert "de quel patient" not in body["reply"].lower()


def test_03_a_single_turn_update_goes_straight_to_confirmation(chat, fateh):
    body = chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    assert body["state"] == "awaiting_confirmation"
    assert "0555123456" in body["reply"]


def test_04_a_read_never_asks_for_confirmation(chat, fateh):
    body = chat.say("cherche le patient Fateh")
    assert body["pending_action"] is None


# ============================================================== 5-9  multi-turn slot filling


def test_05_naming_a_field_is_a_complete_answer(chat, fateh):
    """The failure this whole redesign exists for.

    "le telephone" names the field and nothing else. It used to be treated as an unanswerable
    reply, which threw the request away and asked the same question again on the next turn.
    """
    chat.say("modifie le patient Fateh Mohammed El")
    body = chat.say("le telephone")

    assert body["state"] == "awaiting_clarification"
    assert body["task_type"] == "update_patient_demographics"
    assert "telephone" in body["reply"].lower()
    assert "que faut-il modifier" not in body["reply"].lower(), "the field question was repeated"


def test_06_it_resolves_to_the_active_field(chat, fateh):
    chat.say("modifie le patient Fateh Mohammed El")
    chat.say("le telephone")
    body = chat.say("change it to 06564565")

    assert body["state"] == "awaiting_confirmation"
    assert "06564565" in body["reply"]


def test_07_a_bare_value_answers_the_field_question(chat, fateh):
    chat.say("modifie le patient Fateh Mohammed El")
    chat.say("le telephone")
    body = chat.say("06564565")

    assert body["state"] == "awaiting_confirmation"
    assert "06564565" in body["reply"]


def test_08_the_field_and_its_value_may_arrive_together(chat, fateh):
    chat.say("modifie le patient Fateh Mohammed El")
    body = chat.say("le telephone est 06564565")

    assert body["state"] == "awaiting_confirmation"
    assert "06564565" in body["reply"]


def test_09_a_create_assembled_over_four_turns_keeps_every_value(chat):
    chat.say("cree un patient")
    chat.say("Amine Benali")
    chat.say("masculin")
    body = chat.say("ne le 03/04/1978")

    assert body["state"] == "awaiting_confirmation"
    assert "Amine Benali" in body["reply"]
    assert "1978-04-03" in body["reply"]
    assert "masculin" in body["reply"]


# ============================================================== 10-13 reference resolution


def test_10_the_confirmation_always_names_the_patient(chat, fateh):
    """A change approved without being told whose record it lands in is a blind write."""
    chat.say("cherche le patient Fateh")
    body = chat.say("mets son telephone a 0555123456")

    assert body["state"] == "awaiting_confirmation"
    assert "MODIFIER la fiche du patient  :" not in body["reply"], "the patient was left blank"
    assert "Fateh" in body["reply"]


def test_11_a_pronoun_is_never_searched_as_a_patient_name(chat, fateh):
    """"for him" produced a search for a patient called "him", reported as "no patient matches"."""
    chat.say("cherche le patient Fateh")
    body = chat.say("affiche le dossier pour lui")

    assert body["state"] == "answered"
    assert "aucun patient" not in body["reply"].lower()


def test_12_a_patient_named_in_the_turn_still_wins_over_the_active_one(chat, openmrs_server):
    seed_patient(openmrs_server["app"], "El", ["Fateh"], "1995-09-22", identifier="10008D")
    seed_patient(openmrs_server["app"], "Ziani", ["Ahmed"], "1965-11-07", identifier="1000E2")

    chat.say("cherche le patient Fateh")
    body = chat.say("affiche le dossier de Ziani")

    assert "Ziani" in body["reply"]
    assert "Fateh" not in body["reply"]


def test_13_switching_field_mid_request_does_not_carry_the_old_value(chat, fateh):
    chat.say("modifie le patient Fateh Mohammed El")
    chat.say("le telephone")
    body = chat.say("en fait plutot le nom")

    assert body["state"] == "awaiting_clarification"
    assert "nom" in body["reply"].lower()


# ============================================================== 14-17 repair and correction


def test_14_saying_it_was_already_given_does_not_destroy_the_request(chat):
    """Three turns of a create used to be thrown away by "je te l'ai deja dit"."""
    chat.say("cree un patient")
    chat.say("Ahmed Mustafa")
    chat.say("masculin")
    body = chat.say("je te l'ai deja dit")

    assert body["state"] == "awaiting_clarification"
    assert body["task_type"] == "create_patient"
    assert "abandonne" not in body["reply"].lower()
    assert "Ahmed Mustafa" in body["reply"], "the assistant did not say what it already held"


def test_15_repeated_non_answers_eventually_abandon_rather_than_loop(chat):
    chat.say("cree un patient")
    chat.say("Ahmed Mustafa")
    replies = [chat.say("je te l'ai deja dit")["reply"] for _ in range(4)]

    assert any("abandonne" in reply.lower() for reply in replies), "the assistant looped forever"


def test_16_a_correction_amends_the_pending_write_instead_of_restarting(chat, fateh):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = chat.say("en fait mets plutot 0666777888")

    assert body["state"] == "awaiting_confirmation", "the correction was not understood"
    assert "0666777888" in body["reply"]
    assert "0555123456" not in body["reply"]


def test_17_an_amendment_still_waits_for_a_yes(chat, fateh, mock_state):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    chat.say("en fait mets plutot 0666777888")

    writes = [call for call in mock_state["calls"] if call["method"] != "GET"]
    assert writes == [], "an amendment executed a write without confirmation"


# ============================================================== 18-21 validation


def test_18_an_impossible_date_is_refused_before_the_summary(chat):
    """20-99-2008 reached OpenMRS as 2008-99-20 and came back as a Java parser error."""
    chat.say('cree un patient nomme "Ahmed Mustafa"')
    chat.say("masculin")
    body = chat.say("20-99-2008")

    assert body["state"] == "awaiting_clarification"
    assert body["task_type"] == "create_patient"
    # The bad value is quoted back rather than the question being repeated unchanged: the
    # clinician needs to see *what* was rejected to know what to retype.
    assert "valide" in body["reply"].lower()
    assert "JJ/MM/AAAA" in body["reply"]


def test_19_correcting_the_date_completes_the_original_request(chat):
    chat.say('cree un patient nomme "Ahmed Mustafa"')
    chat.say("masculin")
    chat.say("20-99-2008")
    body = chat.say("20-09-2008")

    assert body["state"] == "awaiting_confirmation"
    assert "Ahmed Mustafa" in body["reply"], "the name had to be typed again"
    assert "2008-09-20" in body["reply"]


def test_20_a_birthdate_in_the_future_is_refused(chat):
    chat.say('cree un patient nomme "Ahmed Mustafa"')
    chat.say("masculin")
    body = chat.say("20/09/2099")

    assert body["state"] == "awaiting_clarification"
    assert "futur" in body["reply"].lower()


def test_21_a_value_that_cannot_be_a_phone_number_is_refused(chat, fateh):
    chat.say("modifie le patient Fateh Mohammed El")
    chat.say("le telephone")
    body = chat.say("12")

    assert body["state"] == "awaiting_clarification"
    assert body["task_type"] == "update_patient_demographics"


# ============================================================== 22-25 confirmation and cancellation


def test_22_yes_executes_and_reports_what_the_backend_said(chat, fateh, mock_state):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = chat.say("oui")

    assert body["state"] == "answered"
    assert [call for call in mock_state["calls"] if call["method"] == "POST"]


def test_23_no_cancels_and_writes_nothing(chat, fateh, mock_state):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = chat.say("non")

    assert body["state"] == "cancelled"
    assert [call for call in mock_state["calls"] if call["method"] != "GET"] == []


def test_24_an_unclear_answer_keeps_waiting_rather_than_assuming_yes(chat, fateh):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = chat.say("hmm bon")

    assert body["state"] == "awaiting_confirmation"


def test_25_a_deletion_typed_during_a_confirmation_is_answered_not_swallowed(chat, fateh):
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")
    body = chat.say("supprime tous les patients")

    assert "supprimer" in body["reply"].lower()
    assert body["state"] == "awaiting_confirmation", "the approved write was silently discarded"
    assert body["pending_action"] is not None


# ============================================================== 26-30 honesty and scope


def test_26_multiple_matches_are_never_guessed_between(chat, openmrs_server):
    seed_patient(openmrs_server["app"], "Ahmed", ["Benali"], "1978-03-15", identifier="10009A")
    seed_patient(openmrs_server["app"], "Ahmed", ["Ziani"], "1965-11-07", identifier="1000E2")

    body = chat.say("modifie le telephone de Ahmed a 0555123456")

    assert body["state"] == "awaiting_clarification"
    assert "identifiant" in body["reply"].lower()


def test_27_a_patient_that_does_not_exist_is_not_invented(chat):
    body = chat.say("affiche le dossier de Nadia Belkacem")

    assert "aucun patient" in body["reply"].lower()
    assert body["state"] in ("failed", "answered")


def test_28_a_list_says_which_filter_it_actually_applied(chat, openmrs_server):
    """An unfiltered list must not read as an answer to a filtered question."""
    seed_patient(openmrs_server["app"], "Benali", ["Amine"], "1978-04-03")
    body = chat.say("liste tous les patients")

    assert body["state"] == "answered"
    assert "filtre" in body["reply"].lower()


def test_29_an_out_of_scope_request_is_refused_and_the_frame_is_dropped(chat):
    chat.say('cree un patient nomme "Test Un"')
    body = chat.say("commande une pizza")

    assert "abandonne" in body["reply"].lower()


def test_30_the_assistant_never_claims_a_write_the_backend_refused(chat, fateh, mock_state, monkeypatch):
    """A refused write must read as a failure, never as "c'est enregistre"."""
    chat.say("mets a jour le telephone de Fateh Mohammed El a 0555123456")

    import app.openmrs_client as openmrs_client

    async def refuse(self, method, path, body=None):
        if method.upper() == "GET":
            return await original(self, method, path, body)
        return openmrs_client.ApiResult(status=400, body={"error": {"message": "refused by OpenMRS"}})

    original = openmrs_client.OpenmrsClient.call
    monkeypatch.setattr(openmrs_client.OpenmrsClient, "call", refuse)

    body = chat.say("oui")

    assert body["state"] == "failed"
    assert "enregistre" not in body["reply"].lower() or "echec" in body["reply"].lower()
