"""Double-click launcher: starts Claude Pulse without a console window."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_pulse.__main__ import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(sys.argv))
