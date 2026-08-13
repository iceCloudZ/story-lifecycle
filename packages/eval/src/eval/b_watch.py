# -*- coding: utf-8 -*-
"""B 线 runner 守护：每 5 分钟检查 runner 存活，死了自动重启（断点续跑保证不重不丢）。"""
import subprocess
import time

RUNNER_MARK = "b_line_runner.py"
PY = r"D:\github\story-lifecycle\.venv-monorepo-test\Scripts\python.exe"
LAUNCHER = r"C:\Users\zzh58\AppData\Local\Temp\opencode\launch_b_line.py"


def runner_alive() -> bool:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*b_line_runner*' } | Measure-Object | Select-Object -ExpandProperty Count"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def main() -> None:
    while True:
        try:
            if not runner_alive():
                print(f"[watch {time.strftime('%H:%M:%S')}] runner 不在 → 重启", flush=True)
                subprocess.run([PY, LAUNCHER], timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"[watch] 检查异常: {exc}", flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
