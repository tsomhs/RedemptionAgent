"""Run every test and finish with a one-line verdict in plain words."""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.path[:0] = [str(HERE), str(HERE.parent)]
    suite = unittest.defaultTestLoader.discover(str(HERE))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    bad = len(result.failures) + len(result.errors)
    print()
    if bad:
        print(f"ΑΠΟΤΕΛΕΣΜΑ: {bad} από {result.testsRun} tests ΑΠΕΤΥΧΑΝ. Δες τις γραμμές με FAIL ή ERROR πιο πάνω.")
    else:
        print(f"ΑΠΟΤΕΛΕΣΜΑ: και τα {result.testsRun} tests πέρασαν.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
