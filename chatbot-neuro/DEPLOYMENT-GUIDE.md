# Deployment guide — step by step

Written to be followed exactly, in order, with no prior knowledge assumed.

Every step says **which computer** to be on. That is the single easiest thing to get wrong, so it
is repeated every time.

## The two computers

| Nickname | Address | What it is |
|---|---|---|
| **SERVER 1** | `10.0.211.249` | The hospital's main server. OpenMRS, Orthanc, the viewer, and the folder with `hospitalCA.crt` and `hospitalCA.key` in it. |
| **SERVER 2** | `10.0.211.250` | The GPU machine. The chatbot lives here. |

## What we are building

The doctor types a question in OpenMRS. OpenMRS passes it to the chatbot on SERVER 2. The chatbot
works out what to do and asks OpenMRS to do it. OpenMRS checks the doctor is allowed, does it, and
writes down what happened.

The doctor's browser never talks to SERVER 2. Only SERVER 1 is allowed to.

## Two words you will see a lot

**Certificate** — an ID card for a computer. It proves `agent.hospital.lan` really is SERVER 2 and
not somebody pretending. **hospitalCA** is the thing that signs those ID cards. Your SERVER 1
already has it, and it already signed the cards for `openmrs`, `orthanc` and `viewer`. We are
going to get a fourth card signed, for the chatbot.

**Trust store** — the list of signatures a program is willing to believe. Java keeps its **own**
list, separate from the rest of the computer. This trips up almost everyone. There is a whole
step below just for it.

---

# PART A — SERVER 1: install the OpenMRS module

### A1. Build the module

On any machine with Java 8 and Maven:

```bash
cd chatbot-neuro/openmrs-module-agentgateway
mvn clean package
```

**You should see:** `BUILD SUCCESS`, and a new file at
`omod/target/agentgateway-1.1.0.omod`.

### A2. Install it into OpenMRS

Open OpenMRS in a browser. Go to **Administration → Manage Modules → Add or Upgrade Module**.
Upload `agentgateway-1.1.0.omod`.

> If you already installed version 1.0.0, upload 1.1.0 the same way. It replaces the old one.
> **You must do this** — 1.0.0 has the bug that hides the chat window.

**You should see:** `agentgateway` in the module list, marked **Started**.

**If it is not started:** click on it and read the error. Almost always it is a missing module.
This one needs `uiframework`, `appui` and `coreapps`, all of which come with the Reference
Application.

### A3. Give people permission to use it

Go to **Administration → Manage Roles**. Add these privileges:

| Privilege | Give it to |
|---|---|
| `App: agentgateway.chat.use` | Surgeon, OR Nurse, Radiologist, Admissions |
| `App: agentgateway.chat.write` | Surgeon, Admissions (the people already allowed to create patients by hand) |
| `App: agentgateway.rollback` | System Administrator only |

**This is not optional.** The chat window is hidden from anyone without `chat.use`. If you skip
this step you will look at a patient page and see nothing, and think the module is broken.

### A4. Copy the two secrets — you need them later

Go to **Administration → Settings → Agentgateway** (the page in your screenshot).

Copy these two values into a text file. You will paste them on SERVER 2 in step B2:

1. **Channel Secret** — a longish random string.
2. **Signing Public Key** — a very long string. The box shows it on several lines because it is
   too long to fit. **It is really one single line.** When you paste it later, it must be one
   line with no spaces and no line breaks.

⚠️ **Never copy "Signing Private Key".** That one stays in OpenMRS forever. Anyone who has it can
pretend to be any doctor in the hospital.

> **This exact mistake has already happened once here.** The private key was pasted into
> `OPENMRS_JWT_PUBLIC_KEY` on Server 2, and the chat then failed on *every* message with a generic
> error that said nothing about keys. How to tell them apart before you paste:
>
> | | Length | Starts with |
> |---|---|---|
> | **Public** — the one you want | ~390 characters | `MIIBIjANBg` |
> | **Private** — never copy this | ~1600 characters | `MIIEvAIBAD` |
>
> The agent service now refuses to start if given the wrong one, and says which field to copy. See
> `IMPLEMENTATION-LOG.md` Finding 2.

