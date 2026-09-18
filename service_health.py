"""Estado de progreso local: no confundir proceso vivo con datos de mercado válidos."""
import json
import math
import os
import time
import uuid
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def heartbeat(path, stage, now=None):
    if path is not None:
        write_json(path, {'updated_at': time.time() if now is None else now, 'stage': stage, 'pid': os.getpid()})


def healthy(path, max_age=900, now=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        at = value['updated_at']
        current = time.time() if now is None else now
        return (not isinstance(at, bool) and isinstance(at, (int, float)) and math.isfinite(at)
                and 0 <= current-at <= max_age and value.get('stage') not in (None, 'stopped'))
    except (OSError, ValueError, TypeError, KeyError):
        return False
