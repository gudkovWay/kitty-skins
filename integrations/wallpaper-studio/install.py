#!/usr/bin/env python3
"""Установка Wallpaper Studio: бэкенд, ресурсы, плагин, потребители, лаунчер, каталог.

Копируются ТОЛЬКО поимённо перечисленные файлы; чужое не трогается. Замена
файла — атомарная (tmp + os.replace), перезаписанное имя предварительно
резервируется рядом с целью (*.bak.<UTC>). Запись в каталог плагинов —
идемпотентный дословный блок q/wallpaper-studio.

Потребители (копии реальных установленных источников, ставятся явно):
  consumers/catalog.py           → каталог обоев wallpaper-context;
                                   назначение — $WALLPAPER_CONTEXT_SCRIPTS,
                                   иначе ~/.omp/agent/skills/wallpaper-context/scripts
  consumers/w-engine-effects.luau→ сервис эффектов;
                                   назначение —
                                   <XDG_CONFIG_HOME>/hypr/noctalia-plugins/w-engine-effects/service.luau

Все назначения резолвят XDG (XDG_CONFIG_HOME, XDG_DATA_HOME), поэтому
изолированная установка в песочницу — это просто запуск с этими переменными,
указывающими в песочницу; живые файлы при этом не пишутся вовсе. Назначение
лаунчера переопределяется $WALLPAPER_STUDIO_LAUNCHER (изолированный абсолютный
путь песочницы), иначе это <XDG_DATA_HOME>/../bin/wallpaper-studio.
NOCTALIA_STATE_HOME установщик только резолвит и отчёт о нём включает: сам он
ничего в state не пишет.

    install.py [--dry-run]

Ничего не включает и не перезагружает: enable/disable плагинов, виджет в бар,
запуск рантайма и итоговый переключатель — за человеком (родительский сценарий;
инструкция печатается в конце, JSON-ом). Зависимости не ставятся. Предполёт:
все поимённые исходники обязаны существовать ДО первой записи; не хватает —
установка не начинается, код возврата 1.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent

BACKEND_FILES = [
    "studio.py", "storage.py", "process.py",
    "context.py", "generator.py", "library.py",
    "playback.py", "capture.py", "capture_scene.py", "profiles.py",
    "frame_preview.py", "viewer.py", "reset.py",
    # Measured source opening, durable attempt ledger and the offline repair CLI.
    "opening.py", "usage.py", "reassemble.py",
    # Reference-led design pipeline: shared design identity, the semantic frame
    # decomposition, the design-target metadata/boundary and the frozen authored
    # guidance consumed by every art-direction/image-generation stage.
    "art_direction.py", "semantic_frame.py", "designs.py", "frame_guidance.py",
]
RESOURCE_DIR = SRC.parent.parent / "assets" / "skins" / "gothic-eclipse"
RESOURCE_FILES = [
    "exact.png", "candle-flames.png", "candle-light.png",
    "candle-wax-mask.png", "source.json", "skin.json",
]
# Bundled exact snapshot of the authored Kitty frame-authoring documents. The
# backend reads the canonical skill directory when it exists and falls back to
# this installed copy otherwise, so a machine without ~/.omp still gets guidance.
FRAME_GUIDANCE_DIR = SRC / "resources" / "frame-guidance"
FRAME_GUIDANCE_FILES = [
    "SKILL.md",
    "references/quality-gates.md",
    "references/gothic-philosophy.md",
    "references/image-generation-rules.md",
]
PLUGIN_FILES = ["plugin.toml", "panel.luau", "widget.luau", "service.luau"]
CONSUMER_FILES = ["catalog.py", "w-engine-effects.luau"]
THEME_DIR = SRC.parent / "noctalia-theme"
THEME_ASSETS = [
    "corner-tl.png", "corner-tr.png", "corner-bl.png", "corner-br.png",
    "rail-top.png", "rail-bottom.png", "rail-left.png", "rail-right.png",
    "emblem.png",
]

HOME = Path(os.environ.get("HOME", str(Path.home())))

CATALOG_BLOCK = """
[[plugin]]
id = "q/wallpaper-studio"
name = "Wallpaper Studio"
version = "2.0.0"
updated_at = {updated}
added_at = {updated}
author = "q"
license = "MIT"
icon = "image"
description = "Генерация рамок под обои: разбор метаданных, кандидаты, одобрение и применение."
deprecated = false
plugin_api = 22
tags = ["wallpaper", "theming"]
"""


# ------------------------------------------------------------------- назначения


def _xdg_dir(env_var: str, default: Path) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw and os.path.isabs(raw):
        return Path(raw)
    return default


def xdg_config_home() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", HOME / ".config")


def xdg_data_home() -> Path:
    return _xdg_dir("XDG_DATA_HOME", HOME / ".local" / "share")


def context_scripts_dir() -> Path:
    """Каталог помощника wallpaper-context: $WALLPAPER_CONTEXT_SCRIPTS побеждает."""
    raw = os.environ.get("WALLPAPER_CONTEXT_SCRIPTS", "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return HOME / ".omp" / "agent" / "skills" / "wallpaper-context" / "scripts"


def destinations() -> dict:
    """Все назначения установки, резолвленные под текущее окружение."""
    config = xdg_config_home()
    data = xdg_data_home()
    # Явный $WALLPAPER_STUDIO_LAUNCHER (изолированный путь песочницы) бьёт
    # XDG-дефолт: живой ~/.local/bin при установке в песочницу не трогается.
    raw_launcher = os.environ.get("WALLPAPER_STUDIO_LAUNCHER", "").strip()
    launcher = (
        Path(os.path.abspath(os.path.expanduser(raw_launcher)))
        if raw_launcher
        else data.parent / "bin/wallpaper-studio"
    )
    return {
        "backend": data / "wallpaper-studio/backend",
        "resources": data / "wallpaper-studio/resources/gothic-eclipse",
        "frame_guidance": data / "wallpaper-studio/resources/frame-guidance",
        "plugin": config / "hypr/noctalia-plugins/wallpaper-studio",
        "launcher": launcher,
        "catalog": config / "hypr/noctalia-plugins/catalog.toml",
        "context_scripts": context_scripts_dir(),
        "effects": config / "hypr/noctalia-plugins/w-engine-effects/service.luau",
    }


# ---------------------------------------------------------------------- запись


def backup(path: Path, dry: bool) -> Path | None:
    """Резервная копия ПЕРЕЗАПИСЫВАЕМОГО имени: копия, не перенос — оригинал
    на месте до самой атомарной замены. Для несуществующего — None."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(path.name + ".bak." + stamp)
    if not dry:
        shutil.copy2(path, target)
    return target


