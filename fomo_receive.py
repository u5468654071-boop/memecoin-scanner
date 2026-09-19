"""Receive bounded public discovery JSON through a dedicated SSH forced command.

Run as an unprivileged account with write access only to the inbox directory.
The administrator fixes --output in authorized_keys; client commands are rejected.
No sessions, keys, shell commands, market assertions or orders are accepted.
"""
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

from fomo_source import MAX_BYTES, parse_snapshot
from service_health import write_json


def receive(raw, path, now=None):
    data, captured, digest = parse_snapshot(raw, now)
    path = Path(path)
    # Lock covers the comparison and atomic replacement, including concurrent SSHs.
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            with path.open('rb') as stream:
                _, prior_at, prior_digest = parse_snapshot(stream.read(MAX_BYTES+1), now, max_age=None)
            if captured < prior_at:
                raise ValueError('out_of_order')
            if captured == prior_at and digest != prior_digest:
                raise ValueError('conflicting_capture')
            if digest == prior_digest:
                return {'accepted':True, 'state':'unchanged', 'snapshot_id':digest}
        write_json(path, data)
    return {'accepted':True, 'state':'received', 'snapshot_id':digest,
            'captured_at':data['captured_at'], 'unique_tokens':len({t['mint'] for t in data['tokens']})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o027)
    try:
        if os.environ.get('SSH_ORIGINAL_COMMAND', ''):
            raise ValueError('remote_commands_disabled')
        result = receive(sys.stdin.buffer.read(MAX_BYTES+1), args.output)
    except (OSError, ValueError) as exc:
        result = {'accepted':False, 'reason':str(exc) if isinstance(exc,ValueError) else 'file_error'}
    print(json.dumps(result, allow_nan=False))
    return 0 if result['accepted'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
