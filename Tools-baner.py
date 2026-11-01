
from datetime import datetime
from platform import platform
from socket import socket
import sys

# ==========================================
# CONSOLE ENCODING
# ==========================================

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# ==========================================
# CONFIGURATION
# ==========================================

GREEN = "\033[92m"
RESET = "\033[0m"

# ==========================================
# NETWORK SCAN BANNER
# ==========================================

banner = r"""
██╗██████╗      ███████╗ ██████╗ █████╗ ███╗   ██╗
██║██╔══██╗     ██╔════╝██╔════╝██╔██╗██║████╗ ██║
██║██████╔╝     ███████╗██║     ███████║██╔██╗ ██║
██║██╔═══╝      ╚════██║██║     ██╔══██║██║╚██╗██║
██║██║          ███████║╚██████╗██║  ██║██║ ╚████║
╚═╝╚═╝          ╚══════╝ ╚═════╝╚═╝  ╚═══╝╚═╝  ╚═══╝

          (●)
         / | \
        /  |  \
      (●) (●) (●)

     NETWORK SCAN
       IP-TOOLS

    [ SYSTEM CONSOLE ]
    [ VERSION 1.1    ]
    [ INITIALIZED    ]
"""

# ==========================================
# DISPLAY BANNER
# ==========================================

try:
    print(f"{GREEN}{banner}{RESET}", flush=True)
except (OSError, UnicodeError) as exc:
    print(f"バナーの表示に失敗しました: {exc}", file=sys.stderr)

