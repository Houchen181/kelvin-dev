"""Check the App Store Connect secrets and write the API key as a clean .p8 file (for ios-testflight.yml).

    python3 asc_key.py <where to write the key>     # reads APPLE_TEAM_ID, ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_P8

A key pasted with Windows line endings, a byte-order mark, quotes or without its BEGIN/END lines is tidied. Says which
secret looks wrong, by its shape only: never prints a secret's value.
"""
import base64
import os
import re
import sys

SHAPES = [
    ("APPLE_TEAM_ID", r"[A-Z0-9]{10}", "10 letters and digits (developer.apple.com/account, Membership details, Team ID)"),
    ("ASC_KEY_ID", r"[A-Z0-9]{10}", "10 letters and digits (the key's Key ID in App Store Connect)"),
    ("ASC_ISSUER_ID", r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
     "an id with dashes (the Issuer ID, above the list of keys)"),
]


def key_bytes(raw: str) -> tuple:
    """(base64 text, decoded bytes) of a pasted key; bytes are empty when it is not base64."""
    text = raw.replace("﻿", "").replace("\r", "").strip().strip("\"'")
    body = re.sub(r"\s", "", re.sub(r"-----(BEGIN|END)[^-]*-----", "", text))
    try:
        return body, base64.b64decode(body, validate=True)
    except ValueError:
        return body, b""


def check(env: dict) -> tuple:
    """(problems, the key as PEM text)."""
    problems = []
    for name, pattern, what in SHAPES:
        value = env.get(name, "").strip()
        if not value:
            problems.append("%s is missing." % name)
        elif not re.fullmatch(pattern, value, re.I):
            problems.append("%s should be %s; it has %d characters." % (name, what, len(value)))
    raw = env.get("ASC_KEY_P8", "")
    body, der = key_bytes(raw)
    # An App Store Connect key is an EC P-256 private key in PKCS#8: about 138 bytes, a DER SEQUENCE.
    if not raw.strip():
        problems.append("ASC_KEY_P8 is missing.")
    elif not der or der[0] != 0x30 or not 100 <= len(der) <= 300:
        problems.append("ASC_KEY_P8 is not the text of an AuthKey_<id>.p8 file (it decodes to %d bytes; a key is "
                        "about 138). Open the .p8 file in Notepad and paste all of it, the BEGIN and END lines too."
                        % len(der))
    pem = "-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----\n" % "\n".join(
        body[i:i + 64] for i in range(0, len(body), 64))
    return problems, pem


def main() -> None:
    raw = os.environ.get("ASC_KEY_P8", "")
    print("ASC_KEY_P8 as stored: %d characters, %d lines, BEGIN line %s, END line %s, Windows line ends %s"
          % (len(raw), raw.count("\n") + 1, "yes" if "-----BEGIN" in raw else "no",
             "yes" if "-----END" in raw else "no", "yes" if "\r" in raw else "no"))
    problems, pem = check(os.environ)
    if problems:
        print("::error::" + " ".join(problems))
        sys.exit(1)
    with open(sys.argv[1], "w", newline="\n") as f:
        f.write(pem)
    os.chmod(sys.argv[1], 0o600)
    print("The secrets look right; the key is ready.")


if __name__ == "__main__":
    main()
