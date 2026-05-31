"""Allow running as `python -m collision_engine` from project root."""

import sys
from pathlib import Path

# 将 .local-packages 加入路径（Windows Store 版 Python 限制）
_local_pkg = Path(__file__).parent / ".local-packages"
if _local_pkg.is_dir():
    sys.path.insert(0, str(_local_pkg))

if __name__ == "__main__":
    from collision_engine import main

    main()