### A5. Tell OpenMRS where the chatbot is

Same settings page:

| Setting | Type this |
|---|---|
| Agent Service Url | `https://agent.hospital.lan` |
| Agent Timeout Millis | `30000` |
| Self Base Url | `http://localhost:8080/openmrs` |

Click **Save**.

> Notice there is no `:8000` in the address. Port 8000 is inside SERVER 2 and never comes out.
> `https://` always means port 443 unless you write something else.

> `Self Base Url` uses plain `http://` on purpose. It is OpenMRS talking to itself inside the same
> machine — that traffic never touches the network, so there is nothing to encrypt.

### A5b. Collect four values needed to create patients

Searching and reading work without these. **Creating** a patient does not. None of them can be
guessed, and each was found by failing against the live instance — with an error message that named
the wrong thing every time (Findings 8, 9 and 10).

**Two you can read off a screen:**

1. **Administration → Manage Identifier Types** — the name of the type used for patients. Here:
   `OpenMRS ID` (described as *"with check-digit"*, which is why the assistant cannot invent an
   identifier and has to reserve one).
2. **Administration → Manage Patient Identifier Sources** — confirm a source exists for that type.
   Here: *Generator for OpenMRS ID*.

**Two that are not shown anywhere in the interface.** Open these two addresses in the browser where
you are logged in as an administrator:

```
https://openmrs.hospital.lan/openmrs/ws/rest/v1/idgen/identifiersource?limit=50
https://openmrs.hospital.lan/openmrs/ws/rest/v1/location?limit=50
```

**Both will show a red HTTP 500 error page. That is expected on this deployment** — its XML
converter is broken, and a browser asks for XML. **The values you need are inside the error text**:
look for `uuid=...` next to the name you want. You need the identifier source's uuid, and the uuid of
the location where identifiers should be recorded as assigned (here: `Registration Desk`).

Write all four down for step B2:

| | Value here |
|---|---|
| Identifier type name | `OpenMRS ID` |
| Identifier type uuid | `05a29f94-c0ed-11e2-94be-8c13b969e334` |
| Identifier source uuid | `691eed12-c0f1-11e2-94be-8c13b969e334` |
| Identifier location uuid | `6351fcf4-e311-4a19-90f9-35667d99a8af` |

### A6. Let SERVER 1 find SERVER 2 by name

On **SERVER 1**, in a terminal:

```bash
echo "10.0.211.250  agent.hospital.lan" | sudo tee -a /etc/hosts
```

If OpenMRS runs in Docker, the container needs it too. Check:

```bash
docker exec openmrs-app getent hosts agent.hospital.lan
```

**You should see:** `10.0.211.250  agent.hospital.lan`.

**If you see nothing**, add this to the OpenMRS service in its `docker-compose.yml` and restart it:

```yaml
extra_hosts:
  - "agent.hospital.lan:10.0.211.250"
```

---

# PART B — SERVER 2: the chatbot

### B1. Copy the project over

Put the `chatbot-neuro` and `server2-stack` folders on SERVER 2, next to each other.

### B2. Fill in the settings file

On **SERVER 2**:

```bash
cd server2-stack
cp .env.example .env
nano .env
```

Everything is already filled in for your hospital. You add the two values from step A4:

```
AGENT_CHANNEL_SECRET=<paste the Channel Secret here>
OPENMRS_JWT_PUBLIC_KEY=<paste the Signing Public Key here, ALL ON ONE LINE>
```

and the four from step A5b, without which searching works but **creating a patient fails**:

```
OPENMRS_PATIENT_IDENTIFIER_TYPE=OpenMRS ID
OPENMRS_PATIENT_IDENTIFIER_TYPE_UUID=<identifier type uuid>
OPENMRS_IDGEN_SOURCE_UUID=<identifier source uuid>
OPENMRS_IDENTIFIER_LOCATION_UUID=<location uuid>
```