def place(src: Path, dst: Path, dry: bool) -> dict:
    """Атомарная замена: копия во временный файл рядом, затем os.replace."""
    record: dict = {"src": str(src), "dst": str(dst)}
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            kept = backup(dst, dry)
            record["backup"] = str(kept) if kept else None
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix="." + dst.name + ".")
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as orig:
                out.write(orig.read())
            os.chmod(tmp, os.stat(src).st_mode & 0o777)
            os.replace(tmp, dst)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    else:
        record["backup"] = str(dst) if dst.exists() else None
    record["ok"] = True
    return record


def _plugin_blocks(text: str) -> list[tuple[int, int, str]]:
    """(start, end, block id) spans of every top-level [[plugin]] block."""
    spans = []
    starts = [match.start() for match in re.finditer(r"^\[\[plugin\]\][ \t]*$", text, re.M)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        body = text[start:end]
        id_match = re.search(r'^id\s*=\s*"([^"]*)"', body, re.M)
        spans.append((start, end, id_match.group(1) if id_match else ""))
    return spans


def install_catalog(catalog: Path, dry: bool) -> dict:
    """Идемпотентный дописывающий блок: ищем точное `id = "q/wallpaper-studio"`.

    Найденный блок сверяется с текущим манифестом плагина (plugin.toml):
    совпадают имя/версия/описание и т.п. — «уже стоит», ничего не пишем.
    Устаревший или расходящийся именованный блок безопасно заменяется целиком,
    но только свой: соседние блоки и остальной файл не трогаются, старый
    added_at сохраняется, updated_at ставится заново.
    """
    manifest = tomllib.loads((SRC / "plugin" / "plugin.toml").read_text(encoding="utf-8"))
    record: dict = {"dst": str(catalog)}
    if catalog.exists():
        text = catalog.read_text(encoding="utf-8")
        for start, end, block_id in _plugin_blocks(text):
            if block_id != manifest["id"]:
                continue
            body = text[start:end]
            stale = not all(
                re.search(rf"^{re.escape(key)}\s*=\s*{re.escape(json.dumps(value))}\s*$", body, re.M)
                for key, value in (
                    ("name", manifest["name"]),
                    ("version", manifest["version"]),
                )
            )
            if not stale:
                record["action"] = "already-present"
                return record
            # Свой блок устарел: пересобираем только его. added_at прежний,
            # если читается, updated_at — сейчас.
            added = re.search(r"^added_at\s*=\s*(\d+)\s*$", body, re.M)
            added_at = int(added.group(1)) if added else int(datetime.now(timezone.utc).timestamp())
            updated = int(datetime.now(timezone.utc).timestamp())
            fresh = CATALOG_BLOCK.format(updated=updated).replace(
                f"added_at = {updated}", f"added_at = {added_at}", 1
            )
            text = text[:start] + fresh.rstrip("\n") + "\n" + text[end:]
            record["action"] = "updated-block"
            previous = re.search(r'^version\s*=\s*"([^"]*)"', body, re.M)
            record["previous_version"] = previous.group(1) if previous else None
            break
        else:
            record["action"] = "appended"
    else:
        text = "# Local plugin source. Point Noctalia at this directory with:\n" \
               "#   noctalia msg plugins source add local path ~/.config/hypr/noctalia-plugins\n"
        record["action"] = "appended"
    if record["action"] == "appended":
        updated = int(datetime.now(timezone.utc).timestamp())
        text = text.rstrip("\n") + "\n" + CATALOG_BLOCK.format(updated=updated)
    if not dry:
        catalog.parent.mkdir(parents=True, exist_ok=True)
        if catalog.exists():
            kept = backup(catalog, dry)
            record["backup"] = str(kept) if kept else None
        fd, tmp = tempfile.mkstemp(dir=str(catalog.parent), prefix="." + catalog.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(text)
            os.replace(tmp, catalog)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return record


def install_launcher(launcher: Path, backend_dst: Path, dry: bool) -> dict:
    # Desktop services do not inherit the interactive shell's user-tool PATH.
    # Keep executable entrypoint directories, not versioned symlink targets.
    tool_dirs = []
    for name in ("omp", "bun", "terminal-skin"):
        executable = shutil.which(name)
        if executable:
            directory = str(Path(executable).absolute().parent)
            if directory not in tool_dirs:
                tool_dirs.append(directory)
    path_setup = (
        f'export PATH={shlex.quote(":".join(tool_dirs))}"${{PATH:+:$PATH}}"\n'
        if tool_dirs else ""
    )
    script = (
        "#!/bin/sh\n"
        "# Wallpaper Studio: CLI к фоновому воркеру бэкенда.\n"
        + path_setup
        + f'exec python3 -B "{backend_dst}/studio.py" "$@"\n'
    )
    record: dict = {"dst": str(launcher)}
    if not dry:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        if launcher.exists():
            kept = backup(launcher, dry)
            record["backup"] = str(kept) if kept else None
        fd, tmp = tempfile.mkstemp(dir=str(launcher.parent), prefix="." + launcher.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(script)
            os.chmod(tmp, 0o755)
            os.replace(tmp, launcher)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return record


def preflight() -> list[str]:
    """Все поимённые исходники должны существовать до первой записи."""
    wanted = (
        [(SRC / "backend" / f) for f in BACKEND_FILES]
        + [RESOURCE_DIR / f for f in RESOURCE_FILES]
        + [FRAME_GUIDANCE_DIR / f for f in FRAME_GUIDANCE_FILES]
        + [(SRC / "plugin" / f) for f in PLUGIN_FILES]
        + [(SRC / "consumers" / f) for f in CONSUMER_FILES]
        + [THEME_DIR / "desktop_skin.luau"]
        + [THEME_DIR / "skin-assets" / f for f in THEME_ASSETS]
    )
    return [str(p) for p in wanted if not p.exists()]


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    dsts = destinations()
    result: dict = {
        "dry_run": dry,
        "environment": {
            "XDG_CONFIG_HOME": str(xdg_config_home()),
            "XDG_DATA_HOME": str(xdg_data_home()),
            "NOCTALIA_STATE_HOME": os.environ.get("NOCTALIA_STATE_HOME") or None,
            "WALLPAPER_CONTEXT_SCRIPTS": os.environ.get("WALLPAPER_CONTEXT_SCRIPTS") or None,
            "WALLPAPER_STUDIO_LAUNCHER": os.environ.get("WALLPAPER_STUDIO_LAUNCHER") or None,
        },
        "destinations": {key: str(value) for key, value in dsts.items()},
        "installed": {},
        "catalog": None,
        "launcher": None,
    }
    missing = preflight()
    if missing:
        result["error"] = "preflight: отсутствуют исходники, ничего не записано"
        result["missing"] = missing
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    def group(name: str, files: list[Path], dst_dir: Path):
        placed = [place(src, dst_dir / src.name, dry) for src in files]
        result["installed"][name] = placed

    group("backend", [(SRC / "backend" / f) for f in BACKEND_FILES], dsts["backend"])
    group("resources", [RESOURCE_DIR / f for f in RESOURCE_FILES], dsts["resources"])
    # Guidance documents keep their original relative layout under
    # resources/frame-guidance (SKILL.md at the root, references/ below it).
    result["installed"]["frame_guidance"] = [
        place(FRAME_GUIDANCE_DIR / relative, dsts["frame_guidance"] / relative, dry)
        for relative in FRAME_GUIDANCE_FILES
    ]
    group("plugin", [(SRC / "plugin" / f) for f in PLUGIN_FILES], dsts["plugin"])
    result["installed"]["theme"] = [
        place(THEME_DIR / "desktop_skin.luau", dsts["plugin"] / "desktop_skin.luau", dry),
        *[place(THEME_DIR / "skin-assets" / f, dsts["plugin"] / "skin-assets" / f, dry)
          for f in THEME_ASSETS],
    ]
    result["installed"]["consumers"] = [
        place(SRC / "consumers/catalog.py", dsts["context_scripts"] / "catalog.py", dry),
        place(SRC / "consumers/w-engine-effects.luau", dsts["effects"], dry),
    ]
    result["catalog"] = install_catalog(dsts["catalog"], dry)
    result["launcher"] = install_launcher(dsts["launcher"], dsts["backend"], dry)

    result["enable_instructions"] = [
        "Источник плагинов уже должен указывать на ~/.config/hypr/noctalia-plugins:",
        "  noctalia msg plugins source add local path ~/.config/hypr/noctalia-plugins",
        "Включить плагин:",
        "  noctalia msg plugins enable q/wallpaper-studio",
        "виджет q/wallpaper-studio:studio-widget добавить в бар через настройки панели;",
        "панель открывается кликом по виджету: q/wallpaper-studio:studio",
        "старый tadomika_ari/w-engine и его виджет отключаются отдельной командой",
        "родительским сценарием переключения; установщик плагины не трогает.",
        "настройки.toml и конфиги Kitty установщик не меняет.",
    ]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
