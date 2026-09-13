"""python -m loa.ripperdoc — run the console (the client).

The console lives in this package's `__init__` (it is a library module with a
`main()` as much as it is a program); this is only the `-m` door, so the module
runs the same entry the installed `ripperdoc` script does.
"""
from loa.ripperdoc import main

if __name__ == "__main__":
    raise SystemExit(main())
