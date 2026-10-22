#!/usr/bin/env python3
"""Fresh-install smoke check for a built agentkai wheel.

The unit suite runs against the source tree with dev dependencies, so it
cannot see packaging breakage. This script can: it creates a CLEAN venv,
installs ONLY the built wheel (dependencies resolve from PyPI exactly as
a user's would), then verifies the two things a fresh install must do:

1. ``agentkai doctor`` — the model-layer (litellm) check passes, i.e.
   LiteLLM and its runtime imports (notably ``mcp``) are really there.
2. ``agentkai run`` — with no provider keys configured, the run must
   reach the model stage and fail with per-provider credential errors.
   Dying on an import (``No module named ...``) is the failure this
   guards against (it shipped in <= 0.6.5 because ``mcp`` was an
   undeclared dependency of LiteLLM's tool-calling path).

Usage:
    python3 scripts/smoke_install.py [--dist dist] [--venv PATH] [--keep]

Exit code 0 = smoke passed, 1 = failed. Stdlib only; needs network for
pip, exactly like a real install.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

KEY_VARS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "OPENAI_API_KEY", "ZHIPUAI_API_KEY")


def run(cmd: list[str], env: dict | None = None, timeout: int = 600) -> str:
    proc = subprocess.run(cmd, env=env, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    return proc.stdout or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--dist", default=str(here / "dist"),
                    help="directory containing the built wheel")
    ap.add_argument("--venv", default=str(
        Path("~/workspace/venvs/agentkai-smoke").expanduser()),
        help="scratch venv to create (deleted first if present)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the venv afterwards for debugging")
    args = ap.parse_args()

    wheels = sorted(Path(args.dist).glob("agentkai-*.whl"))
    if not wheels:
        print(f"FAIL: no agentkai wheel in {args.dist} — build first "
              "(python -m build)")
        return 1
    wheel = wheels[-1]
    venv = Path(args.venv)
    if venv.exists():
        if "agentkai-smoke" not in venv.name:
            print(f"FAIL: refusing to delete unexpected path {venv}")
            return 1
        shutil.rmtree(venv)
    home = venv.parent / (venv.name + "-home")
    if home.exists():
        shutil.rmtree(home)

    print(f"[smoke] creating clean venv at {venv}")
    run([sys.executable, "-m", "venv", str(venv)], timeout=300)
    pip = venv / "bin" / "pip"
    agentkai = venv / "bin" / "agentkai"
    print(f"[smoke] installing {wheel.name} (deps resolve from PyPI)")
    out = run([str(pip), "install", "--quiet", str(wheel)], timeout=900)
    if not agentkai.exists():
        print("FAIL: agentkai entry point missing after install")
        print(out[-2000:])
        return 1

    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    env["AGENTKAI_HOME"] = str(home)

    failures = []

    print("[smoke] agentkai doctor")
    out = run([str(agentkai), "doctor"], env=env, timeout=300)
    if "[FAIL] litellm" in out or "litellm" not in out:
        failures.append("doctor: model-layer (litellm) check did not pass")
        print(out[-2000:])

    print("[smoke] agentkai run (no keys — must reach the model stage)")
    out = run([str(agentkai), "run", "smoke test"], env=env, timeout=600)
    if "No module named" in out:
        failures.append("run: died on a missing module (packaging bug)")
        print(out[-2000:])
    elif "all models failed" not in out:
        failures.append("run: did not reach the model fallback chain — "
                        "unexpected output")
        print(out[-2000:])

    if not args.keep:
        shutil.rmtree(venv, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("[smoke] PASS: fresh install imports cleanly and reaches the "
          "model stage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
