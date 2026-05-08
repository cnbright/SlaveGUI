from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--aux-worker":
        from .service import worker_main_from_payload

        raise SystemExit(worker_main_from_payload(sys.argv[2]))

    import multiprocessing
    from .gui import PmicAuxGuiApp

    app = PmicAuxGuiApp(Path(__file__).resolve().parents[1])
    app.mainloop()


if __name__ == "__main__":
    main()
