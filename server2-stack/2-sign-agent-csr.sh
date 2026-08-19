#!/usr/bin/env bash
#
# STEP 2 of 2 — run this on SERVER 1 (10.0.211.249), in the folder that already holds
# hospitalCA.crt and hospitalCA.key (your existing "certificates" directory).
#
# Signs the request Server 2 produced, so agent.hospital.lan gets a certificate from the same
# authority as openmrs.hospital.lan, orthanc.hospital.lan and viewer.hospital.lan.
#
# The CA private key is used here and stays here. Nothing secret goes back to Server 2 - only the
# signed certificate, which is public by nature.

set -euo pipefail

CA_CERT="${CA_CERT:-hospitalCA.crt}"
CA_KEY="${CA_KEY:-hospitalCA.key}"
CSR="${CSR:-agent.csr}"
EXT="${EXT:-agent-san.cnf}"
OUT="${OUT:-agent.crt}"
DAYS="${DAYS:-825}"   # clients reject server certificates valid for much longer than this

for required in "$CA_CERT" "$CA_KEY" "$CSR" "$EXT"; do
  if [ ! -f "$required" ]; then
    echo "Missing: $required" >&2
    echo >&2
    echo "Run this in the directory that holds hospitalCA.crt and hospitalCA.key, after copying" >&2
    echo "agent.csr and agent-san.cnf here from Server 2." >&2
    exit 1
  fi
done

echo "Signing $CSR with $CA_CERT ..."

# -extfile with the same file step 1 used guarantees the certificate carries exactly the names
# that were requested. Without it OpenSSL would drop the SANs entirely and produce a certificate
# every modern client rejects, because they ignore the common name and look only at SANs.
openssl x509 -req -sha256 -days "$DAYS" \
  -in "$CSR" \
  -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
  -extfile "$EXT" -extensions v3_req \
  -out "$OUT" 2>/dev/null

echo
echo "Signed: $OUT"
openssl x509 -in "$OUT" -noout -subject -issuer -dates | sed 's/^/  /'
openssl x509 -in "$OUT" -noout -text | grep -A1 "Subject Alternative Name" | tail -1 | sed 's/^ */  SAN: /'
echo

if openssl verify -CAfile "$CA_CERT" "$OUT" >/dev/null 2>&1; then
  echo "  Chain check: OK - this certificate verifies against $CA_CERT"
else
  echo "  Chain check: FAILED - do not deploy this certificate" >&2
  exit 1
fi

echo
echo "-----------------------------------------------------------------------"
echo "NEXT — two things, both on Server 1 first, then back to Server 2."
echo
echo "1) Let the OpenMRS container trust this CA, so the agentgateway module can call"
echo "   https://agent.hospital.lan. Without this the chat only ever says"
echo "   \"assistant indisponible\", and the real cause is an SSLHandshakeException"
echo "   buried in the Tomcat log."
echo
echo "   docker cp $CA_CERT openmrs-app:/tmp/hospitalCA.crt"
echo "   docker exec -u 0 openmrs-app keytool -importcert -noprompt \\"
echo "       -alias hospital-ca -file /tmp/hospitalCA.crt \\"
echo "       -keystore \$JAVA_HOME/jre/lib/security/cacerts -storepass changeit"
echo "   docker restart openmrs-app"
echo
echo "   # check it is there:"
echo "   docker exec openmrs-app keytool -list -alias hospital-ca \\"
echo "       -keystore \$JAVA_HOME/jre/lib/security/cacerts -storepass changeit"
echo
echo "2) Copy the signed certificate and the CA back to Server 2:"
echo
echo "   scp $OUT $CA_CERT user@10.0.211.250:/path/to/server2-stack/certs/"
echo "-----------------------------------------------------------------------"
