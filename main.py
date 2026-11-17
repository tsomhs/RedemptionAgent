"""Start the agent.

    python main.py                 scheduler and dashboard (same as: python main.py run)
    python main.py status --month 2026-10
    python main.py --help          every command
"""

import sys

from agent.__main__ import main

sys.exit(main())
