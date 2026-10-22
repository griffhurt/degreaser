# PyInstaller entry script (it needs a file, not a module path).
import sys

from degreaser.__main__ import main

sys.exit(main())
