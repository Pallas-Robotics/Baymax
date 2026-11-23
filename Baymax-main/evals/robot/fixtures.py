"""Rig fixtures: clips and images the timeline plays or shows, checked by SHA-256 before a run.

Fixtures are files the rig cannot generate itself (fall clips, recorded synthetic
voices, photos of objects). They live outside the repository, in `fixtures_dir` from
the rig config (default `~/.local/share/ember/rig-fixtures`), never in Git: a clip of
an actor falling is a recording of a person. `manifest.json` in that folder records
each file's SHA-256 and size:

    python evals/robot/fixtures.py lock  ~/.local/share/ember/rig-fixtures   # after adding files
    python evals/robot/fixtures.py check ~/.local/share/ember/rig-fixtures

Before a run the rig verifies every fixture a planned scenario uses: the file must be
in the manifest and match it, and match a `sha256` pinned in scenarios.json if the cue
has one. Anything else is a rig fault and nothing starts. The report records the
manifest's own SHA-256 so two runs can be compared. Standard library only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

MANIFEST = "manifest.json"
DEFAULT_DIR = "~/.local/share/ember/rig-fixtures"


class FixtureError(Exception):
    """A fixture is missing, unlisted or changed: a rig fault, not a product failure."""


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def folder_of(config):
    return Path(config.get("fixtures_dir") or DEFAULT_DIR).expanduser()


def lock(folder):
    """Write manifest.json for every file in `folder` (not recursive). Returns the manifest."""
    folder = Path(folder)
    entries = {path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
               for path in sorted(folder.iterdir()) if path.is_file() and path.name != MANIFEST
               and not path.name.startswith(".")}
    (folder / MANIFEST).write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entries


def load_manifest(folder):
    path = Path(folder) / MANIFEST
    if not path.is_file():
        raise FixtureError(f"no {MANIFEST} in {folder} (run: python evals/robot/fixtures.py lock {folder})")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise FixtureError(f"{path} is not valid JSON: {error}") from None


def manifest_digest(folder):
    path = Path(folder) / MANIFEST
    return sha256_file(path) if path.is_file() else None


def verify(folder, wanted):
    """{name: Path} for `wanted` ({name: pinned sha256 or None}); FixtureError lists every problem."""
    if not wanted:
        return {}
    folder = Path(folder)
    manifest = load_manifest(folder)
    problems, paths = [], {}
    for name, pinned in sorted(wanted.items()):
        if Path(name).name != name:
            problems.append(f"{name}: fixture names are plain file names")
            continue
        entry, path = manifest.get(name), folder / name
        if entry is None:
            problems.append(f"{name}: not in {MANIFEST}")
        elif not path.is_file():
            problems.append(f"{name}: listed but missing from {folder}")
        else:
            actual = sha256_file(path)
            if actual != entry.get("sha256"):
                problems.append(f"{name}: SHA-256 {actual[:12]}… does not match the manifest's "
                                f"{str(entry.get('sha256'))[:12]}… (file changed since it was locked)")
            elif pinned and actual != pinned:
                problems.append(f"{name}: SHA-256 {actual[:12]}… does not match the {pinned[:12]}… pinned in "
                                "scenarios.json")
            else:
                paths[name] = path
    if problems:
        raise FixtureError("; ".join(problems))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lock or check the robot rig's fixture checksums")
    parser.add_argument("action", choices=("lock", "check"))
    parser.add_argument("folder", type=Path, nargs="?", default=Path(DEFAULT_DIR).expanduser())
    args = parser.parse_args(argv)
    if args.action == "lock":
        entries = lock(args.folder)
        print(f"Locked {len(entries)} fixtures in {args.folder / MANIFEST}")
        return 0
    try:
        manifest = load_manifest(args.folder)
        verify(args.folder, {name: None for name in manifest})
    except FixtureError as error:
        print(f"Fixture check failed: {error}", file=sys.stderr)
        return 1
    print(f"{len(manifest)} fixtures match {args.folder / MANIFEST} (manifest {manifest_digest(args.folder)[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
