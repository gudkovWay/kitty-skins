"""Target-specific artifacts and ownership-aware, non-atomic deployment receipts."""
from __future__ import annotations
from functools import wraps
import fcntl
import json
from pathlib import Path
import shutil
import uuid
import zipfile

import theme_store as fs
import theme_guidance
import storage

BEGIN = '/* BEGIN THEME LIBRARY */'
END = '/* END THEME LIBRARY */'
INCLUDE = BEGIN + '\n@import url("theme-library.css");\n' + END + '\n'
TARGETS = ('zen-browser', 'ayugram', 'frame', 'noctalia', 'discord')


def serialized(fn):
    @wraps(fn)
    def call(store, *args, **kwargs):
        lock = fs.contained(store.root, '.state/deployment.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return fn(store, *args, **kwargs)
    return call


def _release(store, content, release_id):
    wp = store.wallpaper(content)
    path = fs.contained(wp, f'releases/{fs.name(release_id)}/release.json')
    frozen = fs.contained(wp, f'.history/releases/{release_id}/release.json')
    if not path.is_file() or not frozen.is_file() or path.read_bytes() != frozen.read_bytes():
        raise ValueError('published release manifest is missing or changed')
    value = fs.read(path)
    if value.get('content_id') != content or value.get('release_id') != release_id:
        raise ValueError('published release identity mismatch')
    return value


def file_revision(path):
    return fs.digest(path.read_bytes()) if path.exists() else None


def _detail_path(store, content, design, target):
    if target not in TARGETS:
        raise ValueError('unknown application adapter')
    return fs.contained(store.wallpaper(content), f'system-design/{fs.name(design)}/apps/{target}/detail.json')


def _owned_block(text):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise fs.Conflict('malformed or repeated owned CSS include')
    if BEGIN not in text:
        return ''
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    if text[end:end + 1] == '\n':
        end += 1
    return text[start:end]


def _replace_block(text, replacement):
    old = _owned_block(text)
    return text.replace(old, replacement, 1) if old else replacement + text


def _chrome(store):
    binding = fs.read(store.root / '.state/adapters.json', {}).get('zen-browser', {})
    value = binding.get('chrome')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('Zen adapter requires an explicit absolute chrome directory binding')
    path = Path(value)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Zen profile binding must not traverse a symlink')
    return path


def _zen(design):
    p = design['palette']
    # Reviewed renderer code, never model-generated CSS or website rules.
    return f'''/* Static browser chrome; native website content remains untouched. */
@-moz-document url-prefix("chrome://") {{
  :root {{
    --theme-background: {p['background']};
    --theme-foreground: {p['foreground']};
    --theme-accent: {p['accent']};
    --toolbar-bgcolor: var(--theme-background) !important;
    --toolbar-color: var(--theme-foreground) !important;
    --toolbar-field-background-color: var(--theme-background) !important;
    --toolbar-field-color: var(--theme-foreground) !important;
    --arrowpanel-background: var(--theme-background) !important;
    --arrowpanel-color: var(--theme-foreground) !important;
    --focus-outline-color: var(--theme-accent) !important;
    --tab-line-color: var(--theme-accent) !important;
    --tab-selected-textcolor: var(--theme-foreground) !important;
    --tab-selected-bgcolor: var(--theme-accent) !important;
  }}
  #navigator-toolbox, #zen-main-app-wrapper {{
    background: var(--theme-background) !important;
    color: var(--theme-foreground) !important;
  }}
  .zen-browser-generic-background::before,
  .zen-browser-generic-background::after {{ background: none !important; }}
}}
'''


def _telegram(design):
    p = design['palette']
    colors = {
        'windowBg': p['background'], 'windowFg': p['foreground'], 'windowBgOver': p['accent'],
        'windowFgOver': p['background'], 'windowBgActive': p['accent'], 'windowFgActive': p['background'],
        'windowFgSub': p['foreground'], 'windowFgSubOver': p['foreground'], 'windowBgRipple': p['accent'],
        'activeButtonBg': p['accent'], 'activeButtonFg': p['background'],
        'dialogsBg': p['background'], 'dialogsNameFg': p['foreground'], 'dialogsTextFg': p['foreground'],
        'dialogsBgActive': p['accent'], 'dialogsNameFgActive': p['background'], 'dialogsTextFgActive': p['background'],
        'historyTextInFg': p['foreground'], 'historyTextOutFg': p['foreground'],
        'msgInBg': p['background'], 'msgOutBg': p['background'],
        'msgInServiceFg': p['accent'], 'msgOutServiceFg': p['accent'],
        'msgInLinkFg': p['accent'], 'msgOutLinkFg': p['accent'],
        'historyComposeAreaBg': p['background'], 'historyComposeAreaFg': p['foreground'],
        'historyScrollBg': p['accent'], 'historyScrollBarBg': p['foreground'],
    }
    return '// Wallpaper theme: native palette, remaining roles retain native defaults.\n' + ''.join(f'{k}: {v};\n' for k, v in colors.items())


def _variant(store, content, target, variant_id):
    import library
    state = storage.read_state(store.root)
    variants = state['noctalia']['variants'] if target == 'noctalia' else state['variants']
    variant = next((v for v in variants if v['id'] == variant_id and v['content_id'] == content), None)
    if variant is None:
        raise ValueError('selected native variant is not owned by this wallpaper')
    if target == 'frame':
        binding = fs.read(store.root / '.state/adapters.json', {}).get('frame', {})
        if binding.get('renderer') != 'hyprland/kitty-skins':
            raise ValueError('No compatible data-pack frame adapter is bound to the actual renderer; authored ShojiWM frames remain separate.')
        import process
        cli = shutil.which('hyprctl')
        if not cli:
            raise ValueError('Hyprland frame renderer is not available')
        try:
            plugins = json.loads(process.run([cli, '-j', 'plugins'], timeout=10, cancel=lambda: False).stdout)
        except (RuntimeError, ValueError) as exc:
            raise ValueError('Cannot verify the bound native frame renderer') from exc
        if not isinstance(plugins, list) or not any(isinstance(p, dict) and p.get('name') == 'kitty-skins' for p in plugins):
            raise ValueError('The bound compositor has no live kitty-skins renderer')
    if target == 'noctalia':
        import noctalia_bundle
        library._require_fullpack_ready(variant, noctalia_bundle.native_status())
    return variant


def build_detail(store, content, design_id, target, *, variant_id=None):
    selected = store.design(content, design_id)
    design = selected['design']
    if design.get('source_context', {}).get('analysis_sha256') != file_revision(store.analysis_path(content)):
        raise fs.Conflict('wallpaper analysis changed since this system design was saved')
    path = _detail_path(store, content, design_id, target)
    previous_revision = file_revision(path)
    build_id = uuid.uuid4().hex
    folder = fs.contained(store.wallpaper(content), f'system-design/{design_id}/apps/{target}/revisions/{build_id}')
    folder.mkdir(parents=True)
    view_path = path.with_name('view.json')
    view_bytes = view_path.read_bytes() if view_path.exists() else None
    view_record = json.loads(view_bytes) if view_bytes is not None else None
    if view_record:
        if view_record.get('design_revision') != selected['revision']:
            raise fs.Conflict('app concept belongs to an earlier global design')
        source = Path(view_record['generation_folder'])
        history = store.wallpaper(content) / '.history/generations'
        fs.contained(history, source.relative_to(history).as_posix())
        if file_revision(source / 'inputs.json') != view_record['frozen_inputs_sha256']:
            raise fs.Conflict('app concept frozen input changed')
        shutil.copytree(source / 'pipeline', folder / 'pipeline')
        shutil.copy2(source / 'inputs.json', folder / 'inputs.json')
    guidance = theme_guidance.freeze(folder, target, base=store.root)
    detail = {'schema_version': 1, 'content_id': content, 'design_id': design_id,
              'design_revision': selected['revision'], 'target': target, 'status': 'ready',
              'runtime': 'unverified', 'guidance': guidance, 'artifact': None, 'expected_config': {},
              'view_revision': fs.digest(view_bytes) if view_bytes is not None else None,
              'capability': {'preview': bool(view_record), 'activation': 'manual', 'rollback': 'owned-files-only'}}
    supported = {'zen-browser': {'native-palette'}, 'ayugram': {'native-palette', 'background-texture'},
                 'frame': {'existing-native-variant'}, 'noctalia': {'existing-native-variant'}, 'discord': set()}
    unsupported = []
    if view_record:
        view = view_record['view']
        fs.atomic(folder / 'view.json', fs.encoded(view_record))
        preview = Path(view_record['preview']['image'])
        fs.contained(source, preview.relative_to(source).as_posix())
        if file_revision(preview) != view_record['preview']['sha256']:
            raise fs.Conflict('app concept image changed')
        shutil.copy2(preview, folder / 'concept.png')
        detail['preview'] = str(folder / 'concept.png')
        unsupported += [s['id'] for s in view['surfaces'] if s['capability'] not in supported[target]]
        if target in ('zen-browser', 'ayugram') and view['motion'] != 'static':
            unsupported.append('animated native app surfaces')
    if target == 'zen-browser' and design['apps'].get(target, {}).get('elements'):
        unsupported.append('shared-element chrome/frame assembly')
    if target == 'ayugram' and design['apps'].get(target, {}).get('elements'):
        backgrounds = {pin['element'] for pin in design.get('elements', [])
                       if fs.read(store.resolve_pin(content, pin) / 'element.json').get('kind') == 'texture'
                       and 'background' in fs.read(store.resolve_pin(content, pin) / 'element.json').get('roles', [])}
        if not set(design['apps'][target]['elements']) <= backgrounds or len(design['apps'][target]['elements']) > 1:
            unsupported.append('foreground/layered message or frame assembly')
    detail['capability']['supported_surfaces'] = sorted(supported[target])
    if target == 'zen-browser':
        chrome = _chrome(store)
        for file in (chrome / 'userChrome.css', chrome / 'theme-library.css'):
            if file.is_symlink():
                raise fs.Conflict('Zen owned file is a symlink; explicit config ownership is required')
        text = (chrome / 'userChrome.css').read_text() if (chrome / 'userChrome.css').exists() else ''
        if (chrome / 'theme-library.css').exists() and not _owned_block(text):
            raise fs.Conflict('theme-library.css already exists without our owned include')
        artifact = folder / 'theme-library.css'
        fs.atomic(artifact, _zen(design).encode())
        detail['artifact'] = str(artifact)
        detail['expected_config'] = {str(chrome / 'userChrome.css'): file_revision(chrome / 'userChrome.css'), str(chrome / 'theme-library.css'): file_revision(chrome / 'theme-library.css')}
        detail['capability']['activation'] = 'owned-include/awaiting-restart'
    elif target == 'ayugram':
        artifact = folder / 'wallpaper.tdesktop-theme'
        with zipfile.ZipFile(artifact, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('colors.tdesktop-theme', _telegram(design))
            for pin in design.get('elements', []):
                element = store.resolve_pin(content, pin)
                metadata = fs.read(element / 'element.json')
                if 'background' in metadata['roles'] and metadata['kind'] == 'texture':
                    archive.write(element / 'element.png', 'background.png')
                    break
        detail['artifact'] = str(artifact)
        detail['capability']['activation'] = 'explicit-native-theme-import'
    elif target in ('frame', 'noctalia'):
        if not variant_id:
            detail.update(status='requires-component', reason='Select a real renderer-compatible frame pack/Noctalia variant; a concept is not an implementation.')
        else:
            try:
                variant = _variant(store, content, target, variant_id)
                if target == 'noctalia':
                    colors = variant.get('recipe', {}).get('colors', {})
                    for native_role, global_role in (('surface', 'background'), ('text', 'foreground'), ('accent', 'accent')):
                        if colors.get(native_role, '').lower() != design['palette'][global_role].lower():
                            raise ValueError('native variant palette differs from the locked system design')
                if design['apps'].get(target, {}).get('elements'):
                    raise ValueError('native variant needs a reviewed shared-element assembly binding')
                artifact = folder / 'native-variant.json'
                fs.atomic(artifact, fs.encoded(variant))
                detail.update(artifact=str(artifact), variant_id=variant_id,
                              native_identity=fs.digest(fs.encoded({k: variant.get(k) for k in ('id', 'content_id', 'recipe', 'presentation', 'pack_id', 'source_sha256')})))
                detail['capability']['activation'] = 'existing-native-adapter'
            except (ValueError, RuntimeError) as exc:
                detail.update(status='requires-component', reason=str(exc))
    else:
        detail.update(status='requires-component', reason='No authorized Discord renderer/loader is installed by this architecture.')
    if unsupported:
        detail.update(status='requires-component', artifact=None,
                      reason='Native adapter does not implement requested surfaces: ' + ', '.join(unsupported))
    if detail['artifact']:
        detail['artifact_sha256'] = file_revision(Path(detail['artifact']))
    if store.design(content, design_id)['revision'] != selected['revision']:
        raise fs.Conflict('global design changed during app detail compilation')
    if file_revision(view_path) != detail['view_revision']:
        raise fs.Conflict('app concept changed during artifact compilation')
    revision = store.put(content, path.relative_to(store.wallpaper(content)).as_posix(), detail, expected=previous_revision)
    return {**detail, 'revision': revision}


def approve_detail(store, content, design_id, target, revision):
    path = _detail_path(store, content, design_id, target)
    detail = fs.read(path)
    if file_revision(path) != revision:
        raise fs.Conflict('app detail changed since preview')
    if detail['status'] != 'ready' or not detail.get('artifact'):
        raise ValueError('app detail is not publishable: ' + detail.get('reason', 'requires component'))
    if file_revision(path.with_name('view.json')) != detail.get('view_revision'):
        raise fs.Conflict('app concept changed since artifact compilation')
    fs.contained(path.parent, Path(detail['artifact']).relative_to(path.parent).as_posix())
    if file_revision(Path(detail['artifact'])) != detail['artifact_sha256']:
        raise fs.Conflict('app detail artifact changed since preview')
    if target in ('frame', 'noctalia'):
        variant = _variant(store, content, target, detail['variant_id'])
        identity = fs.digest(fs.encoded({k: variant.get(k) for k in ('id', 'content_id', 'recipe', 'presentation', 'pack_id', 'source_sha256')}))
        if identity != detail['native_identity']:
            raise fs.Conflict('native variant changed since artifact compilation')
        import library
        if store.root.resolve() != storage.data_root().resolve():
            raise ValueError('native approval cannot escape an isolated library')
        library.approve(detail['variant_id'])
    approval = {'revision': revision, 'artifact_sha256': detail['artifact_sha256'], 'runtime': detail['runtime']}
    store.put(content, f'system-design/{design_id}/apps/{target}/approval.json', approval, expected=file_revision(path.with_name('approval.json')))
    return approval


def publish(store, content, design_id, targets):
    if not isinstance(targets, list) or not targets or len(set(targets)) != len(targets):
        raise ValueError('release requires an explicit nonempty unique target selection')
    design = store.design(content, design_id)
    expected_analysis = design['design'].get('source_context', {}).get('analysis_sha256')
    if expected_analysis != file_revision(store.analysis_path(content)):
        raise fs.Conflict('wallpaper analysis changed since the selected system design')
    wp = store.wallpaper(content)
    release_id = uuid.uuid4().hex
    staging = fs.contained(wp, 'releases/.' + release_id)
    staging.mkdir(parents=True)
    entries = {}
    try:
        for target in targets:
            path = _detail_path(store, content, design_id, target)
            detail = fs.read(path)
            approval = fs.read(path.with_name('approval.json'), {})
            if not detail or detail['status'] != 'ready' or approval.get('revision') != file_revision(path):
                raise ValueError('release target has no exact approved detail: ' + target)
            if file_revision(path.with_name('view.json')) != detail.get('view_revision'):
                raise fs.Conflict('app concept changed since approval')
            if detail['design_revision'] != design['revision']:
                raise fs.Conflict('app detail belongs to an earlier global design')
            artifact = Path(detail['artifact'])
            fs.contained(path.parent, artifact.relative_to(path.parent).as_posix())
            if file_revision(artifact) != approval.get('artifact_sha256'):
                raise fs.Conflict('approved artifact changed before publication')
            destination = staging / target / artifact.name
            destination.parent.mkdir()
            shutil.copy2(artifact, destination)
            if file_revision(destination) != detail['artifact_sha256']:
                raise fs.Conflict('artifact changed during publication')
            shutil.copytree(artifact.parent / 'pipeline', destination.parent / 'pipeline')
            for authored in ('inputs.json', 'view.json', 'concept.png'):
                if (artifact.parent / authored).is_file():
                    shutil.copy2(artifact.parent / authored, destination.parent / authored)
            entries[target] = {**detail, 'artifact': str(wp / 'releases' / release_id / target / artifact.name)}
        for pin in design['design'].get('elements', []):
            source = store.resolve_pin(content, pin)
            shutil.copytree(source, staging / 'elements' / pin['element'])
        analysis = store.analysis_path(content)
        shutil.copy2(analysis, staging / 'analysis.json')
        if file_revision(staging / 'analysis.json') != expected_analysis:
            raise fs.Conflict('wallpaper analysis changed during release assembly')
        fs.atomic(staging / 'design.json', fs.encoded(design))
        manifest = {'schema_version': 1, 'content_id': content, 'release_id': release_id,
                    'design_id': design_id, 'design_revision': design['revision'],
                    'analysis_sha256': expected_analysis, 'targets': entries,
                    'status': 'published', 'runtime': 'unverified'}
        fs.atomic(staging / 'release.json', fs.encoded(manifest))
        fs.atomic(fs.contained(wp, f'.history/releases/{release_id}/release.json'), fs.encoded(manifest))
        staging.rename(wp / 'releases' / release_id)
        return manifest
    except BaseException:
        shutil.rmtree(staging)
        raise


def prepare_apply(store, content, release_id, targets):
    manifest = _release(store, content, release_id)
    if not manifest or manifest.get('content_id') != content:
        raise ValueError('release identity mismatch')
    if not targets or not set(targets) <= set(manifest['targets']):
        raise ValueError('apply selection must be a subset of released targets')
    expected = {}
    if 'zen-browser' in targets:
        chrome = _chrome(store)
        expected['zen-browser'] = {str(chrome / n): file_revision(chrome / n) for n in ('userChrome.css', 'theme-library.css')}
    return {'release_id': release_id, 'targets': targets, 'expected_config': expected, 'cross_app_atomic': False}


def apply(store, content, release_id, targets, *, expected_config=None):
    wp = store.wallpaper(content)
    manifest = _release(store, content, release_id)
    if not manifest or manifest.get('content_id') != content or not targets or not set(targets) <= set(manifest['targets']) or len(set(targets)) != len(targets):
        raise ValueError('release or selected targets do not belong to this wallpaper')
    # Validate every input/config BEFORE touching any selected target.
    for target in targets:
        detail = manifest['targets'][target]
        artifact = Path(detail['artifact'])
        if not artifact.resolve().is_relative_to(wp.resolve()) or artifact.is_symlink() or file_revision(artifact) != detail['artifact_sha256']:
            raise ValueError('published artifact changed or escaped its wallpaper')
        if target in ('frame', 'noctalia'):
            if store.root.resolve() != storage.data_root().resolve():
                raise ValueError('native deployment cannot escape an isolated library')
            variant = _variant(store, content, target, detail['variant_id'])
            identity = fs.digest(fs.encoded({k: variant.get(k) for k in ('id', 'content_id', 'recipe', 'presentation', 'pack_id', 'source_sha256')}))
            if identity != detail['native_identity']:
                raise fs.Conflict('native variant changed since release')
        expected = (expected_config or {}).get(target, detail['expected_config'])
        if target == 'zen-browser':
            chrome = _chrome(store)
            required = {str(chrome / n) for n in ('userChrome.css', 'theme-library.css')}
            if not isinstance(expected, dict) or set(expected) != required:
                raise ValueError('deployment requires complete exact owned-config expectations')
            for file in (chrome / 'userChrome.css', chrome / 'theme-library.css'):
                if file.is_symlink():
                    raise fs.Conflict('Zen config ownership changed to a symlink')
        for path, revision in expected.items():
            if target != 'zen-browser' or Path(path).parent != _chrome(store):
                raise ValueError('config expectation names an unowned target path')
            if file_revision(Path(path)) != revision:
                raise fs.Conflict('config changed since deployment preview: ' + path)
    deployment_id = uuid.uuid4().hex
    folder = fs.contained(wp, '.history/deployments/' + deployment_id)
    folder.mkdir(parents=True)
    receipt = {'content_id': content, 'release_id': release_id, 'deployment_id': deployment_id,
               'cross_app_atomic': False, 'targets': {}, 'status': 'in-progress'}
    try:
        for target in targets:
            detail = manifest['targets'][target]
            if target == 'zen-browser':
                chrome = _chrome(store)
                chrome.mkdir(parents=True, exist_ok=True)
                sheet, user = chrome / 'theme-library.css', chrome / 'userChrome.css'
                old_text = user.read_text() if user.exists() else ''
                old_sheet = sheet.read_bytes() if sheet.exists() else None
                row = {'status': 'installing', 'chrome': str(chrome), 'old_block': _owned_block(old_text),
                       'sheet_existed': old_sheet is not None, 'sheet_after': detail['artifact_sha256'], 'user_existed': user.exists()}
                receipt['targets'][target] = row
                if old_sheet is not None:
                    fs.atomic(folder / 'zen-before.css', old_sheet)
                fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
                fs.atomic(sheet, Path(detail['artifact']).read_bytes())
                fs.atomic(user, _replace_block(old_text, INCLUDE).encode())
                row['status'] = 'installed-awaiting-restart'
            elif target == 'ayugram':
                receipt['targets'][target] = {'status': 'exported-awaiting-user-import', 'artifact': detail['artifact']}
            elif target in ('frame', 'noctalia'):
                import library
                variant = _variant(store, content, target, detail['variant_id'])
                identity = fs.digest(fs.encoded({k: variant.get(k) for k in ('id', 'content_id', 'recipe', 'presentation', 'pack_id', 'source_sha256')}))
                if identity != detail['native_identity']:
                    raise fs.Conflict('native variant changed since release')
                state = storage.read_state(store.root)
                previous = state['noctalia'].get('last_applied') if target == 'noctalia' else state.get('last_applied')
                receipt['targets'][target] = {'status': 'activating', 'previous': previous, 'variant_id': detail['variant_id']}
                fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
                if store.root.resolve() != storage.data_root().resolve():
                    raise ValueError('native activation cannot escape an isolated library')
                library.apply(detail['variant_id'], expected_content_id=content)
                receipt['targets'][target]['status'] = 'activation-command-succeeded/runtime-unverified'
            fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
        receipt['status'] = 'delivered/runtime-unverified'
    except Exception as exc:
        receipt.update(status='partial-failure', error=str(exc), rollback='explicit-addressed-rollback-required')
        fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
        raise RuntimeError(f"deployment {deployment_id} partially failed; receipt: {folder / 'receipt.json'}; {exc}") from exc
    fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
    fs.atomic(store.root / '.state/deployments.json', fs.encoded({'last': {'content_id': content, 'deployment_id': deployment_id, 'receipt': str(folder / 'receipt.json')}}))
    return receipt


def rollback(store, content, deployment_id):
    folder = fs.contained(store.wallpaper(content), f'.history/deployments/{fs.name(deployment_id)}')
    receipt = fs.read(folder / 'receipt.json')
    if not receipt or receipt.get('content_id') != content:
        raise ValueError('deployment receipt belongs to another wallpaper')
    for target, row in receipt['targets'].items():
        if row.get('status') == 'rolled-back':
            continue
        if target == 'zen-browser':
            chrome = _chrome(store)
            if str(chrome) != row['chrome']:
                raise fs.Conflict('Zen binding changed since deployment')
            sheet, user = chrome / 'theme-library.css', chrome / 'userChrome.css'
            text = user.read_text() if user.exists() else ''
            current_block = _owned_block(text)
            before_sheet = file_revision(folder / 'zen-before.css') if row['sheet_existed'] else None
            current_sheet = file_revision(sheet)
            if current_sheet not in (row['sheet_after'], before_sheet) or current_block not in (INCLUDE, row['old_block']):
                raise fs.Conflict('owned theme/include changed after deployment; rollback refused')
            if current_block == INCLUDE:
                fs.atomic(user, _replace_block(text, row['old_block']).encode())
            if current_sheet == row['sheet_after']:
                if row['sheet_existed']:
                    fs.atomic(sheet, (folder / 'zen-before.css').read_bytes())
                elif sheet.exists():
                    sheet.unlink()
            row['status'] = 'rolled-back'
        elif target == 'ayugram':
            row['status'] = 'export-retained/no-live-state-owned'
        else:
            import library
            state = storage.read_state(store.root)
            active = state['noctalia'].get('last_applied') if target == 'noctalia' else state.get('last_applied')
            if not isinstance(active, dict) or active.get('variant_id') != row['variant_id']:
                raise fs.Conflict('native activation changed after this deployment')
            previous = row.get('previous')
            if not isinstance(previous, dict) or not previous.get('variant_id'):
                raise ValueError('no previous owned native variant; explicit native clear is required')
            if store.root.resolve() != storage.data_root().resolve():
                raise ValueError('native rollback cannot escape an isolated library')
            library.apply(previous['variant_id'], expected_content_id=content)
            row['status'] = 'rolled-back/runtime-unverified'
        fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
    receipt['status'] = 'addressed-rollback-complete/runtime-unverified'
    fs.atomic(folder / 'receipt.json', fs.encoded(receipt))
    return receipt

# Serialize only deployment mutations; library authoring has a separate lock.
apply = serialized(apply)
rollback = serialized(rollback)
