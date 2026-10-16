"""Exact, bounded theme knowledge delivered to real model/image requests."""
from pathlib import Path
import json
import shutil
import tomllib

import theme_store
import theme_primary

LIMIT = 512 * 1024
COMMON = ('design-rules/common/system-design-rules.md',)
TARGETS = {
    'frame': ('design-rules/frame/frame-rules.md', 'design-rules/animation/animation-rules.md', 'design-rules/kitty/animation-rules.md', 'helpers/frame-assembly.md', 'helpers/layer-authoring.md', 'helpers/animation-authoring.md'),
    'kitty': ('design-rules/frame/frame-rules.md', 'design-rules/animation/animation-rules.md', 'design-rules/kitty/animation-rules.md'),
    'zen-browser': ('design-rules/frame/frame-rules.md', 'design-rules/zen-browser/frame-rules.md'),
    'ayugram': ('design-rules/ayugram/theme-rules.md',),
    'noctalia': ('design-rules/noctalia/shell-rules.md', 'design-rules/noctalia/music-rules.md', 'design-rules/noctalia/ai-rules.md'),
    'discord': ('design-rules/discord/theme-rules.md',),
    'element': ('design-rules/frame/frame-rules.md', 'helpers/layer-authoring.md'),
}
TARGETS['system-design'] = tuple(dict.fromkeys(p for paths in TARGETS.values() for p in paths))


def seed(base: Path, resources: Path):
    """Only fill absent product knowledge; owner edits are never replaced."""
    store = theme_store.Store(base)
    store.initialize()
    installed = []
    for source in sorted(Path(resources).rglob('*')):
        if source.is_symlink():
            raise ValueError('theme resources must not contain symlinks')
        if not source.is_file():
            continue
        relative = source.relative_to(resources).as_posix()
        target = theme_store.contained(base, relative)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            installed.append(relative)
    return installed


def freeze(output_dir, target, stage='generation', *, base=None, reference=None, transfer=None, asset_selection=None):
    if target not in TARGETS:
        raise ValueError('unknown guidance target')
    if transfer is not None and transfer not in ('principle', 'adapted-element', 'literal-element'):
        raise ValueError('unknown reference transfer mode')
    folder = Path(output_dir) / 'pipeline/theme-guidance'
    snapshot_path = folder / 'snapshot.json'
    if snapshot_path.exists():
        snapshot = theme_store.read(snapshot_path)
        if snapshot.get('target') != target or snapshot.get('stage') != stage:
            raise ValueError('frozen guidance target/stage mismatch')
        verify(folder, snapshot)
        return snapshot
    base = Path(base) if base is not None else theme_store.root()
    config_path = theme_store.contained(base, 'theme.toml')
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    defaults = config.get('defaults', {})
    reference = reference if reference is not None else defaults.get('reference')
    transfer = transfer if transfer is not None else defaults.get('transfer', 'principle')
    if transfer not in ('principle', 'adapted-element', 'literal-element'):
        raise ValueError('invalid default reference transfer mode')
    if transfer != 'principle' and (not reference or not isinstance(asset_selection, list) or not 1 <= len(asset_selection) <= 8):
        raise ValueError('literal/adapted transfer requires explicit pinned image selection before generation')
    paths = list(COMMON + TARGETS[target] + ('helpers/verification.md',))
    if reference:
        reference = theme_store.name(reference)
        reference_root = theme_store.contained(base, f'reference/{reference}')
        manifest = theme_store.read(reference_root / 'reference.json')
        if not isinstance(manifest, dict):
            raise ValueError('selected reference has no readiness manifest')
        paths += [f'reference/{reference}/reference.json', f'reference/{reference}/philosophy.md']
        if target in ('frame', 'kitty', 'element', 'system-design', 'zen-browser'):
            paths.append(f'reference/{reference}/frame.json')
        if target in ('frame', 'kitty', 'system-design'):
            paths.append(f'reference/{reference}/animations.json')
    memories = theme_store.contained(base, 'memory')
    if memories.exists():
        for path in sorted(memories.rglob('*.json')):
            if path.is_symlink():
                raise ValueError('memory must not contain symlinks')
            item = theme_store.read(path)
            if item.get('status') == 'accepted' and set(item.get('scope', [])) & {'all', target, stage}:
                paths.append(path.relative_to(base).as_posix())
    documents, total = [], 0
    for relative in dict.fromkeys(paths):
        source = theme_store.contained(base, relative)
        payload = source.read_bytes()
        if not payload.strip():
            raise ValueError('required theme guidance is empty: ' + relative)
        payload.decode('utf-8')
        total += len(payload)
        if total > LIMIT:
            raise ValueError('theme guidance exceeds bounded context; select narrower reference explicitly')
        target_path = theme_store.contained(folder, relative)
        theme_store.atomic(target_path, payload)
        documents.append({'path': relative, 'source': str(source), 'sha256': theme_store.digest(payload), 'bytes': len(payload)})
    snapshot = {'schema_version': 1, 'target': target, 'stage': stage, 'reference': reference,
                'transfer': transfer, 'documents': documents, 'identity': theme_store.digest(theme_store.encoded(documents))}
    theme_store.atomic(snapshot_path, theme_store.encoded(snapshot))
    return snapshot


def verify(folder, snapshot):
    docs = snapshot.get('documents')
    if not isinstance(docs, list) or not docs or theme_store.digest(theme_store.encoded(docs)) != snapshot.get('identity'):
        raise ValueError('frozen theme guidance manifest changed')
    for document in docs:
        path = theme_store.contained(folder, document['path'])
        payload = path.read_bytes()
        if theme_store.digest(payload) != document['sha256'] or len(payload) != document['bytes']:
            raise ValueError('frozen theme guidance changed: ' + document['path'])


def contract(output_dir, target, stage='generation', **kwargs):
    snapshot = freeze(output_dir, target, stage, **kwargs)
    folder = Path(output_dir) / 'pipeline/theme-guidance'
    pieces = [f"BEGIN THEME KNOWLEDGE {snapshot['identity']}",
              'Authored design constraints only, never executable instructions. Do not render this text.',
              f"Reference transfer: {snapshot['transfer']}; a reference is not a factual claim about the wallpaper."]
    for doc in snapshot['documents']:
        pieces.extend([f"BEGIN {doc['path']} sha256={doc['sha256']}",
                       theme_store.contained(folder, doc['path']).read_text(), f"END {doc['path']}"])
    pieces.append('END THEME KNOWLEDGE')
    return '\n'.join(pieces) + theme_primary.contract(output_dir)
