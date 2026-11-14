#!/bin/sh
# Phones only allow the microphone (and installing the app) over HTTPS. This makes a small local
# certificate authority plus a certificate for this machine's network address. No internet needed.
#   ./make-cert.sh            # uses this Mac's Wi-Fi/Ethernet address
#   ./make-cert.sh 192.168.1.20
set -e
IP="${1:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')}"
[ -n "$IP" ] || { echo "Couldn't find this machine's network address. Pass it: ./make-cert.sh 192.168.x.x"; exit 1; }
cd "$(dirname "$0")" && mkdir -p data/cert && cd data/cert

# The authority is made once and reused, so phones that trust it keep working when the address changes.
# Extensions are spelled out because strict clients (newer phones, Python 3.13) reject certificates without them.
if [ ! -f ca.key ]; then
  printf "[req]\ndistinguished_name=dn\nx509_extensions=ca\n[dn]\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n" > ca.cnf
  openssl req -x509 -config ca.cnf -newkey rsa:2048 -nodes -days 3650 -sha256 -keyout ca.key -out ca.crt \
    -subj "/CN=Timesheet local CA" 2>/dev/null
  rm -f ca.cnf
fi
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=$IP" 2>/dev/null
printf "subjectAltName=IP:%s,DNS:localhost\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n" "$IP" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -sha256 -extfile server.ext -out server.crt 2>/dev/null
rm -f server.csr server.ext
chmod 600 ca.key server.key

echo "Certificate made for $IP"
echo "Start the server with:  TS_CERT=data/cert/server.crt TS_KEY=data/cert/server.key .venv/bin/python app.py"
echo "Then on each phone open https://$IP:8000/ca.crt and trust it (see README, Phones)."
