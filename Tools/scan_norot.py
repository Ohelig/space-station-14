#!/usr/bin/env python3
"""
scan_norot.py — Find entity prototypes with suspicious `noRot: true` sprite settings.

Background
----------
PR #5425 changed construction ghost rotation to respect SpriteComponent.NoRotation.
This means any prototype with `noRot: true` whose sprite art is actually directional
(e.g. has 4-direction states) will have a broken non-rotating construction ghost.

This script scans all YAML entity prototypes under Resources/Prototypes/, finds
those with `noRot: true` on a Sprite component, then inspects the referenced
RSI meta.json to determine whether the sprite asset is directional.

Output
------
A plaintext report of candidate prototypes to review.  The script does NOT
modify any game data files.

Flags per candidate
-------------------
  CONFIRMED  — RSI meta.json was found and at least one state has directions > 1
  HEURISTIC  — RSI file was found but meta.json could not be read, or sprite
               reference is present but the RSI directory is missing

Usage
-----
    # From the repository root:
    python Tools/scan_norot.py

    # Narrow to a sub-tree:
    python Tools/scan_norot.py --proto-root Resources/Prototypes/_Starlight

    # Output only CONFIRMED matches:
    python Tools/scan_norot.py --confirmed-only

    # Output as CSV (id, file, sprite, reason):
    python Tools/scan_norot.py --csv
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Generator, NamedTuple

# ── YAML parsing ─────────────────────────────────────────────────────────────
# The game's YAML uses custom tags (!type:...) that standard parsers reject.
# We do a line-oriented scan instead of full YAML parsing to stay dependency-free.


class Candidate(NamedTuple):
    proto_id: str
    file_path: str
    sprite_path: str  # e.g. "_Starlight/Structures/Furniture/Chairs/comfy_chair.rsi"
    flag: str         # "CONFIRMED" or "HEURISTIC"
    reason: str


# ── Prototype scanner ─────────────────────────────────────────────────────────

def iter_yaml_files(root: Path) -> Generator[Path, None, None]:
    for path in root.rglob("*.yml"):
        yield path


def _parse_prototypes(text: str) -> list[dict]:
    """
    Very lightweight YAML block splitter.
    Splits on top-level `- type: entity` and returns a list of dicts with
    keys: 'id', 'raw_block' (the raw text of the prototype block).
    """
    protos = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("- type: entity"):
            if current:
                protos.append(current)
            current = [line]
        elif line.startswith("- type:") and current:
            # A new top-level prototype of a different type — end the current block
            protos.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        protos.append(current)

    result = []
    for block_lines in protos:
        block = "".join(block_lines)
        if "type: entity" not in block:
            continue
        proto_id = _extract_field(block_lines, "id:")
        result.append({"id": proto_id, "raw_block": block, "lines": block_lines})
    return result


def _extract_field(lines: list[str], key: str) -> str:
    """Return the value of a simple `key: value` line, first match wins."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(key):
            return stripped[len(key):].strip()
    return ""


def _find_sprite_components(block_lines: list[str]) -> list[dict]:
    """
    Find all `- type: Sprite` component blocks within a prototype block.
    Returns list of dicts with keys: 'noRot', 'sprite_paths'.
    """
    sprite_components: list[dict] = []
    in_sprite = False
    indent_sprite = -1
    current_sprite: dict | None = None
    # We look for `- type: Sprite` blocks inside the prototype.
    # We assume the components list is indented, and each list item starts with `- type:`.

    for line in block_lines:
        stripped = line.strip()
        # Detect start of a Sprite component
        if stripped == "- type: Sprite":
            indent_sprite = len(line) - len(line.lstrip())
            in_sprite = True
            current_sprite = {"noRot": False, "sprite_paths": []}
            sprite_components.append(current_sprite)
            continue

        if in_sprite and current_sprite is not None:
            # Detect end of this component block (same or lesser indent level with `- type:`)
            line_indent = len(line) - len(line.lstrip()) if line.strip() else indent_sprite + 1
            if stripped.startswith("- type:") and line_indent <= indent_sprite:
                in_sprite = False
                current_sprite = None
                # But if this line starts a NEW Sprite, handle it:
                if stripped == "- type: Sprite":
                    indent_sprite = line_indent
                    in_sprite = True
                    current_sprite = {"noRot": False, "sprite_paths": []}
                    sprite_components.append(current_sprite)
                continue

            # noRot flag
            if "noRot: true" in stripped:
                current_sprite["noRot"] = True

            # sprite: path.rsi
            if stripped.startswith("sprite:") and stripped.endswith(".rsi"):
                path_val = stripped[len("sprite:"):].strip()
                current_sprite["sprite_paths"].append(path_val)

    return sprite_components


# ── RSI metadata inspector ────────────────────────────────────────────────────

