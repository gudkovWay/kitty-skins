#!/usr/bin/env python3
"""Inspect or stage an immutable bamboo02 bundle; never activate a desktop."""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(repo):
    pack = Path(repo) / "assets/skins/bamboo-02"
    manifest = json.loads((pack / "pack.json").read_text())
    if manifest.get("id") != "bamboo-02" or manifest.get("renderer") != "bamboo02":
        raise ValueError("Not a bamboo02 renderer pack")
    if not manifest.get("assets"):
        raise ValueError("Pack has no assets")
    for name, details in manifest["assets"].items():
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"Invalid asset path: {name}")
        path = pack / relative
        if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(pack)):
            raise ValueError(f"Symlink asset path: {name}")
        if not path.is_file():
            raise ValueError(f"Missing asset: {name}")
        if digest(path) != details["sha256"]:
            raise ValueError(f"Asset digest mismatch: {name}")
    return manifest


def hyprland_config(pack):
    # ensure_ascii=False avoids JSON-only \u escapes in Lua path strings.
    root = json.dumps(str(pack), ensure_ascii=False)
    return (
        "-- Explicit opt-in only: load libbamboo-02.so before calling apply().\n"
        "-- After apply(): hl.dispatch(hl.plugin.bamboo02.reload()).\n"
        "-- Before unload: hl.dispatch(hl.plugin.bamboo02.restore('all')).\n"
        "-- Do not target this Kitty with another frame/weather plugin.\n"
        "local M = {}\n"
        "function M.apply()\n"
        "    assert(hl.plugin.bamboo02 ~= nil, 'bamboo02 plugin is not loaded')\n"
        "    -- The plugin owns exact source blur; avoid blurring its lanterns twice.\n"
        "    hl.window_rule({match = {class = '^(kitty)$'}, no_blur = true})\n"
        "    hl.config({plugin = {bamboo02 = {\n"
        f"        root = {root},\n"
        "        target = 'kitty', enabled = 1, reduced_motion = 0\n"
        "    }}})\n"
        "end\nreturn M\n"
    )


def _publish(parent_fd, scratch_name, target_name):
    """Publish once using Linux renameat2; never replace a public pathname."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "Atomic no-replace publication is unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(scratch_name), parent_fd, os.fsencode(target_name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target_name)


def stage(repo, plugin, destination, header_hash):
    repo, plugin = Path(repo).resolve(), Path(plugin)
    requested = Path(destination).expanduser().absolute()
    target = requested.parent.resolve() / requested.name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing destination: {target}")
    manifest = inspect(repo)
    if plugin.is_symlink() or not plugin.is_file():
        raise FileNotFoundError(f"Build the native plugin first: {plugin}")
    plugin_hash = digest(plugin)
    shaders = repo / "src/bamboo/shaders"
    if not shaders.is_dir() or not any(shaders.glob("*.frag")):
        raise FileNotFoundError(f"Native shader sources missing: {shaders}")
    source_pack = repo / "assets/skins/bamboo-02"
    for root in (source_pack, shaders):
        if target.parent.is_relative_to(root.resolve()):
            raise ValueError("Destination is inside a copied input directory")
        for item in root.rglob("*"):
            if item.is_symlink():
                raise ValueError(f"Source contains symlink: {item}")
    target.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    scratch = None
    published = False
    try:
        actual_parent = Path(f"/proc/self/fd/{parent_fd}").resolve()
        if any(actual_parent.is_relative_to(root.resolve()) for root in (source_pack, shaders)):
            raise ValueError("Destination is inside a copied input directory")
        # All publication and cleanup are relative to this stable directory fd.
        # The public destination does not exist until the atomic no-replace move.
        scratch = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=f"/proc/self/fd/{parent_fd}"))
        shutil.copytree(source_pack, scratch / "pack")
        shutil.copytree(shaders, scratch / "pack/shaders")
        shutil.copy2(plugin, scratch / "libbamboo-02.so")
        copied_manifest = json.loads((scratch / "pack/pack.json").read_text())
        if copied_manifest != manifest:
            raise ValueError("Source manifest changed during staging")
        for name, details in manifest["assets"].items():
            if digest(scratch / "pack" / name) != details["sha256"]:
                raise ValueError(f"Staged asset digest mismatch: {name}")
        if digest(scratch / "libbamboo-02.so") != plugin_hash:
            raise ValueError("Plugin digest changed during staging")
        (scratch / "hyprland.lua").write_text(hyprland_config(target / "pack"))
        result = {
            "id": manifest["id"], "destination": str(target),
            "activation": "not-applied", "hyprland_header_hash": header_hash,
            "source_plugin_sha256": plugin_hash,
            "assets": {name: details["sha256"] for name, details in manifest["assets"].items()},
            "rollback": "Call hl.dispatch(hl.plugin.bamboo02.restore('all')), then unload only this bundle's plugin; staging changed no existing configuration.",
            "files": {},
        }
        result["files"] = {
            str(path.relative_to(scratch)): digest(path)
            for path in sorted(scratch.rglob("*")) if path.is_file()
        }
        (scratch / "bundle.json").write_text(json.dumps(result, indent=2) + "\n")
        _publish(parent_fd, scratch.name, target.name)
        published = True
        return result
    finally:
        if scratch is not None and not published:
            shutil.rmtree(scratch.name, dir_fd=parent_fd)
        os.close(parent_fd)


def build_identity(header):
    text = Path(header).read_text()
    match = re.search(r'^#define\s+GIT_COMMIT_HASH\s+"([a-f0-9]+)"', text, re.MULTILINE)
    if not match:
        raise ValueError("Cannot identify installed Hyprland headers")
    return match.group(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    install = sub.add_parser("stage")
    install.add_argument("destination", type=Path)
    install.add_argument("--plugin", type=Path, required=True)
    install.add_argument("--headers", type=Path, default=Path("/usr/include/hyprland/src/version.h"))
    args = parser.parse_args(argv)
    try:
        result = inspect(args.repo) if args.command == "inspect" else stage(
            args.repo, args.plugin, args.destination, build_identity(args.headers))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"bamboo02: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
