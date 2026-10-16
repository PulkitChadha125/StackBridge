"""Start of the StakBridge EXE (see StakBridge.spec). Running it from source works too:
    .venv\\Scripts\\python.exe desktop_launcher.py
"""

import multiprocessing

if __name__ == "__main__":
    # In the EXE every broker worker is the same EXE started again; this hands those over to the worker code.
    multiprocessing.freeze_support()
    from stakbridge.desktop import run

    run()
