#!/bin/bash
# Sign the app with a stable self-signed identity instead of ad-hoc.
#
# An ad-hoc signature pins the app's identity to the hash of this exact build, so macOS treats
# every update as a brand-new app and drops what the user granted — access to removable volumes
# above all, which is where people keep their game libraries.  A self-signed certificate makes
# the designated requirement "identifier + certificate", so the next build still matches and the
# grant survives.  (Gatekeeper is unaffected either way: without notarization the download still
# needs the one-time xattr step.)
#
# The identity lives in its own keychain with a fixed password, so signing never pops a dialog.
# If anything here fails, the build falls back to ad-hoc signing.
set -uo pipefail
APP="$1"
NAME="Shader Warmer Signing"
KC="$HOME/Library/Keychains/shader-warmer-signing.keychain-db"
PW="shader-warmer-signing"

make_identity() {
    local tmp; tmp=$(mktemp -d)
    cat > "$tmp/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$tmp/key.pem" -out "$tmp/cert.pem" \
        -days 7300 -config "$tmp/cert.cnf" 2>/dev/null || return 1
    openssl pkcs12 -export -legacy -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -out "$tmp/id.p12" \
        -passout pass:"$PW" 2>/dev/null \
    || openssl pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -out "$tmp/id.p12" \
        -passout pass:"$PW" 2>/dev/null || return 1
    [ -f "$KC" ] || security create-keychain -p "$PW" "$KC" || return 1
    security set-keychain-settings "$KC"                       # no auto-lock timeout
    security unlock-keychain -p "$PW" "$KC" || return 1
    security import "$tmp/id.p12" -k "$KC" -P "$PW" -T /usr/bin/codesign >/dev/null || return 1
    security set-key-partition-list -S apple-tool:,apple: -s -k "$PW" "$KC" >/dev/null 2>&1
    rm -rf "$tmp"
}

signed=0
if command -v openssl >/dev/null; then
    [ -f "$KC" ] && security unlock-keychain -p "$PW" "$KC" 2>/dev/null
    if ! security find-identity -p codesigning "$KC" 2>/dev/null | grep -q "$NAME"; then
        make_identity || echo "(не удалось создать подпись — будет ad-hoc)"
    fi
    if security find-identity -p codesigning "$KC" 2>/dev/null | grep -q "$NAME"; then
        codesign --force --deep --sign "$NAME" --keychain "$KC" "$APP" 2>/dev/null && signed=1
    fi
fi
if [ "$signed" = 1 ]; then
    echo "signed: $(codesign -d -r- "$APP" 2>&1 | grep designated)"
else
    codesign --force --deep --sign - "$APP" 2>/dev/null && echo "signed ad-hoc (identity unavailable)"
fi
