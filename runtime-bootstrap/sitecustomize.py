"""Copied only into the private runtime's site-packages by the payload builder."""
import os
import sys

sys.dont_write_bytecode = True
try:
    from scripts.local_installation import bootstrap_installed
    if bootstrap_installed() is None:
        raise ValueError("installation_required")
except BaseException:
    try:
        os.write(2, b"installed_runtime_unavailable\n")
    except OSError:
        pass
    os._exit(78)
