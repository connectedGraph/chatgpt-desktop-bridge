import os
import sys
import glob
import subprocess
import shutil

def find_chatgpt_executable(custom_path=None) -> str:
    """
    Finds the ChatGPT executable using multiple strategies:
    1. Explicit custom path (CLI arg, config, or env var CHATGPT_PATH)
    2. Check active running process
    3. Windows AppX / MSIX package discovery (OpenAI.Codex)
    4. Common Windows installation directories
    5. Common macOS Application directories
    """
    # Strategy 1: User-specified custom path
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path)

    env_path = os.environ.get("CHATGPT_PATH")
    if env_path and os.path.isfile(env_path):
        return os.path.abspath(env_path)

    # Strategy 2: System PATH
    which_path = shutil.which("ChatGPT") or shutil.which("chatgpt")
    if which_path and os.path.isfile(which_path):
        return os.path.abspath(which_path)

    # Strategy 3: Platform specific detection
    if sys.platform == "win32":
        # Check WindowsApps directory for MSIX package (OpenAI.Codex)
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        windows_apps = os.path.join(prog_files, "WindowsApps")
        if os.path.isdir(windows_apps):
            pattern = os.path.join(windows_apps, "OpenAI.Codex_*", "app", "ChatGPT.exe")
            matches = glob.glob(pattern)
            if matches:
                # Pick the latest version if multiple exist
                matches.sort(reverse=True)
                return matches[0]

        # Query PowerShell AppxPackage if accessible
        try:
            cmd = 'Get-AppxPackage *OpenAI.Codex* | Select-Object -ExpandProperty InstallLocation'
            out = subprocess.check_output(["powershell", "-NoProfile", "-Command", cmd], text=True, stderr=subprocess.DEVNULL).strip()
            if out:
                candidate = os.path.join(out, "app", "ChatGPT.exe")
                if os.path.isfile(candidate):
                    return candidate
        except Exception:
            pass

        # Check standard Windows Win32 directories
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        candidates = [
            os.path.join(local_app_data, "Programs", "ChatGPT", "ChatGPT.exe"),
            os.path.join(local_app_data, "OpenAI", "ChatGPT", "ChatGPT.exe"),
            os.path.join(prog_files, "ChatGPT", "ChatGPT.exe"),
            os.path.join(prog_files, "OpenAI", "ChatGPT", "ChatGPT.exe"),
            os.path.join(prog_files_x86, "ChatGPT", "ChatGPT.exe"),
            os.path.join(prog_files_x86, "OpenAI", "ChatGPT", "ChatGPT.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return os.path.abspath(c)

    elif sys.platform == "darwin":
        mac_candidates = [
            "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
            os.path.expanduser("~/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
        ]
        for c in mac_candidates:
            if os.path.isfile(c):
                return os.path.abspath(c)

    return ""

if __name__ == "__main__":
    path = find_chatgpt_executable()
    print("Detected ChatGPT path:", path)
