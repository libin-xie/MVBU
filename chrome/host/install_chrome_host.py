#!/usr/bin/env python3
"""Registers the MVBU helper (MultiValue Basic Utility) with Chrome, Chromium, Edge and Brave.

Works on Windows, macOS and Linux, for the current user only (no administrator rights).
On Windows it writes the registry with Python's winreg module, so no PowerShell script runs
and PowerShell execution policies (signed-scripts-only) do not apply.

    python install_chrome_host.py                       use the first Python that has uopy
    python install_chrome_host.py --python PATH         use this Python for the helper
    python install_chrome_host.py --extension-id ID     also allow another extension ID
    python install_chrome_host.py --uninstall           remove the registration

Usually started from install-chrome-host.cmd (Windows) or install-chrome-host.sh (macOS / Linux).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

NAME = "com.clarityascend.mvbu"
# Extension IDs the helper accepts. More can be added with --extension-id.
STORE_ID = "heepofiejichbhogampmkghinpgjilpa"  # Chrome Web Store
                                                # https://chrome.google.com/webstore/detail/heepofiejichbhogampmkghinpgjilpa
DEV_ID = "fjppjmpkcfdmbibpmfaflmpggfcidfil"    # unpacked build of chrome/ext (fixed key in chrome/extension-key.txt)
EXT_IDS = [STORE_ID, DEV_ID]
HERE = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(HERE, "mvbu_native_host.py")
# Copied into the install folder, so the unpacked package can be moved or deleted after installing.
HELPER_FILES = ["mvbu_native_host.py", "mvbu_helper.py"]
MOCK_DIR = "mock"
DESCRIPTION = "MVBU helper (Rocket uopy bridge to UniVerse and UniData)"
WINDOWS = sys.platform == "win32"

# Windows: registry keys (under HKEY_CURRENT_USER) whose default value points at the manifest.
WIN_KEYS = [(browser, base + "\\NativeMessagingHosts\\" + NAME) for browser, base in [
    ("Chrome", r"Software\Google\Chrome"),
    ("Chromium", r"Software\Chromium"),
    ("Edge", r"Software\Microsoft\Edge"),
    ("Brave", r"Software\BraveSoftware\Brave-Browser"),
]]


def browser_dirs():
    """macOS / Linux: (browser, profile folder, always) — the manifest goes in <folder>/NativeMessagingHosts."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
        return [("Chrome", os.path.join(base, "Google", "Chrome"), True),
                ("Chrome Beta", os.path.join(base, "Google", "Chrome Beta"), False),
                ("Chromium", os.path.join(base, "Chromium"), False),
                ("Edge", os.path.join(base, "Microsoft Edge"), False),
                ("Brave", os.path.join(base, "BraveSoftware", "Brave-Browser"), False)]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [("Chrome", os.path.join(base, "google-chrome"), True),
            ("Chrome Beta", os.path.join(base, "google-chrome-beta"), False),
            ("Chromium", os.path.join(base, "chromium"), False),
            ("Edge", os.path.join(base, "microsoft-edge"), False),
            ("Brave", os.path.join(base, "BraveSoftware", "Brave-Browser"), False)]


def install_dir():
    if WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "MVBU")
    return os.path.join(os.path.expanduser("~"), ".mvbu")


# ----------------------------------------------------------------------------- Python
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def real_python(cmd):
    """The executable behind a command such as ['py', '-3'], or None if it does not run Python 3."""
    p = run(cmd + ["-c", "import sys; print(sys.executable if sys.version_info >= (3, 8) else '')"])
    path = p.stdout.strip() if p and p.returncode == 0 else ""
    return os.path.abspath(path) if path else None


def has_uopy(python):
    p = run([python, "-c", "import uopy"])
    return bool(p and p.returncode == 0)


def candidates():
    seen, out = set(), []

    def add(path):
        if path and "WindowsApps" not in path and os.path.normcase(path) not in seen:
            seen.add(os.path.normcase(path))
            out.append(path)

    add(os.path.abspath(sys.executable))
    if WINDOWS and shutil.which("py"):
        p = run(["py", "-0p"])  # every Python the launcher knows about
        for line in (p.stdout.splitlines() if p and p.returncode == 0 else []):
            part = line.strip().split()
            if part and part[-1].lower().endswith("python.exe"):
                add(real_python([part[-1]]))
        add(real_python(["py", "-3"]))
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            add(real_python([exe]))
    if sys.platform == "darwin":
        for exe in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            if os.path.exists(exe):
                add(real_python([exe]))
    return [c for c in out if c]


def pick_python(requested):
    if requested:
        exe = real_python([requested])
        if not exe:
            sys.exit("Python 3.8 or later was not found at: %s" % requested)
        return exe
    found = candidates()
    with_uopy = [c for c in found if has_uopy(c)]
    if with_uopy:
        return with_uopy[0]
    if found:
        return found[0]
    sys.exit("Python 3 was not found. Install Python 3.8 or later, then run: python -m pip install uopy")


