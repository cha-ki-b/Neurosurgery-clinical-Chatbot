#!/usr/bin/env bash
#
# STEP 1 of 2 — run this on SERVER 2 (10.0.211.250, the GPU machine).
#
# Creates the private key for agent.hospital.lan and a certificate signing request (CSR) to be
# signed by the certificate authority Server 1 already has (hospitalCA).
#
# Why not just make another self-signed certificate here: the hospital would then have two
# authorities to install and keep track of, and every machine that talks to both servers would
# need both. You already have hospitalCA signing openmrs / orthanc / viewer .hospital.lan; the
# assistant should be a fourth certificate from the same authority, not a parallel universe.
#
# Note what does NOT happen here: the CA's private key is never copied to this machine. Only the
# CSR travels, and a CSR is public information - it contains the public key and the name being
# requested, nothing secret. The signing happens on Server 1 in step 2.

set -euo pipefail

cd "$(dirname "$0")"

CERT_DIR="certs"
AGENT_HOSTNAME="${AGENT_SERVER_NAME:-agent.hospital.lan}"
AGENT_IP="${AGENT_SERVER_IP:-10.0.211.250}"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  AGENT_HOSTNAME="${AGENT_SERVER_NAME:-$AGENT_HOSTNAME}"
  AGENT_IP="${AGENT_SERVER_IP:-$AGENT_IP}"
fi

mkdir -p "$CERT_DIR"

if [ -f "$CERT_DIR/agent.key" ]; then
  echo "A key already exists at $CERT_DIR/agent.key."
  echo "Delete it first if you really want a new one - reusing it keeps the existing certificate valid."
  exit 1
fi

# The subject alternative names are what clients actually check. The IP is included so that a
# quick test with https://10.0.211.250 does not produce a confusing name mismatch on top of
# whatever is actually being debugged.
cat > "$CERT_DIR/agent-san.cnf" <<CNF
[req]
distinguished_name = dn
req_extensions     = v3_req
prompt             = no

[dn]
C  = DZ
O  = CHU Blida
OU = Neurochirurgie
CN = $AGENT_HOSTNAME

[v3_req]
basicConstraints = CA:FALSE
keyUsage         = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = DNS:$AGENT_HOSTNAME,DNS:localhost,IP:$AGENT_IP,IP:127.0.0.1
CNF

openssl req -nodes -newkey rsa:2048 -sha256 \
  -config "$CERT_DIR/agent-san.cnf" \
  -keyout "$CERT_DIR/agent.key" \
  -out    "$CERT_DIR/agent.csr" 2>/dev/null

chmod 600 "$CERT_DIR/agent.key"

echo "Done."
echo
echo "  $CERT_DIR/agent.key   private key  - stays on this machine, never copy it anywhere"
echo "  $CERT_DIR/agent.csr   request      - copy this to Server 1"
echo "  $CERT_DIR/agent-san.cnf            - kept so step 2 can reuse the exact same names"
echo
echo "Requested names: DNS:$AGENT_HOSTNAME, DNS:localhost, IP:$AGENT_IP, IP:127.0.0.1"
echo
echo "-----------------------------------------------------------------------"
echo "NEXT — copy the request and the extension file to Server 1 and sign there:"
echo
echo "  scp $CERT_DIR/agent.csr $CERT_DIR/agent-san.cnf 2-sign-agent-csr.sh \\"
echo "      user@10.0.211.249:/path/to/certificates/"
echo
echo "  # then, on Server 1, in the folder holding hospitalCA.crt and hospitalCA.key:"
echo "  ./2-sign-agent-csr.sh"
echo
echo "  # and copy the two results back here:"
echo "  scp user@10.0.211.249:/path/to/certificates/agent.crt      $CERT_DIR/"
echo "  scp user@10.0.211.249:/path/to/certificates/hospitalCA.crt $CERT_DIR/"
echo "-----------------------------------------------------------------------"