def _check_rsi_directional(rsi_path: str, textures_root: Path) -> tuple[str, str]:
    """
    Given an RSI path like `_Starlight/Structures/.../foo.rsi`, look for its
    meta.json under textures_root.

    Returns (flag, reason):
      ("CONFIRMED", reason)  if meta.json found and any state has directions > 1
      ("NONE", reason)       if meta.json found but no directional states
      ("HEURISTIC", reason)  if RSI dir found but meta.json is missing/unreadable
      ("MISSING", reason)    if RSI dir not found at all
    """
    rsi_dir = textures_root / rsi_path
    meta_file = rsi_dir / "meta.json"

    if not rsi_dir.exists():
        return "MISSING", f"RSI directory not found: {rsi_dir}"

    if not meta_file.exists():
        return "HEURISTIC", f"meta.json missing in {rsi_dir}"

    try:
        with open(meta_file, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return "HEURISTIC", f"Could not read meta.json ({exc})"

    states = meta.get("states", [])
    directional_states = [
        s.get("name", "?") for s in states if int(s.get("directions", 1)) > 1
    ]
    if directional_states:
        return "CONFIRMED", (
            f"directions > 1 in states: {', '.join(directional_states)}"
        )
    return "NONE", "All states have directions=1 (not directional)"


# ── Main scan ─────────────────────────────────────────────────────────────────

def scan(
    proto_root: Path,
    textures_root: Path,
    confirmed_only: bool = False,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    for yaml_file in sorted(iter_yaml_files(proto_root)):
        try:
            text = yaml_file.read_text(encoding="utf-8")
        except OSError:
            continue

        prototypes = _parse_prototypes(text)
        for proto in prototypes:
            proto_id = proto["id"] or "<unknown>"
            sprite_components = _find_sprite_components(proto["lines"])

            for comp in sprite_components:
                if not comp["noRot"]:
                    continue

                sprite_paths = comp["sprite_paths"]
                if not sprite_paths:
                    # noRot: true but no explicit sprite: path we can inspect
                    if not confirmed_only:
                        candidates.append(Candidate(
                            proto_id=proto_id,
                            file_path=str(yaml_file),
                            sprite_path="<no explicit sprite path>",
                            flag="HEURISTIC",
                            reason="noRot: true but sprite path not parsed from block",
                        ))
                    continue

                for sp in sprite_paths:
                    flag, reason = _check_rsi_directional(sp, textures_root)
                    if flag == "CONFIRMED" or (not confirmed_only and flag == "HEURISTIC"):
                        candidates.append(Candidate(
                            proto_id=proto_id,
                            file_path=str(yaml_file),
                            sprite_path=sp,
                            flag=flag,
                            reason=reason,
                        ))

    return candidates


# ── Output formatters ─────────────────────────────────────────────────────────

def print_report(candidates: list[Candidate]) -> None:
    if not candidates:
        print("No suspicious prototypes found.")
        return

    print(f"Found {len(candidates)} candidate(s) with suspicious noRot: true\n")
    print("=" * 72)
    for c in candidates:
        print(f"[{c.flag}] {c.proto_id}")
        print(f"  File   : {c.file_path}")
        print(f"  Sprite : {c.sprite_path}")
        print(f"  Reason : {c.reason}")
        print()


def print_csv(candidates: list[Candidate]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["flag", "proto_id", "file_path", "sprite_path", "reason"])
    for c in candidates:
        writer.writerow([c.flag, c.proto_id, c.file_path, c.sprite_path, c.reason])


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory)",
    )
    parser.add_argument(
        "--proto-root",
        default=None,
        help="Sub-path under repo-root to scan for prototypes "
             "(default: Resources/Prototypes)",
    )
    parser.add_argument(
        "--textures-root",
        default=None,
        help="Sub-path under repo-root that contains RSI assets "
             "(default: Resources/Textures)",
    )
    parser.add_argument(
        "--confirmed-only",
        action="store_true",
        help="Only report CONFIRMED cases (RSI meta.json found with directions > 1)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        dest="csv_output",
        help="Output results as CSV instead of a human-readable report",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    proto_root = Path(args.proto_root) if args.proto_root else repo_root / "Resources" / "Prototypes"
    textures_root = Path(args.textures_root) if args.textures_root else repo_root / "Resources" / "Textures"

    if not proto_root.exists():
        print(f"ERROR: Prototype directory not found: {proto_root}", file=sys.stderr)
        return 1
    if not textures_root.exists():
        print(f"ERROR: Textures directory not found: {textures_root}", file=sys.stderr)
        return 1

    candidates = scan(proto_root, textures_root, confirmed_only=args.confirmed_only)

    if args.csv_output:
        print_csv(candidates)
    else:
        print_report(candidates)

    return 0


if __name__ == "__main__":
    sys.exit(main())