Save and close (`Ctrl+O`, `Enter`, `Ctrl+X`).

> Check the public key before you move on: about 390 characters, starting `MIIBIjANBg`. If it is
> ~1600 characters and starts `MIIEvAIBAD`, that is the **private** key — go back to A4. The service
> will refuse to start and tell you so, but it is quicker to catch here.

### B3. Ask for an ID card for the chatbot

On **SERVER 2**:

```bash
./1-make-agent-csr.sh
```

**You should see:** `Requested names: DNS:agent.hospital.lan, DNS:localhost, IP:10.0.211.250, IP:127.0.0.1`

This makes two files. `agent.key` is the secret half and never leaves SERVER 2. `agent.csr` is the
request — it contains nothing secret, so it is safe to copy.

### B4. Send the request to SERVER 1

On **SERVER 2**:

```bash
scp certs/agent.csr certs/agent-san.cnf 2-sign-agent-csr.sh user@10.0.211.249:~/certificates/
```

Change `~/certificates/` to wherever `hospitalCA.crt` and `hospitalCA.key` actually live on
SERVER 1.

### B5. Sign it — on SERVER 1

Now move to **SERVER 1**:

```bash
cd ~/certificates
./2-sign-agent-csr.sh
```

**You should see:**

```
issuer=C=DZ, O=CHU Blida, CN=hospitalCA
SAN: DNS:agent.hospital.lan, DNS:localhost, IP Address:10.0.211.250, IP Address:127.0.0.1
Chain check: OK
```

`Chain check: OK` is the important line. If it says FAILED, stop and check you are in the folder
with the real `hospitalCA.key`.

### B6. The step everybody forgets — teach Java to trust hospitalCA

Still on **SERVER 1**:

```bash
docker cp hospitalCA.crt openmrs-app:/tmp/hospitalCA.crt
docker exec -u 0 openmrs-app keytool -importcert -noprompt -alias hospital-ca -file /tmp/hospitalCA.crt -keystore $JAVA_HOME/jre/lib/security/cacerts -storepass changeit
docker restart openmrs-app
```

Check it worked:

```bash
docker exec openmrs-app keytool -list -alias hospital-ca -keystore $JAVA_HOME/jre/lib/security/cacerts -storepass changeit
```

**You should see:** a line containing `hospital-ca` and `trustedCertEntry`.

> **Why this matters.** OpenMRS is a Java program, and Java keeps its own private list of
> signatures it trusts. Your hospital's CA is not on it. Without this step, OpenMRS refuses to
> talk to `https://agent.hospital.lan`, the chat says "assistant indisponible", and the real
> reason is hidden in the Tomcat log. Nothing else in the setup will tell you.

> `changeit` is genuinely the password. It is Java's default and it is the same everywhere.

> If you ever rebuild the OpenMRS container, this is erased and must be done again.

### B7. Bring the ID card back and start the chatbot

Back on **SERVER 2**:

```bash
scp user@10.0.211.249:~/certificates/agent.crt certs/
scp user@10.0.211.249:~/certificates/hospitalCA.crt certs/
docker compose up -d
```

**You should see:** two containers starting, `server2-proxy` and `clinical-agent`.

Check with:

```bash
docker compose ps
```

**You should see:** both `running`, and `clinical-agent` marked `healthy`.

---

# PART C — Checks

Do these in order. The first one that fails tells you where the problem is, so do not skip ahead.

### ✅ Check 1 — the chatbot is alive (on SERVER 2)

```bash
curl --cacert certs/hospitalCA.crt https://agent.hospital.lan/health
```

**Pass:** `{"status":"ok","openmrs_base_url":"https://openmrs.hospital.lan/openmrs","fhir_capabilities_known":true,...}`

| If you see | It means | Do this |
|---|---|---|
| `Connection refused` | the containers are not running | `docker compose ps`, then `docker compose logs` |
| a certificate error | the name in the ID card does not match | redo B3–B5 |
| `"fhir_capabilities_known": false` | the chatbot cannot reach OpenMRS | check the next line |

