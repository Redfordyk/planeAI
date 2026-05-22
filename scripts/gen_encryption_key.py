"""Generate a Fernet-compatible key for django-encrypted-model-fields.

Run on the host (NOT in CI, NOT inside any container that logs stdout
somewhere persistent):

    python scripts/gen_encryption_key.py

Copy the printed value into the `FIELD_ENCRYPTION_KEY` slot in the
host's `.env` (chmod 600) or your secrets manager. Do NOT commit it.
Losing this key means every encrypted WorkspaceAIConfig row in the
database becomes unreadable — back up the value somewhere with the
same care you give the database itself.

Format: 32 random bytes, urlsafe-base64-encoded. Matches what
cryptography.fernet.Fernet.generate_key() returns; the field library
accepts either Fernet keys or 32-byte urlsafe-b64 strings.
"""

from __future__ import annotations

import base64
import os
import sys


def main() -> int:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
