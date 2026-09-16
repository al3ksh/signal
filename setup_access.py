"""Create/reset a private access file. Prints a fresh password once; do not put it in source control."""
import hashlib
import json
import os
from pathlib import Path
import secrets

def create_access(target):
    password = secrets.token_urlsafe(18)
    salt = secrets.token_bytes(32)
    iterations = 310000
    auth = {'salt': salt.hex(), 'iterations': iterations, 'hash': hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations).hex(), 'secret': secrets.token_hex(32)}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(auth, stream)
    return password

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?', default=os.path.join(os.getenv('SIGNAL_DATA_DIR', 'data'), 'access.json'))
    parser.add_argument('--if-missing', action='store_true', help='Keep existing credentials on repeated setup.')
    args = parser.parse_args()
    if args.if_missing and Path(args.target).exists():
        print('Existing access password preserved.')
    else:
        print('Save this access password; it is only shown once:\n' + create_access(args.target))
