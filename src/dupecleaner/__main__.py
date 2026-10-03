"""Чтобы работало `python -m dupecleaner`, а не только `dupecleaner`.

Консольная команда ставится в `.venv\\Scripts\\` и вне активированного
окружения не находится — ровно на этом спотыкаешься, когда открываешь
PowerShell не в папке проекта. Запуск модулем работает всегда, если
интерпретатор тот самый:

    .venv\\Scripts\\python.exe -m dupecleaner albums --help

Три строки, которые убирают один тупик на каждом новом компьютере.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
