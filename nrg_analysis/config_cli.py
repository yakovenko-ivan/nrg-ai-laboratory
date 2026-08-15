"""Configuration utility for the portable NRG AI Laboratory checkout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any

from .laboratory import Laboratory, LOCAL_CONFIG_NAME


FALLBACK_LOCAL_TEMPLATE = """# Machine-local overrides for config/laboratory.toml.
# NRG is an external dependency of this repository.

[paths]
# campaign_root = "${NRG_CAMPAIGN_ROOT}"
# runs_root = "${NRG_RUNS_ROOT}"
# studies_root = "${NRG_STUDIES_ROOT}"
# task_setup_template = "${NRG_SOURCE_ROOT}/package_interface/task_setup"

[runtime]
# computing_module = "${NRG_BUILD_ROOT}/bin/computing_module"
# package_interface_0d = "${NRG_PACKAGE_INTERFACE_0D}"

[execution]
# default_threads = 1
"""


def _base_path(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else Laboratory.default_config_path()


def _load(args: argparse.Namespace, *, validate: bool, require_runtime: bool) -> Laboratory:
    return Laboratory.load(
        args.laboratory,
        local_path=args.local,
        validate=validate,
        require_runtime=require_runtime,
    )


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_init_local(args: argparse.Namespace) -> int:
    base = _base_path(args.laboratory)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else base.with_name(LOCAL_CONFIG_NAME).resolve()
    )
    if output.exists() and not args.force:
        raise FileExistsError(f"local configuration already exists: {output}")

    example = base.with_name(f"{LOCAL_CONFIG_NAME}.example")
    output.parent.mkdir(parents=True, exist_ok=True)
    if example.is_file():
        shutil.copyfile(example, output)
        source = str(example)
    else:
        output.write_text(FALLBACK_LOCAL_TEMPLATE, encoding="utf-8")
        source = "built-in template"

    _emit({"created": str(output), "source": source, "base_config": str(base)})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    lab = _load(args, validate=False, require_runtime=False)
    _emit({"laboratory": lab.to_dict()})
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    lab = _load(args, validate=True, require_runtime=not args.no_runtime)
    _emit(
        {
            "ok": True,
            "runtime_required": not args.no_runtime,
            "laboratory": lab.to_dict(),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--laboratory",
        help="base laboratory TOML; otherwise NRG_LABORATORY_CONFIG or repository default",
    )
    parser.add_argument(
        "--local",
        help="explicit local override TOML; otherwise NRG_LABORATORY_LOCAL_CONFIG or sibling local file",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-local", help="create a machine-local override file")
    init.add_argument("--output", help="output path; defaults beside the base configuration")
    init.add_argument("--force", action="store_true", help="replace an existing local configuration")
    init.set_defaults(func=cmd_init_local)

    show = sub.add_parser("show", help="print the fully resolved effective configuration")
    show.set_defaults(func=cmd_show)

    validate = sub.add_parser("validate", help="validate configuration and trusted runtime paths")
    validate.add_argument(
        "--no-runtime",
        action="store_true",
        help="validate repository/configuration paths without requiring NRG executables",
    )
    validate.set_defaults(func=cmd_validate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
