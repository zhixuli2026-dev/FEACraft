"""Fixed Windows launcher: select UTF-8 before the approved cmd is created."""
import argparse
import base64
import ctypes
import os
from pathlib import Path
import subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--command',required=True);args=parser.parse_args()
    command=base64.b64decode(args.command,validate=True).decode('utf8')
    if not command or len(command)>4000 or '\0' in command:raise ValueError('Invalid approved Windows command')
    if os.name!='nt':raise RuntimeError('Windows command launcher requires Windows')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    if not kernel.SetConsoleCP(65001) or not kernel.SetConsoleOutputCP(65001):raise ctypes.WinError(ctypes.get_last_error())
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/cmd.exe'
    # The registered gate owns this process and every descendant. Keep the
    # user's shell text intact; cmd must see UTF-8 at startup, before /c parses it.
    with subprocess.Popen(command,shell=True,executable=str(executable),stdin=subprocess.DEVNULL,stderr=subprocess.STDOUT) as process:
        return process.wait()


if __name__=='__main__':raise SystemExit(main())
