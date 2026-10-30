"""
Update check + self-update from GitHub Releases.

check() asks the GitHub API for the latest release. install() downloads its zip,
unpacks it to a temp folder and hands over to a tiny PowerShell script that waits
for this process to exit, copies the new files over the install folder and starts
the app again. Only the PyInstaller build can update itself; from source it just
opens the release page.
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

from version import __version__

REPO = "wisnuub/arctis-control"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
FROZEN = getattr(sys, "frozen", False)


def _ver(tag):
    return tuple(int(p) for p in tag.lstrip("vV").split(".") if p.isdigit())


def check(timeout=10):
    """{'tag', 'newer', 'zip_url', 'page'} for the latest release; raises on network errors."""
    req = urllib.request.Request(API, headers={"User-Agent": f"ArctisControl/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    zip_url = next((a["browser_download_url"] for a in rel.get("assets", [])
                    if a["name"].lower().endswith(".zip")), None)
    tag = rel["tag_name"]
    return {"tag": tag, "newer": _ver(tag) > _ver(__version__), "zip_url": zip_url,
            "page": rel.get("html_url", f"https://github.com/{REPO}/releases")}


def install(info):
    """Download + stage the update and launch the swap script. Returns True if the
    caller should now quit so the script can replace the files."""
    if not FROZEN or not info.get("zip_url"):
        webbrowser.open(info["page"])
        return False
    work = Path(tempfile.mkdtemp(prefix="ArctisControl-update-"))
    zpath = work / "update.zip"
    req = urllib.request.Request(info["zip_url"], headers={"User-Agent": "ArctisControl"})
    with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
        f.write(r.read())
    staged = work / "files"
    with zipfile.ZipFile(zpath) as z:
        z.extractall(staged)
    if not (staged / "ArctisControl.exe").exists():
        raise RuntimeError("update zip has no ArctisControl.exe")

    install_dir = Path(sys.executable).parent
    script = work / "swap.ps1"
    script.write_text(f'''
$ErrorActionPreference = "SilentlyContinue"
Wait-Process -Id {os.getpid()} -Timeout 30
Copy-Item -Path "{staged}\\*" -Destination "{install_dir}" -Recurse -Force
Start-Process -FilePath "{install_dir / "ArctisControl.exe"}" -ArgumentList "--background"
Start-Sleep 2
Remove-Item -Path "{work}" -Recurse -Force
''', encoding="utf-8")
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", str(script)],
                     creationflags=0x08000000 | 0x00000008)   # no window, detached
    return True
