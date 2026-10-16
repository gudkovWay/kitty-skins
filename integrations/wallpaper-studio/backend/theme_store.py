"""Wallpaper-owned design library. No model, renderer or shell execution here.

The operational index lives in .state; creative revisions are always contained
by their wallpaper. Paths supplied by models are relative, never executable.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

ID = re.compile(r'^[0-9a-f]{64}$')
NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,95}$')


class Conflict(ValueError):
    """The caller's frozen revision no longer matches the working copy."""


def root() -> Path:
    value = os.environ.get('WALLPAPER_STUDIO_DATA_DIR') or os.environ.get('THEME_HOME')
    return Path(value).expanduser().absolute() if value else Path.home() / '.theme'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def contained(base: Path, relative: str) -> Path:
    """Reject traversal and every symlink, including dangling intermediate links."""
    rel = Path(relative)
    if not relative or rel.is_absolute() or any(p in ('..', '.') for p in rel.parts):
        raise ValueError('asset path must be a contained relative path')
    base = Path(base).absolute()
    current = base
    if current.is_symlink():
        raise ValueError('library root must not be a symlink')
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('library paths must not traverse symlinks')
    if not current.resolve().is_relative_to(base.resolve()):
        raise ValueError('asset path escaped its owner')
    return current


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('wallpaper identity must be the full SHA-256 content id')
    return value


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError('name must be lowercase kebab/snake case, at most 96 characters')
    return value


def tree_revision(folder: Path) -> str:
    hashes = []
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError('snapshot assets must not be symlinks')
        if path.is_file() and path.name != 'revision.json':
            hashes.append((path.relative_to(folder).as_posix(), digest(path.read_bytes())))
    return digest(encoded(hashes))


