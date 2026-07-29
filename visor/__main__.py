"""Entry point for `python -m visor` — same CLI as the `visor` script.

The AppImage runs the bundled interpreter this way, since it has no
console-script shims on PATH.
"""

from visor.cli.main import app

if __name__ == "__main__":
    app()
