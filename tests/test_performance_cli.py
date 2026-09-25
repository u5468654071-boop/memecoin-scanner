import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import main


class PerformanceCliTests(unittest.TestCase):
    def test_missing_database_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'absent'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(['performance', '--data-dir', str(root)])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(root.exists())

    def test_ambiguous_reversed_and_other_command_windows_rejected(self):
        for arguments in (
                ['performance', '--since', '2026-09-21'],
                ['performance', '--since', '2026-09-22T00:00:00Z', '--until', '2026-09-21T00:00:00Z'],
                ['report', '--since', '2026-09-21T00:00:00Z']):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_empty_existing_database_can_be_read_without_schema_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scanner.sqlite3'
            sqlite3.connect(path).close()
            original = path.read_bytes()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['performance', '--data-dir', folder,
                    '--since', '2026-09-21T00:00:00Z', '--until', '2026-09-25T00:00:00Z']), 0)
            self.assertIsInstance(json.loads(output.getvalue()), dict)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
