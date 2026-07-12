from __future__ import annotations

import os
import pty
import subprocess
import sys

import pytest

from plsqlwks.ui import (
    CTRL_G,
    KEY_ALT_PLUS,
    KEY_ALT_X,
    KEY_CTRL_ALT_C,
    KEY_CTRL_ALT_R,
    KEY_CTRL_DOWN,
    KEY_CTRL_ENTER,
    KEY_CTRL_EQUALS,
    KEY_CTRL_UP,
)


HARNESS = r"""
import os
import sys
import tty

from plsqlwks.ui import decode_key_sequence

tty.setraw(0)
size = int(sys.argv[1])
print("READY", flush=True)
data = os.read(0, size)
decoded = decode_key_sequence(list(data))
print("" if decoded is None else decoded, flush=True)
"""


@pytest.mark.pty
@pytest.mark.integration
@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (b"\x1bx", KEY_ALT_X),
        (b"\x1bX", KEY_ALT_X),
        (b"\x1b+", KEY_ALT_PLUS),
        (b"\x1b\x03", KEY_CTRL_ALT_C),
        (b"\x1b\x12", KEY_CTRL_ALT_R),
        (b"\x1b[99;7u", KEY_CTRL_ALT_C),
        (b"\x1b[114;7u", KEY_CTRL_ALT_R),
        (b"\x1b[13;5u", KEY_CTRL_ENTER),
        (b"\x1b[27;5;13~", KEY_CTRL_ENTER),
        (b"\x1b[13;5~", KEY_CTRL_ENTER),
        (b"\x1b[10;5u", KEY_CTRL_ENTER),
        (b"\x1b[10;5~", KEY_CTRL_ENTER),
        (b"\n", KEY_CTRL_ENTER),
        (b"\x1b[61;5u", KEY_CTRL_EQUALS),
        (b"\x1b[27;5;61~", KEY_CTRL_EQUALS),
        (b"\x1b[103;5u", CTRL_G),
        (b"\x1b[27;5;103~", CTRL_G),
        (b"\x1b[1;5A", KEY_CTRL_UP),
        (b"\x1b[1;5B", KEY_CTRL_DOWN),
        (b"\x1b[27;5;65~", KEY_CTRL_UP),
        (b"\x1b[27;5;66~", KEY_CTRL_DOWN),
    ],
)
def test_pty_key_sequences_decode_to_shortcuts(sequence: bytes, expected: int):
    assert run_key_harness(sequence) == str(expected)


@pytest.mark.pty
@pytest.mark.integration
def test_pty_raw_cr_enter_is_not_decoded_as_ctrl_enter():
    assert run_key_harness(b"\r") == ""


def run_key_harness(sequence: bytes) -> str:
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"PTY is not available: {exc}")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(len(sequence))],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        slave = -1
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        os.write(master, sequence)
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
    assert proc.returncode == 0, stderr
    return stdout.strip()
