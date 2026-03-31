from __future__ import annotations

from pathlib import Path

from .gui import PmicAuxGuiApp


def main() -> None:
    app = PmicAuxGuiApp(Path(__file__).resolve().parents[1])
    app.mainloop()


if __name__ == "__main__":
    main()

