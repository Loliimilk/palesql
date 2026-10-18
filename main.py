import sys

from palesql import cli as _cli


if __name__ == "__main__":
    raise SystemExit(_cli.main(sys.argv[1:]))

sys.modules[__name__] = _cli
