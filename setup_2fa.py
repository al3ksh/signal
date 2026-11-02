"""Enroll an authenticator app for app control through the public address. Shows the secret and backup codes once.

Run on the host as the user SIGNAL runs as. Removing the file turns public control off again."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from urllib.parse import quote


def create_2fa(target, label='SIGNAL'):
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
    backup = [secrets.token_hex(5) for _ in range(8)]
    data = {'secret': secret, 'backup': [hashlib.sha256(code.encode()).hexdigest() for code in backup]}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
    uri = 'otpauth://totp/' + quote(label) + '?secret=' + secret + '&issuer=SIGNAL&algorithm=SHA1&digits=6&period=30'
    return secret, uri, [code[:5] + '-' + code[5:] for code in backup]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?', default=os.getenv('SIGNAL_CONTROL_2FA_FILE') or os.path.join(os.getenv('SIGNAL_DATA_DIR', 'data'), 'control-2fa.json'))
    parser.add_argument('--reset', action='store_true', help='Replace an existing authenticator; the old one stops working.')
    parser.add_argument('--label', default='SIGNAL')
    args = parser.parse_args()
    if Path(args.target).exists():
        if not args.reset:
            raise SystemExit('An authenticator is already enrolled. Use --reset to replace it.')
        Path(args.target).unlink()
    secret, uri, backup = create_2fa(args.target, args.label)
    if shutil.which('qrencode'):
        subprocess.run(['qrencode', '-t', 'ansiutf8', uri], check=False)
    print('Scan the code above, or add this setup key by hand (time-based):\n  ' + ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    print('\nBackup codes, each works once. Keep them somewhere safe:\n  ' + '\n  '.join(backup))
    print('\nShown only once. Control through the public address now asks for the password and a code.')
