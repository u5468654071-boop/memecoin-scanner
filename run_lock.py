"""Bloqueo local liberado por el sistema operativo al cerrar o caer el proceso."""
import os
from pathlib import Path


class ScanLock:
    def __init__(self, db_path):
        self.path = Path(str(db_path) + '.scan.lock')
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('Ya hay un escaneo o evaluación usando esta base de datos') from None
        self.handle = handle

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None
