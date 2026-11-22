"""Share files at unlisted, protected or open URLs with a time-to-live."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main

    _main()
