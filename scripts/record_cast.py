#!/usr/bin/env python3
"""Records a terminal session as an asciicast v2 file.

There is no asciinema binary in this repo's toolchain and adding one as a
dependency to produce a README image would be a poor trade, so this is the
~60 lines of `pty` that the format actually needs. The output is the standard
asciicast v2 format, so it plays anywhere asciinema casts play and renders to
an animated SVG with:

    npx svg-term-cli --in docs/assets/demo.cast --out docs/assets/demo.svg

Usage:
    python scripts/record_cast.py OUT.cast -- command args...

The recording is a real session: the command runs, its real output is
captured with real timings, and nothing is typed in afterwards.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shlex
import signal
import struct
import sys
import termios
import time
import fcntl

COLUMNS = 100
ROWS = 30


def record(out_path: str, argv: list[str]) -> int:
    pid, master = pty.fork()
    if pid == 0:
        # Child: a predictable terminal, so the cast renders the same way
        # wherever it was recorded.
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLUMNS"] = str(COLUMNS)
        os.environ["LINES"] = str(ROWS)
        os.execvp(argv[0], argv)
        os._exit(127)

    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))

    header = {
        "version": 2,
        "width": COLUMNS,
        "height": ROWS,
        "timestamp": int(time.time()),
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
    }

    started = time.time()
    events: list[list] = []
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.2)
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                events.append([round(time.time() - started, 6), "o", chunk.decode("utf-8", "replace")])
                # Mirror to the real terminal so a human can watch it happen.
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
    except KeyboardInterrupt:
        os.kill(pid, signal.SIGINT)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        for event in events:
            handle.write(json.dumps(event) + "\n")

    print(f"\nwrote {out_path} ({len(events)} events)", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if "--" not in argv or len(argv) < 3:
        print(f"usage: {argv[0]} OUT.cast -- command args...", file=sys.stderr)
        return 2
    split = argv.index("--")
    out_path = argv[1]
    command = argv[split + 1 :]
    if not command:
        print("no command given", file=sys.stderr)
        return 2
    print(f"recording: {shlex.join(command)}", file=sys.stderr)
    return record(out_path, command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
