#!/usr/bin/env python3
"""Bootstrap a cloned NRG AI Laboratory repository into a local Python venv.

This script deliberately installs only the repository's Python layer.  It does
not install Pi, Node.js, compilers, or NRG itself.  Those external prerequisites
are diagnosed afterwards by ``nrg-lab-doctor``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import subprocess
import sys


MIN_PYTHON = (3, 11)


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(command: list[str], *, cwd: Path, dry_run: bool) -> None:
    print("+", " ".join(command))
    if dry_run:
        return
    subprocess.run(command, cwd=cwd, check=True)


def _check_python(interpreter: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    completed = subprocess.run(
        [
            interpreter,
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3]))); "
            "raise SystemExit(0 if sys.version_info >= (3, 11) else 3)",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        version = completed.stdout.strip() or "unknown"
        raise SystemExit(f"Python 3.11 or newer is required; {interpreter} reports {version}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", default=".venv", help="virtual-environment directory (default: .venv)")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used to create the venv")
    parser.add_argument("--dev", action="store_true", help="install the optional test dependency set")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="disable PEP 517 build isolation; requires suitable setuptools already available",
    )
    parser.add_argument(
        "--init-local-config",
        action="store_true",
        help="create config/laboratory.local.toml from the example after installation",
    )
    parser.add_argument("--skip-doctor", action="store_true", help="do not run environment diagnostics")
    parser.add_argument("--dry-run", action="store_true", help="show commands without changing the checkout")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _check_python(args.python, dry_run=args.dry_run)

    repo = Path(__file__).resolve().parent.parent
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        raise SystemExit(f"pyproject.toml not found at repository root: {repo}")

    venv = Path(args.venv).expanduser()
    if not venv.is_absolute():
        venv = repo / venv
    venv = venv.resolve()
    venv_python = _venv_python(venv)

    try:
        if not venv_python.is_file():
            _run([args.python, "-m", "venv", str(venv)], cwd=repo, dry_run=args.dry_run)

        install_target = ".[dev]" if args.dev else "."
        install_command = [str(venv_python), "-m", "pip", "install"]
        if args.offline:
            install_command.append("--no-build-isolation")
        install_command.extend(["-e", install_target])
        _run(install_command, cwd=repo, dry_run=args.dry_run)

        if args.init_local_config:
            local_config = repo / "config" / "laboratory.local.toml"
            if local_config.exists():
                print(f"Preserving existing local configuration: {local_config}")
            else:
                _run(
                    [str(venv_python), "-m", "nrg_analysis.config_cli", "init-local"],
                    cwd=repo,
                    dry_run=args.dry_run,
                )

        if not args.skip_doctor:
            _run([str(venv_python), "-m", "nrg_analysis.doctor"], cwd=repo, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"Bootstrap command failed with exit code {exc.returncode}.", file=sys.stderr)
        if args.offline:
            print(
                "Offline mode requires the selected environment to already contain "
                "a setuptools version satisfying pyproject.toml.",
                file=sys.stderr,
            )
        return exc.returncode or 1
    except OSError as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 2

    print(f"Bootstrap complete. Python: {venv_python}")
    if os.name == "nt":
        print(f"Activate with: {venv / 'Scripts' / 'activate'}")
    else:
        print(f"Activate with: source {venv / 'bin' / 'activate'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