# ----------------------------------------------------------------------------- install
def copy_helper():
    """Copy the helper (and its mock data) into the install folder. Returns the installed host script."""
    d = install_dir()
    os.makedirs(d, exist_ok=True)
    for name in HELPER_FILES:
        shutil.copyfile(os.path.join(HERE, name), os.path.join(d, name))
    src = os.path.join(HERE, MOCK_DIR)
    if os.path.isdir(src):
        dst = os.path.join(d, MOCK_DIR)
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            if os.path.isfile(os.path.join(src, name)):
                shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
    return os.path.join(d, "mvbu_native_host.py")


def write_launcher(python, host_script):
    d = install_dir()
    os.makedirs(d, exist_ok=True)
    if WINDOWS:
        path = os.path.join(d, "mvbu-native-host.bat")
        with open(path, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
            f.write('@echo off\n"%s" -u "%s" %%*\n' % (python, host_script))
    else:
        path = os.path.join(d, "mvbu-native-host")
        with open(path, "w", encoding="utf-8") as f:
            # Chrome starts programs with a minimal PATH, so the Python path is fixed here.
            f.write('#!/bin/sh\nexec "%s" -u "%s" "$@"\n' % (python, host_script))
        os.chmod(path, 0o755)
    return path


def manifest(launcher, extra_ids):
    ids = []
    for i in EXT_IDS + list(extra_ids):
        if i and i not in ids:
            ids.append(i)
    origins = ["chrome-extension://%s/" % i for i in ids]
    return {"name": NAME, "description": DESCRIPTION, "path": launcher, "type": "stdio", "allowed_origins": origins}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:  # no byte-order mark: Chrome rejects one
        json.dump(data, f, indent=2)
        f.write("\n")


def install(python, extra_ids):
    host = copy_helper()
    launcher = write_launcher(python, host)
    data = manifest(launcher, extra_ids)
    done = []
    if WINDOWS:
        import winreg
        path = os.path.join(install_dir(), NAME + ".json")
        write_json(path, data)
        for browser, key in WIN_KEYS:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, path)
            done.append(browser)
    else:
        for browser, folder, always in browser_dirs():
            if always or os.path.isdir(folder):
                write_json(os.path.join(folder, "NativeMessagingHosts", NAME + ".json"), data)
                done.append(browser)
    return launcher, host, done


def uninstall():
    if WINDOWS:
        import winreg
        for _, key in WIN_KEYS:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass
        files = [os.path.join(install_dir(), NAME + ".json"), os.path.join(install_dir(), "mvbu-native-host.bat")]
    else:
        files = [os.path.join(f, "NativeMessagingHosts", NAME + ".json") for _, f, _ in browser_dirs()]
        files.append(os.path.join(install_dir(), "mvbu-native-host"))
    d = install_dir()
    files += [os.path.join(d, n) for n in HELPER_FILES]
    mock = os.path.join(d, MOCK_DIR)
    if os.path.isdir(mock):
        files += [os.path.join(mock, n) for n in os.listdir(mock)]
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass
    for folder in (os.path.join(d, "__pycache__"), mock, d):  # remove the folders only if nothing else is left
        try:
            if os.path.basename(folder) == "__pycache__":
                shutil.rmtree(folder)
            else:
                os.rmdir(folder)
        except OSError:
            pass
    print("The MVBU helper has been removed from Chrome, Chromium, Edge and Brave.")


def main():
    ap = argparse.ArgumentParser(description="Register the MVBU helper with Chrome, Chromium, Edge and Brave.")
    ap.add_argument("--python", help="Python executable the helper should use (default: the first one with uopy)")
    ap.add_argument("--extension-id", action="append", default=[], help="also allow this extension ID (can be repeated)")
    ap.add_argument("--uninstall", action="store_true", help="remove the registration")
    a = ap.parse_args()
    if a.uninstall:
        uninstall()
        return
    missing = [n for n in HELPER_FILES if not os.path.exists(os.path.join(HERE, n))]
    if missing:
        sys.exit("%s not found next to this script (%s). Unpack the whole package first." % (" and ".join(missing), HERE))
    extra = a.extension_id + [os.environ.get("MVBU_EXTENSION_ID", "")]
    python = pick_python(a.python)
    if not has_uopy(python):
        print("Warning: Rocket uopy is not installed for %s." % python)
        print("         Mock mode works, but connecting to a server does not. Install it with:")
        print('         "%s" -m pip install uopy' % python)
        print()
    launcher, host, browsers = install(python, extra)
    print("Registered with: %s" % ", ".join(browsers))
    print()
    print("The MVBU helper is installed.")
    print("  Python: %s" % python)
    print("  Helper: %s" % host)
    print("  Launcher: %s" % launcher)
    print()
    print("The helper now runs from %s, so this unpacked folder can be moved or deleted." % install_dir())
    print("Run this installer again after updating MVBU.")
    print()
    print("Next: install MultiValue Basic Utility from the Chrome Web Store:")
    print("  https://chrome.google.com/webstore/detail/%s" % STORE_ID)
    print("If MultiValue Basic Utility is already open, reload the page.")


if __name__ == "__main__":
    main()
