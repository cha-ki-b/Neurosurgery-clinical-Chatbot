"""Patient data must not reach this server's logs.

``LOG_PROMPTS`` was believed to control this. It controlled two log lines out of fifteen; the
other thirteen wrote the clinician's sentence, or a name, phone number or date of birth pulled out
of it, at INFO level whatever the flag said. This service holds no data and keeps no record - its
logs are for diagnosing behaviour, and the authoritative copy of every prompt already lives in
``agentgateway_operation_log`` on the OpenMRS side, under the hospital's own access control.

The test drives the conversations that historically produced those lines: a dropped slot, an
abandoned frame, a task switch, a rejected value.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CHANNEL_SECRET
from tests.mock_openmrs import seed_patient

# Deliberately distinctive so a substring check cannot pass by accident.
PATIENT_NAME = "Zoubir Belkacemi"
PHONE = "0798451236"
BIRTHDATE = "14/02/1961"


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def say(client, mint, prompt, cid="phi"):
    return client.post(
        "/chat",
        json={"conversation_id": cid, "prompt": prompt,
              "delegated_token": mint(username="dr.benali", may_write=True, conversation_id=cid),
              "context": {"locale": "fr"}},
        headers={"X-Agent-Channel-Key": CHANNEL_SECRET},
    ).json()


def _leaks(caplog, *secrets):
    text = "\n".join(record.getMessage() for record in caplog.records)
    return [secret for secret in secrets if secret in text]


def test_no_patient_data_in_the_logs_across_a_whole_conversation(client, mint, caplog, openmrs_server):
    seed_patient(openmrs_server["app"], "Belkacemi", ["Zoubir"], "1961-02-14", identifier="1000ZZ")
    caplog.set_level(logging.DEBUG)

    say(client, mint, f'cree un patient nomme "{PATIENT_NAME}"')      # slot extraction
    say(client, mint, "masculin")
    say(client, mint, "32-45-1961")                                    # a rejected value
    say(client, mint, BIRTHDATE)
    say(client, mint, "non")
    say(client, mint, f"mets a jour le telephone de {PATIENT_NAME} a {PHONE}")   # a write plan
    say(client, mint, "commande une pizza")                            # an abandoned frame

    leaked = _leaks(caplog, PATIENT_NAME, "Zoubir", "Belkacemi", PHONE, "1961-02-14", "32-45-1961")
    assert not leaked, f"patient data reached the logs: {leaked}"


def test_the_logs_still_say_enough_to_diagnose_a_turn(client, mint, caplog):
    """Redaction that removes the diagnosis too would just move the problem."""
    caplog.set_level(logging.INFO)

    say(client, mint, 'cree un patient nomme "Zoubir Belkacemi"', cid="phi2")
    say(client, mint, "commande une pizza", cid="phi2")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "create_patient" in text, "the task is not recoverable from the logs"
    assert "Abandoning" in text, "the abandoned frame is not visible in the logs"


def test_turning_prompt_logging_on_is_announced_as_a_phi_decision(monkeypatch, caplog):
    """An operator switching it on must be told what it means, at the moment they do it."""
    from app.config import settings

    monkeypatch.setattr(settings, "log_prompts", True)
    caplog.set_level(logging.WARNING)

    from app.main import app

    with TestClient(app):
        pass

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "LOG_PROMPTS is ON" in text
    assert "patient data" in text


def test_prompt_logging_when_on_really_does_show_the_prompt(client, mint, monkeypatch, caplog):
    """The debugging switch must still work, or someone will add a worse one."""
    from app.config import settings

    monkeypatch.setattr(settings, "log_prompts", True)
    caplog.set_level(logging.INFO)

    say(client, mint, "commande une pizza", cid="phi3")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "pizza" in text


def test_a_refused_search_does_not_log_the_name_it_searched_for(client, mint, caplog, monkeypatch):
    """The branch the mock could never reach, and where the leak actually lived.

    ``OpenmrsClient.call`` logged the request path verbatim on every non-2xx and every transport
    error. A patient search path *is* patient data - ``Patient?name=Zoubir Belkacemi`` - and those
    are not rare branches: a 403 for a clinician without the privilege, a 404 while the relay
    filter is misconfigured, or a slow Server 1 all reach them. Every existing test missed it
    because the mock OpenMRS answers every seeded search with 200.
    """
    import app.openmrs_client as openmrs_client

    # The real ``call`` must run - that is where the leak was. Only the server's answer is faked.
    original_request = openmrs_client.httpx.AsyncClient.request

    async def refusing_request(self, method, url, **kwargs):
        response = await original_request(self, method, url, **kwargs)
        response.status_code = 403
        return response

    monkeypatch.setattr(openmrs_client.httpx.AsyncClient, "request", refusing_request)
    caplog.set_level(logging.DEBUG)

    say(client, mint, f"cherche le patient {PATIENT_NAME}", cid="refused")

    leaked = _leaks(caplog, PATIENT_NAME, "Zoubir", "Belkacemi")
    assert not leaked, f"a refused search logged the name it searched for: {leaked}"


def test_an_unreachable_openmrs_does_not_log_the_name_it_searched_for(client, mint, caplog, monkeypatch):
    import app.openmrs_client as openmrs_client

    async def always_timeout(self, method, url, **kwargs):
        raise openmrs_client.httpx.ReadTimeout("too slow")

    monkeypatch.setattr(openmrs_client.httpx.AsyncClient, "request", always_timeout)
    caplog.set_level(logging.DEBUG)

    say(client, mint, f"cherche le patient {PATIENT_NAME}", cid="timeout")

    leaked = _leaks(caplog, PATIENT_NAME, "Zoubir", "Belkacemi")
    assert not leaked, f"an unreachable OpenMRS logged the name it searched for: {leaked}"


def test_the_redacted_path_still_says_which_endpoint_failed(client, mint, caplog, monkeypatch):
    """Redaction that removes the diagnosis too would just move the problem."""
    import app.openmrs_client as openmrs_client

    original_request = openmrs_client.httpx.AsyncClient.request

    async def refusing_request(self, method, url, **kwargs):
        response = await original_request(self, method, url, **kwargs)
        response.status_code = 403
        return response

    monkeypatch.setattr(openmrs_client.httpx.AsyncClient, "request", refusing_request)
    caplog.set_level(logging.INFO)

    say(client, mint, f"cherche le patient {PATIENT_NAME}", cid="refused2")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "/ws/fhir2/R4/Patient" in text, "the endpoint is no longer recoverable from the logs"
    assert "name=<redacted>" in text, "the parameter name was lost along with its value"


# --------------------------------------------------------------------------- the structural guard

# Every log argument below was read and judged not to be patient data: task and slot *names*,
# configuration, counts, status codes, conversation ids, exception types. Two of the entries are
# raw prompts and one is a raw model response - all three sit inside `if settings.log_prompts:`,
# which is the supervised-debugging path and is tested above.
#
# This allowlist is the point of the test. A line-based grep is what audited these calls the first
# time, and it silently skipped a `log.info(` whose arguments were on the next line - which is
# exactly how a raw prompt survived the redaction pass. An AST walk cannot miss that.
REVIEWED_SAFE = {
    "', '.join(sorted(self._capabilities.resources)) or '(none)'",
    "settings.openmrs_base_url", "settings.llm_base_url", "settings.nlu_engine",
    "'enabled' if settings.patientview_tools_enabled else 'disabled'",
    "conversation_id", "user.username", "len(payload.prompt)",
    "result.state", "result.task_type", "elapsed_ms",
    "request.client.host if request.client else '?'",
    "exc", "type(exc).__name__", "budget", "key", "task",
    "deterministic.task", "frame.task", "interpretation.task", "tool.task",
    "problem.slot", "sorted(set(filled))", "pending.task_type", "sorted(amendment)",
    "response.status_code", "method", "operation.method",
    "choice.get('finish_reason')", "len(content or '')",
    # Guarded by `if settings.log_prompts:` - the supervised path.
    "payload.prompt", "body['messages'][-1]['content']", "content",
}

PHI_WRAPPERS = {"safe", "safe_slots", "safe_path"}


def _log_call_arguments():
    """Every argument passed to a log call anywhere in app/, with its source text."""
    import ast
    import pathlib

    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in {"info", "warning", "error", "debug", "exception", "critical"}
                    and isinstance(func.value, ast.Name) and func.value.id == "log"):
                continue
            for argument in node.args[1:]:      # args[0] is the format string
                yield path, node.lineno, argument


def test_every_log_argument_is_redacted_or_reviewed():
    import ast

    unreviewed = []
    for path, line, argument in _log_call_arguments():
        if isinstance(argument, ast.Constant):
            continue
        if (isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name)
                and argument.func.id in PHI_WRAPPERS):
            continue
        source = ast.unparse(argument)
        if source not in REVIEWED_SAFE:
            unreviewed.append(f"{path}:{line} -> {source}")

    assert not unreviewed, (
        "New log arguments that are neither redacted nor on the reviewed-safe list:\n  "
        + "\n  ".join(unreviewed)
        + "\n\nWrap them in phi.safe()/safe_path()/safe_slots(), or add them to REVIEWED_SAFE "
          "after checking they cannot carry patient data."
    )
