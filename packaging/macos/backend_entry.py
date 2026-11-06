"""PyInstaller entrypoint. Keep relative imports inside the application package."""

import multiprocessing

from plamo_cp.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
