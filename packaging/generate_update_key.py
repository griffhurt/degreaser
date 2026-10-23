"""Create the EdDSA key pair that signs update archives (docs/decisions.md D41). Run once:

    uv run python packaging/generate_update_key.py [--private-key-file PATH]

- Writes the public key to src/degreaser/updates/public_key.txt (commit it: Sparkle and
  WinSparkle use it to verify every update).
- Writes the private key to PATH (default ~/degreaser-update-private-key.txt, readable only by
  you) and never prints it. Store it as the SPARKLE_ED_PRIVATE_KEY repository secret:

      gh secret set SPARKLE_ED_PRIVATE_KEY < ~/degreaser-update-private-key.txt

  then keep the file somewhere safe (a password manager) and delete it. Losing the private key
  means installed copies can't verify new updates; replacing the key (--force) has the same
  effect, so only do it if the old one leaked.
"""

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_KEY_FILE = ROOT / "src" / "degreaser" / "updates" / "public_key.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--private-key-file", type=Path, default=Path.home() / "degreaser-update-private-key.txt"
    )
    parser.add_argument("--force", action="store_true", help="replace an existing key pair")
    args = parser.parse_args()

    if PUBLIC_KEY_FILE.read_text("utf-8").strip() and not args.force:
        print(f"{PUBLIC_KEY_FILE} already has a key. Use --force only if it leaked.")
        return 1
    if args.private_key_file.exists():
        print(f"{args.private_key_file} already exists; not overwriting it.")
        return 1

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    fd = os.open(args.private_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(seed).decode() + "\n")
    PUBLIC_KEY_FILE.write_text(base64.b64encode(public).decode() + "\n", encoding="utf-8")
    print(f"Public key written to {PUBLIC_KEY_FILE.relative_to(ROOT)} (commit it).")
    print(f"Private key written to {args.private_key_file}. Next:")
    print(f"  gh secret set SPARKLE_ED_PRIVATE_KEY < {args.private_key_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
