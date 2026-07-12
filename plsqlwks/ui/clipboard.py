from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
from typing import Callable

@dataclass(frozen=True)
class ClipboardProvider:
    name: str
    copy: Callable[[str], bool] | None = None
    paste: Callable[[], str | None] | None = None

def normalize_clipboard_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def copy_to_system_clipboard(text: str, providers: list[ClipboardProvider] | None = None) -> str | None:
    for provider in providers if providers is not None else system_clipboard_providers():
        if provider.copy is None:
            continue
        try:
            if provider.copy(text):
                return provider.name
        except Exception:
            continue
    return None


def paste_from_clipboard(
    internal_clipboard: str,
    providers: list[ClipboardProvider] | None = None,
) -> tuple[str, str]:
    for provider in providers if providers is not None else system_clipboard_providers():
        if provider.paste is None:
            continue
        try:
            text = provider.paste()
        except Exception:
            continue
        if text:
            return normalize_clipboard_text(text), provider.name
    if internal_clipboard:
        return normalize_clipboard_text(internal_clipboard), "internal clipboard"
    return "", "clipboard"


def system_clipboard_providers() -> list[ClipboardProvider]:
    providers: list[ClipboardProvider] = []
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        providers.append(
            ClipboardProvider(
                name="Wayland clipboard",
                copy=command_clipboard_copy(["wl-copy"]),
                paste=command_clipboard_paste(["wl-paste"]),
            )
        )
    if shutil.which("xclip"):
        providers.append(
            ClipboardProvider(
                name="X11 clipboard",
                copy=command_clipboard_copy(["xclip", "-selection", "clipboard"]),
                paste=command_clipboard_paste(["xclip", "-selection", "clipboard", "-o"]),
            )
        )
    if shutil.which("clip.exe") or shutil.which("powershell.exe"):
        providers.append(
            ClipboardProvider(
                name="Windows clipboard",
                copy=command_clipboard_copy(["clip.exe"]) if shutil.which("clip.exe") else None,
                paste=command_clipboard_paste(
                    ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
                )
                if shutil.which("powershell.exe")
                else None,
            )
        )
    return providers


def command_clipboard_copy(command: list[str]) -> Callable[[str], bool]:
    def copy(text: str) -> bool:
        completed = subprocess.run(
            command,
            input=text,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return completed.returncode == 0

    return copy


def command_clipboard_paste(command: list[str]) -> Callable[[], str | None]:
    def paste() -> str | None:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout

    return paste