class Store:
    def __init__(self, base: Path | None = None):
        self.root = Path(base) if base is not None else root()
        if self.root.is_symlink():
            raise ValueError('theme root must not be a symlink')

    @contextmanager
    def locked(self):
        lock = contained(self.root, '.state/library.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def initialize(self):
        with self.locked():
            for relative in ('design-rules', 'reference', 'memory/decisions', 'memory/lessons', 'helpers', 'wallpapers'):
                contained(self.root, relative).mkdir(parents=True, exist_ok=True)
            config = contained(self.root, 'theme.toml')
            if not config.exists():
                atomic(config, b'schema_version = 1\n# Creative history belongs to each wallpaper.\nwallpapers = "wallpapers"\nstate = ".state"\n\n[defaults]\nreference = "bambooc"\ntransfer = "principle"\n')

    def wallpaper(self, content_id: str, title: str = '') -> Path:
        content_id = identifier(content_id)
        with self.locked():
            path = contained(self.root, '.state/wallpapers.json')
            index = read(path, {})
            if content_id in index:
                folder = contained(self.root, index[content_id])
                manifest = read(folder / 'wallpaper.json')
                if not isinstance(manifest, dict) or manifest.get('content_id') != content_id:
                    raise ValueError('wallpaper index/manifest identity mismatch')
                return folder
            text = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode().lower()
            slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')[:56] or 'wallpaper'
            relative = f'wallpapers/{slug}-{content_id[:12]}'
            folder = contained(self.root, relative)
            if folder.exists():
                relative = f'wallpapers/{slug}-{content_id}'
                folder = contained(self.root, relative)
            folder.mkdir(parents=True)
            atomic(folder / 'wallpaper.json', encoded({'schema_version': 1, 'content_id': content_id, 'title': title, 'alias': folder.name}))
            index[content_id] = relative
            atomic(path, encoded(index))
            return folder

    def analysis_path(self, content_id):
        return contained(self.wallpaper(content_id), 'analysis.json')

    def generation_dir(self, content_id, generation_id):
        return contained(self.wallpaper(content_id), f'system-design/{name(generation_id)}/artifacts')

    def put(self, content_id, relative, value, *, expected):
        wp = self.wallpaper(content_id)
        path = contained(wp, relative)
        if relative.startswith('.history/'):
            raise ValueError('immutable history cannot be edited')
        payload = encoded(value)
        with self.locked():
            previous = path.read_bytes() if path.exists() else None
            revision = digest(previous) if previous is not None else None
            if revision != expected:
                raise Conflict(f'stale revision for {relative}: expected {expected}, current {revision}')
            if previous is not None:
                category = name(Path(relative).parts[0].split('.')[0])
                snapshot = contained(wp, f'.history/{category}/{revision}/{path.name}')
                if not snapshot.exists():
                    atomic(snapshot, previous)
            atomic(path, payload)
        return digest(payload)

    def accept_element(self, content_id, element, metadata, *, expected):
        element = name(element)
        wp = self.wallpaper(content_id)
        folder = contained(wp, f'shared/{element}')
        if not isinstance(metadata, dict):
            raise ValueError('element metadata must be an object')
        if metadata.get('kind') not in ('object', 'texture', 'ornament', 'mask', 'assembly', 'animation-layer'):
            raise ValueError('element kind is required')
        if not metadata.get('usage') or not metadata.get('roles'):
            raise ValueError('element needs explicit usage and foreground/background/structural roles')
        if metadata.get('animation') not in ('static', 'layered'):
            raise ValueError('element animation must be static or layered')
        if not isinstance(metadata.get('layers'), list):
            raise ValueError('element layers must be a list')
        if metadata['animation'] == 'layered' and not metadata['layers']:
            raise ValueError('layered motion requires authored layers and pivots')
        allowed = {'scale', 'crop', 'recolor', 'atlas', 'tile'}
        if not isinstance(metadata.get('adaptations'), list) or not set(metadata['adaptations']) <= allowed:
            raise ValueError('unknown deterministic adaptation')
        if metadata.get('unique') and 'tile' in metadata['adaptations']:
            raise ValueError('unique ornaments cannot be tiled')
        image = contained(folder, 'element.png')
        if not image.is_file():
            raise ValueError('element.png must exist before approval')
        for layer in metadata['layers']:
            if not isinstance(layer, dict) or not layer.get('pivot') or not layer.get('ownership'):
                raise ValueError('each animation layer needs a pivot and ownership')
            if not contained(folder, layer.get('file', '')).is_file():
                raise ValueError('animation layer asset is missing')
        with self.locked():
            old = read(folder / 'revision.json', {}).get('revision')
            if expected != old:
                raise Conflict('element approval conflicts with its accepted revision')
            atomic(folder / 'element.json', encoded(metadata))
            atomic(folder / 'usage.md', (metadata['usage'] + '\n').encode())
            revision = tree_revision(folder)
            pin = {'content_id': content_id, 'element': element, 'revision': revision}
            snapshot = contained(wp, f'.history/elements/{element}/{revision}')
            if not snapshot.exists():
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(folder, snapshot, ignore=shutil.ignore_patterns('revision.json'))
                atomic(snapshot / 'revision.json', encoded(pin))
            atomic(folder / 'revision.json', encoded(pin))
            return pin

    def resolve_pin(self, content_id, pin):
        if not isinstance(pin, dict) or pin.get('content_id') != identifier(content_id):
            raise ValueError('element pin belongs to another wallpaper')
        element, revision = name(pin['element']), identifier(pin['revision'])
        folder = contained(self.wallpaper(content_id), f'.history/elements/{element}/{revision}')
        if not folder.is_dir() or tree_revision(folder) != revision:
            raise ValueError('pinned element missing or modified')
        return folder

    def save_design(self, content_id, design_id, design, *, expected):
        design_id = name(design_id)
        if not isinstance(design, dict) or design.get('layout_mode') not in ('style-only', 'suggest-layout', 'auto-layout'):
            raise ValueError('design needs an explicit layout mode')
        if design.get('artistic_mode') not in ('explore', 'specification'):
            raise ValueError('design needs an independent artistic mode')
        if not design.get('rationale') or not design.get('wallpaper_facts'):
            raise ValueError('significant design choices must cite wallpaper facts and tradeoffs')
        topology = design.get('topology', {})
        if topology.get('bars') not in (1, 2) or topology.get('music') not in ('inside-bar', 'widget') or topology.get('ai') not in ('inside-bar', 'widget'):
            raise ValueError('design topology must specify bars, music and AI placement')
        palette = design.get('palette', {})
        if not all(re.fullmatch(r'#[0-9a-fA-F]{6}', palette.get(k, '')) for k in ('background', 'foreground', 'accent')):
            raise ValueError('global palette requires background, foreground and accent')
        apps = design.get('apps')
        if not isinstance(apps, dict) or not apps:
            raise ValueError('design needs target-specific representations')
        if apps.get('zen-browser', {}).get('motion', 'static') != 'static':
            raise ValueError('Zen frame exception forbids animation')
        for pin in design.get('elements', []):
            self.resolve_pin(content_id, pin)
        analysis_path = self.analysis_path(content_id)
        if analysis_path.is_file():
            design = {**design, 'source_context': {'analysis_sha256': digest(analysis_path.read_bytes())}}
        revision = self.put(content_id, f'system-design/{design_id}/design.json', design, expected=expected)
        wp = self.wallpaper(content_id)
        atomic(contained(wp, f'system-design/{design_id}/rationale.md'), (design['rationale'] + '\n').encode())
        return {'content_id': content_id, 'design_id': design_id, 'revision': revision, 'design': design}

    def design(self, content_id, design_id):
        path = contained(self.wallpaper(content_id), f'system-design/{name(design_id)}/design.json')
        data = path.read_bytes()
        return {'content_id': content_id, 'design_id': design_id, 'revision': digest(data), 'design': json.loads(data)}

    def selection(self, content_id):
        return read(contained(self.wallpaper(content_id), 'selection.json'), {})

    def choose_primary(self, content_id, design_id, revision, *, expected):
        design = self.design(content_id, design_id)
        if design['revision'] != revision:
            raise Conflict('selected design changed since preview')
        analysis_path = self.analysis_path(content_id)
        analysis_revision = digest(analysis_path.read_bytes()) if analysis_path.is_file() else None
        if design['design'].get('source_context', {}).get('analysis_sha256') != analysis_revision:
            raise Conflict('selected design belongs to an earlier wallpaper analysis')
        value = {'primary': design_id, 'design_revision': revision}
        return self.put(content_id, 'selection.json', value, expected=expected)

    def browse(self, *, file_section=None, page=0, page_size=30, content_id=None, summary_only=False):
        """Only metadata and real files; never invokes native status or scans media."""
        sections = {k: [] for k in ('Rules', 'References', 'Memory', 'Helpers', 'Wallpapers', 'Elements', 'Designs', 'Releases', 'History')}
        if type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError('invalid bounded library page')
        if file_section is not None and file_section not in sections:
            raise ValueError('unknown library section')
        scoped_root = self.wallpaper(identifier(content_id)) if content_id else None
        counts = {section: 0 for section in sections}
        for prefix, section in (('design-rules', 'Rules'), ('reference', 'References'), ('memory', 'Memory'), ('helpers', 'Helpers'), ('wallpapers', 'Wallpapers')):
            base = contained(self.root, prefix)
            if not base.exists():
                continue
            for path in sorted(base.rglob('*')):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(self.root)
                selected = section
                if '.history' in relative.parts:
                    selected = 'History'
                elif prefix == 'wallpapers':
                    if 'shared' in relative.parts:
                        selected = 'Elements'
                    elif 'system-design' in relative.parts:
                        selected = 'Designs'
                    elif 'releases' in relative.parts:
                        selected = 'Releases'
                if file_section is not None and selected != file_section:
                    continue
                if scoped_root is not None and prefix == 'wallpapers' and not path.is_relative_to(scoped_root):
                    continue
                offset = counts[selected]
                counts[selected] += 1
                if page * page_size <= offset < (page + 1) * page_size:
                    sections[selected].append({'path': str(path), 'relative': relative.as_posix(), 'url': path.as_uri(), 'bytes': path.stat().st_size})
        if file_section is not None:
            return {'root': str(self.root), 'sections': sections, 'section_counts': counts,
                    'page': page, 'page_size': page_size, 'content_id': content_id, 'file_section': file_section}
        workspaces = []
        for content, relative in read(self.root / '.state/wallpapers.json', {}).items():
            if content_id is not None and content != content_id:
                continue
            wp = contained(self.root, relative)
            design_paths = sorted(wp.glob('system-design/*/design.json'))
            element_paths = sorted(wp.glob('shared/*/element.json'))
            release_paths = sorted(wp.glob('releases/*/release.json'))
            deployment_paths = sorted(wp.glob('.history/deployments/*/receipt.json'))
            analysis_path = wp / 'analysis.json'
            analysis = read(analysis_path, {})
            analysis_revision = digest(analysis_path.read_bytes()) if analysis_path.is_file() else None
            details = analysis.get('analysis', {})
            workspace = {'content_id': content, 'path': str(wp), 'title': read(wp / 'wallpaper.json').get('title', ''),
                         'selection': self.selection(content),
                         'selection_revision': digest((wp / 'selection.json').read_bytes()) if (wp / 'selection.json').exists() else None,
                         'summary_only': summary_only, 'page': page, 'page_size': page_size,
                         'counts': {'designs': len(design_paths), 'elements': len(element_paths),
                                    'releases': len(release_paths), 'deployments': len(deployment_paths)},
                         'analysis': {'path': str(analysis_path), 'revision': analysis_revision,
                                      'profile': analysis.get('profile'), 'base_frame': details.get('base_frame'),
                                      'coverage': details.get('coverage'), 'representative_states': details.get('representative_states', []),
                                      'requested_supplements': details.get('supplements', []),
                                      'accepted_supplements': [r['name'] for r in details.get('supplement_records', [])]},
                         'designs': [], 'elements': [], 'releases': [], 'deployments': []}
            if not summary_only:
                start, end = page * page_size, (page + 1) * page_size
                for path in design_paths[start:end]:
                    if path.is_symlink():
                        continue
                    value = self.design(content, path.parent.name)
                    value['stale'] = value['design'].get('source_context', {}).get('analysis_sha256') != analysis_revision
                    value['preview'] = str(path.parent / 'previews/system.png') if (path.parent / 'previews/system.png').is_file() else None
                    value['details'] = []
                    for p in sorted(path.parent.glob('apps/*/detail.json')):
                        if p.is_symlink():
                            continue
                        detail = read(p)
                        value['details'].append({k: detail.get(k) for k in ('target', 'status', 'runtime', 'reason', 'artifact', 'preview', 'capability')}
                                                | {'revision': digest(p.read_bytes()), 'approved': read(p.with_name('approval.json'), {}).get('revision') == digest(p.read_bytes())})
                    workspace['designs'].append(value)
                for path in element_paths[start:end]:
                    if not path.is_symlink():
                        workspace['elements'].append({'name': path.parent.name, 'metadata': read(path), 'pin': read(path.with_name('revision.json')), 'image': str(path.with_name('element.png'))})
                for paths, key in ((release_paths, 'releases'), (deployment_paths, 'deployments')):
                    workspace[key] = [read(p) for p in paths[start:end] if not p.is_symlink()]
            workspaces.append(workspace)
        import tomllib
        defaults = tomllib.loads(contained(self.root, 'theme.toml').read_text()).get('defaults', {})
        reference_options = [p.parent.name for p in sorted(contained(self.root, 'reference').glob('*/reference.json')) if not p.is_symlink()]
        return {'root': str(self.root), 'sections': sections, 'section_counts': counts, 'page': page,
                'page_size': page_size, 'content_id': content_id, 'wallpapers': workspaces,
                'guidance_defaults': defaults, 'reference_options': reference_options}


def import_legacy(source: Path, destination: Path):
    """Build a complete sibling tree, then publish once. The source stays intact.

    No running job may be migrated: its captured absolute paths would otherwise
    publish into the archived source. The caller must stop only its own worker.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if destination.exists():
        raise Conflict('destination already exists; import never merges a partial library')
    index = read(source / 'index.json')
    state = read(source / 'studio.json', {})
    if not isinstance(index, dict) or not isinstance(index.get('items'), list):
        raise ValueError('legacy inventory is missing or malformed')
    if any(j.get('state') in ('queued', 'running') for j in state.get('jobs', []) if isinstance(j, dict)):
        raise Conflict('legacy library still has queued/running jobs')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + destination.name + '.import-', dir=destination.parent))
    store = Store(staging)
    mappings = {}
    try:
        store.initialize()
        for item in index['items']:
            content_id = identifier(item['id'])
            wp = store.wallpaper(content_id, item.get('title', ''))
            atomic(wp / 'wallpaper.json', encoded({**read(wp / 'wallpaper.json'), 'source': item}))
            for folder_name in ('analysis', 'frames'):
                old = source / folder_name / (content_id + '.json' if folder_name == 'analysis' else content_id)
                target = wp / 'analysis.json' if folder_name == 'analysis' else wp / 'system-design'
                if old.exists():
                    if old.is_symlink():
                        raise ValueError('legacy creative source is a symlink')
                    if old.is_dir():
                        if any(p.is_symlink() for p in old.rglob('*')):
                            raise ValueError('legacy creative assets contain symlinks')
                        target.mkdir(parents=True, exist_ok=True)
                        for child in old.iterdir():
                            target_child = store.generation_dir(content_id, child.name)
                            shutil.copytree(child, target_child)
                            mappings[str(child)] = str(destination / target_child.relative_to(staging))
                    else:
                        shutil.copy2(old, target)
                        mappings[str(old)] = str(destination / target.relative_to(staging))
            if (wp / 'analysis.json').exists():
                original_analysis = read(wp / 'analysis.json')
                atomic(wp / '.history/import/analysis.json', encoded(original_analysis))
                for frame in original_analysis.get('evidence', {}).get('frames', []):
                    evidence = Path(frame.get('file', ''))
                    if evidence.is_file():
                        if digest(evidence.read_bytes()) != frame.get('sha256'):
                            raise ValueError('legacy evidence hash mismatch')
                        copied = contained(wp, f"frames/samples/imported-{frame['sha256']}{evidence.suffix}")
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(evidence, copied)
                        mappings[str(evidence)] = str(destination / copied.relative_to(staging))
            binding = source / 'analysis' / (content_id + '.binding.json')
            if binding.is_file():
                shutil.copy2(binding, wp / 'analysis.binding.json')
            for old_name, relative in (('captures', 'frames/captures'), ('reset', '.history/reset')):
                old = source / old_name / content_id
                if old.is_dir():
                    copied = contained(wp, relative)
                    shutil.copytree(old, copied)
                    mappings[str(old)] = str(destination / copied.relative_to(staging))
            wp_history = wp / '.history/import'
            scoped = json.loads(json.dumps(state))
            for section in (scoped, scoped.get('noctalia', {})):
                for key in ('variants', 'jobs', 'feedback'):
                    if key in section:
                        section[key] = [row for row in section[key] if row.get('content_id') == content_id]
                for key in ('mappings', 'design_overrides'):
                    if key in section:
                        section[key] = {k: v for k, v in section[key].items() if k == content_id}
                if 'design_profiles' in section:
                    section['design_profiles'] = {k: v for k, v in section['design_profiles'].items() if v.get('content_id') == content_id}
            atomic(wp_history / 'studio.json', encoded(scoped))
            atomic(wp_history / 'catalog-item.json', encoded(item))
        # Operational data is global; creative paths above are per-wallpaper.
        for file in source.iterdir():
            if file.is_file() and file.suffix == '.json' and file.name != 'studio.json':
                shutil.copy2(file, staging / '.state' / file.name)
        if (source / 'imports').is_dir():
            shutil.copytree(source / 'imports', staging / '.state/imports')
            mappings[str(source / 'imports')] = str(destination / '.state/imports')

        def rewrite(value):
            if isinstance(value, dict):
                return {k: rewrite(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rewrite(v) for v in value]
            if isinstance(value, str):
                for old in sorted(mappings, key=len, reverse=True):
                    if value == old or value.startswith(old + '/'):
                        return mappings[old] + value[len(old):]
            return value

        atomic(staging / '.state/studio.json', encoded(rewrite(state)))
        # Actual generation requests/source manifests remain byte-for-byte
        # provenance. Only working metadata and operational paths are relocated.
        for file in staging.rglob('*.json'):
            if '.history' not in file.parts and 'artifacts' not in file.parts and file.name != 'wallpapers.json':
                atomic(file, encoded(rewrite(read(file))))
        for item in index['items']:
            wp = store.wallpaper(item['id'])
            binding_path = wp / 'analysis.binding.json'
            if binding_path.exists() and (wp / 'analysis.json').exists():
                binding = read(binding_path)
                binding['analysis_sha256'] = digest((wp / 'analysis.json').read_bytes())
                atomic(binding_path, encoded(binding))
        atomic(staging / '.state/import.json', encoded({'source': str(source), 'wallpapers': len(index['items']), 'paths': mappings}))
        os.rename(staging, destination)
        return {'root': str(destination), 'wallpapers': len(index['items']), 'source_preserved': True}
    except BaseException:
        shutil.rmtree(staging)
        raise
