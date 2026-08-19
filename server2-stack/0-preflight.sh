#!/usr/bin/env bash
#
# Run this on SERVER 2 before "docker compose up -d".
#
# Every check here corresponds to a real way this deployment fails, and each one fails in a way
# that is hard to trace back from the symptom: the container reports only "unhealthy", or the
# chat says only "assistant indisponible". Checking up front turns all of them into one line of
# plain text.
#
#   ./0-preflight.sh
#
# It changes nothing. Safe to run as often as you like.

set -uo pipefail
cd "$(dirname "$0")"

PASS=0
FAIL=0

ok()   { printf '  [ OK ]  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL]  %s\n' "$1"; printf '          -> %s\n' "$2"; FAIL=$((FAIL+1)); }
warn() { printf '  [warn]  %s\n' "$1"; }

echo
echo "Preflight checks for the clinical assistant (Server 2)"
echo "======================================================"

# ---------------------------------------------------------------- 1. the settings file
echo
echo "1. Settings file"

if [ ! -f .env ]; then
  bad ".env is missing" "cp .env.example .env, then fill in the two secrets from OpenMRS"
else
  ok ".env exists"

  # A .env edited or pasted from Windows carries carriage returns. They become part of the value,
  # so the channel secret silently stops matching Server 1 and every chat turn returns 403 - with
  # both sides looking identical on screen.
  if grep -q $'\r' .env; then
    bad ".env contains Windows line endings (CR)" "run: sed -i 's/\r\$//' .env"
  else
    ok ".env has Unix line endings"
  fi

  # shellcheck disable=SC1091
  set -a; . ./.env 2>/dev/null; set +a

  for var in AGENT_SERVER_NAME OPENMRS_SERVER_CIDR OPENMRS_BASE_URL AGENT_CHANNEL_SECRET OPENMRS_JWT_PUBLIC_KEY; do
    value="${!var:-}"
    if [ -z "$value" ]; then
      bad "$var is empty" "fill it in in .env (the two secrets come from OpenMRS: Administration -> Settings -> Agentgateway)"
    else
      ok "$var is set"
    fi
  done

  # The public key box in OpenMRS wraps the text to fit. That wrapping is not part of the key.
  if [ -n "${OPENMRS_JWT_PUBLIC_KEY:-}" ]; then
    if printf '%s' "$OPENMRS_JWT_PUBLIC_KEY" | grep -qE '[[:space:]]'; then
      bad "OPENMRS_JWT_PUBLIC_KEY contains spaces or line breaks" \
          "paste it as ONE single line - the wrapping you see in OpenMRS is only the text box"
    elif printf -- "-----BEGIN PUBLIC KEY-----\n%s\n-----END PUBLIC KEY-----\n" "$OPENMRS_JWT_PUBLIC_KEY" \
         | openssl pkey -pubin -noout >/dev/null 2>&1; then
      ok "OPENMRS_JWT_PUBLIC_KEY is a valid RSA public key"
    else
      bad "OPENMRS_JWT_PUBLIC_KEY is not a readable public key" \
          "re-copy the Signing PUBLIC Key field from OpenMRS (not the private one)"
    fi
  fi
fi

# ---------------------------------------------------------------- 2. certificates
echo
echo "2. Certificates"

for f in certs/agent.crt certs/agent.key certs/hospitalCA.crt; do
  if [ -d "$f" ]; then
    # The single-file bind mount trap: Docker created a directory where a file was expected.
    bad "$f is a DIRECTORY, not a file" \
        "run: mv $f/* /tmp/ 2>/dev/null; rmdir $f; then copy the real file into certs/ again"
  elif [ ! -f "$f" ]; then
    bad "$f is missing" "see steps B3-B7 of DEPLOYMENT-GUIDE.md"
  else
    ok "$f exists"
  fi
done

if [ -f certs/agent.crt ] && [ -f certs/hospitalCA.crt ]; then
  if openssl verify -CAfile certs/hospitalCA.crt certs/agent.crt >/dev/null 2>&1; then
    ok "agent.crt was signed by hospitalCA.crt"
  else
    bad "agent.crt does not verify against hospitalCA.crt" \
        "you may have copied the wrong file - redo step B5 on Server 1"
  fi
fi

if [ -f certs/agent.crt ] && [ -f certs/agent.key ]; then
  # Mismatched key and certificate is what happens when step B3 is run twice: the second run
  # makes a new key, and the certificate already signed belongs to the first one.
  crt_mod=$(openssl x509 -noout -modulus -in certs/agent.crt 2>/dev/null | openssl md5)
  key_mod=$(openssl rsa -noout -modulus -in certs/agent.key 2>/dev/null | openssl md5)
  if [ -n "$crt_mod" ] && [ "$crt_mod" = "$key_mod" ]; then
    ok "agent.key matches agent.crt"
  else
    bad "agent.key does NOT match agent.crt" \
        "delete certs/agent.* and redo steps B3-B5 without skipping any"
  fi
fi

if [ -f certs/agent.crt ] && [ -n "${AGENT_SERVER_NAME:-}" ]; then
  if openssl x509 -in certs/agent.crt -noout -text 2>/dev/null | grep -q "DNS:$AGENT_SERVER_NAME"; then
    ok "agent.crt covers $AGENT_SERVER_NAME"
  else
    bad "agent.crt does not list $AGENT_SERVER_NAME" \
        "the name in .env and the name in the certificate must match exactly"
  fi
fi

# ---------------------------------------------------------------- 3. the network
echo
echo "3. Network"

if [ -n "${OPENMRS_BASE_URL:-}" ]; then
  host=$(printf '%s' "$OPENMRS_BASE_URL" | sed -E 's#^https?://##; s#[:/].*$##')
  if getent hosts "$host" >/dev/null 2>&1; then
    ok "$host resolves to $(getent hosts "$host" | awk '{print $1}' | head -1)"
  else
    bad "$host does not resolve on this machine" \
        "add it to /etc/hosts:  echo '10.0.211.249  $host' | sudo tee -a /etc/hosts"
  fi
fi

# ---------------------------------------------------------------- 4. docker
echo
echo "4. Docker"

if docker info >/dev/null 2>&1; then
  ok "docker is usable without sudo"
elif sudo -n docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
  warn "docker needs sudo for this user"
  echo "          -> works, but every command needs sudo. To fix it permanently:"
  echo "                sudo usermod -aG docker \$USER"
  echo "             then log out and back in (the group is only applied at login)."
else
  bad "cannot reach the docker daemon" "is docker running?  sudo systemctl status docker"
fi

# ---------------------------------------------------------------- verdict
echo
echo "======================================================"
if [ "$FAIL" -eq 0 ]; then
  echo "  $PASS checks passed. You are clear to run:  docker compose up -d"
else
  echo "  $FAIL problem(s) found. Fix the [FAIL] lines above, then run this again."
fi
echo
exit $(( FAIL > 0 ? 1 : 0 ))
