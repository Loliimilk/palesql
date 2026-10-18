import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build_suite():
    return unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(ROOT),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run PaleSQL tests")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="show only the final summary",
    )
    args = parser.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    print("PaleSQL test suite")
    print(f"Repository: {ROOT}")
    print("Checking unit and integration tests...\n")
    result = unittest.TextTestRunner(
        verbosity=1 if args.quiet else 2,
        stream=sys.stdout,
        descriptions=True,
    ).run(build_suite())
    print("\nPaleSQL test summary")
    print(f"  tests:   {result.testsRun}")
    print(f"  failures: {len(result.failures)}")
    print(f"  errors:   {len(result.errors)}")
    print(f"  skipped:  {len(result.skipped)}")
    print("  status:   PASS" if result.wasSuccessful() else "  status:   FAIL")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