### ✅ Check 2 — the chatbot can reach OpenMRS (on SERVER 2)

Only if check 1 said `false`:

```bash
docker compose logs clinical-agent | grep -i capabilit
```

| If you see | Do this |
|---|---|
| `Name or service not known` | add `openmrs.hospital.lan` to SERVER 2's `/etc/hosts` |
| `certificate verify failed` | `certs/hospitalCA.crt` is missing or wrong — copy it again from SERVER 1 |

### ✅ Check 3 — SERVER 1 can reach the chatbot (on SERVER 1)

```bash
curl --cacert ~/certificates/hospitalCA.crt https://agent.hospital.lan/health
```

**Pass:** the same `{"status":"ok",...}`.

**Fail:** check A6 (the name) and that nothing is blocking port 443 between the two machines.

### ✅ Check 4 — nobody else can reach it (from any other PC on the network)

```bash
curl -k -o /dev/null -w '%{http_code}\n' https://agent.hospital.lan/health
```

**Pass: `403`.** This is a *good* failure. Only SERVER 1 is allowed in.

**If you get `200`, stop and fix it.** Check `OPENMRS_SERVER_CIDR=10.0.211.249/32` in `.env`, then
`docker compose up -d`.

### ✅ Check 5 — the plain IP address gives nothing (from any other PC)

```bash
curl -k https://10.0.211.250/health
```

**Pass:** `Empty reply from server`. The chatbot only answers to its proper name.

### ✅ Check 6 — what the chatbot can actually do here (on SERVER 1)

```bash
curl --cacert ~/certificates/hospitalCA.crt -H "X-Agent-Channel-Key: PASTE_YOUR_CHANNEL_SECRET" https://agent.hospital.lan/capabilities
```

**Pass:** a list of tools, each with `"available": true` or `false` **and a reason**.

Expected on a normal install:

| Tool | Expected | Why |
|---|---|---|
| `search_patient` | `true` | |
| `get_patient_summary` | `true` | |
| `create_patient` | `true` | |
| `update_patient_demographics` | `true` | |
| `book_appointment` | maybe `false` | only if your `fhir2` version does not offer Appointment |
| `record_neuro_assessment` | `false` | expected — see "known limitation" at the end |

**If everything says false**, the chatbot could not read OpenMRS's capability list. Go back to
check 2.

### ✅ Check 7 — the chat window is visible (in a browser)

Log into OpenMRS as a user who has `App: agentgateway.chat.use`, and open **any patient**.

**Pass:** an **"ASSISTANT CLINIQUE"** box in the right-hand column of the patient dashboard, with
a message area and a text box.

You should also see:
- **"Assistant clinique"** in the patient's action links,
- **"Assistant clinique"** on the OpenMRS home page.

**If you see nothing:**

| Check | How |
|---|---|
| Is the module version 1.1.0? | Administration → Manage Modules. **1.0.0 cannot show the chat** — that was the bug. |
| Does your user have the privilege? | Administration → Manage Roles → your role → `App: agentgateway.chat.use` |
| Does the page itself work? | Open `https://openmrs.hospital.lan/openmrs/agentgateway/chat.page` directly. If this works but the box does not appear, it is the privilege or the module version. |

> **Note about the neurosurgery dashboard.** The box appears on the *standard* OpenMRS patient
> page. The `patientview` neurosurgery dashboard is a different page, and it does not pick the box
> up automatically. To put the assistant there too, add this one line to `patientview`'s
> `patient.gsp` and rebuild that module:
>
> ```groovy
> ${ ui.includeFragment("agentgateway", "chatWidget") }
> ```
>
> It needs no other change — the box finds the patient from the address bar, and `patientview`'s
> pages already put it there. It is not done for you because it edits a different module.

### ✅ Check 8 — a real conversation (in the browser)

In the chat box on a patient's page, type:

```
affiche le dossier
```

**Pass:** the assistant replies with that patient's name, sex and date of birth.

Now type something that writes:

```
mets a jour le telephone du patient, tel 0555 12 34 56
```

**Pass:** the assistant does **not** save. It shows a summary and asks
*"Confirmez-vous la modification ?"*

Type `oui`.

**Pass:** *"C'est enregistré."*

**This two-step behaviour is the most important thing to verify.** The assistant must never write
anything without you saying yes first.

### ✅ Check 9 — it was written down (in the browser, as an admin)

As a user with `App: agentgateway.rollback`, open:

```
https://openmrs.hospital.lan/openmrs/agentgateway/operationLog.page
```

**Pass:** a table with one row per action, showing who did it, what was called, and whether it can
be undone. Click **Detail** on the update you just made — you should see the phone number before
and after.

### ✅ Check 10 — a read-only user cannot write

Log in as someone with `chat.use` but **not** `chat.write`. Try the same phone number change.

**Pass:** *"Votre compte permet uniquement les consultations."* — and nothing is saved.

---

## Everything green?

Then the chain works: browser → OpenMRS → chatbot → OpenMRS → database, with permissions checked
at every step and every action written down.

## Known limitations, so they do not surprise you

**Booking an appointment does not work**, and it is not a matter of switching it on. `fhir2` on this
installation exposes no `Appointment` resource at all, and the `appointmentscheduling` module models
booking as putting a patient into a **time slot that an administrator created in advance** — there is
no "book at this date and time" operation. Two questions have to be settled before it can be built:
whether provider schedules are configured here at all, and whether "book an appointment" in the brief
means this or something closer to a request or referral. See `IMPLEMENTATION-LOG.md` Findings 6 and 11.

**`null null` appears at the top of every page**, including the assistant's. That is not the
assistant: it is OpenMRS's own page header printing the logged-in user's first and last name, and the
`admin` account has neither on its person record. Give that account a name and it goes away. The same
missing name is why the assistant's audit log needed a fallback to display who acted (Finding 12).

`record_neuro_assessment` (Glasgow / Karnofsky) reports itself as **unavailable**, and that is
correct, not a bug. The existing `patientview` endpoints identify a patient by an internal
**number** that the OpenMRS web API does not hand out — so nothing outside OpenMRS can address a
patient there. `patientview` first needs to expose its data at `/ws/rest/v1/patientview/...` keyed
on UUID (architecture section 4.3). When that exists, two things switch it on, and you need
**both**:

1. `PATIENTVIEW_TOOLS_ENABLED=true` in `.env` on SERVER 2
2. add `/ws/rest/v1/patientview/` to **Audited Path Prefixes** in the OpenMRS settings

## Which interpreter is running

The assistant reads the clinician's sentence with one of two engines, chosen by `NLU_ENGINE` in
`.env` on SERVER 2:

| Value | What it means |
|---|---|
| `rules` (default) | A deterministic French interpreter. No GPU needed. It understands the phrasings it has patterns for and asks a clarifying question otherwise. |
| `medgemma` | The local MedGemma model served by vLLM. Understands far more phrasing. Falls back to `rules` automatically whenever the model is unreachable or answers unusably. |

Starting the model needs the overlay as well as the base stack:

```bash
cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml up -d
```

To go back to the deterministic engine — the first thing to try if a turn is read oddly, because it
tells you in one restart whether the model or the plumbing is at fault:

```bash
sed -i 's/^NLU_ENGINE=.*/NLU_ENGINE=rules/' ~/server2-stack/.env && cd ~/server2-stack && docker compose up -d clinical-agent
```

Which engine is live is stated in the agent's log at startup:

```bash
docker compose logs clinical-agent | grep Interpretation
```

## If you get stuck

| Where to look | Command |
|---|---|
| Chatbot log | `docker compose logs -f clinical-agent` (SERVER 2) |
| Proxy log | `docker compose logs -f nginx` (SERVER 2) |
| OpenMRS log | `docker logs -f openmrs-app` (SERVER 1) |
| Was it a permission problem? | the OpenMRS log prints `Requires privilege: …` |
| Was it a certificate problem? | the OpenMRS log prints `SSLHandshakeException` → redo step B6 |
