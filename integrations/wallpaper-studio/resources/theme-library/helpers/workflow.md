# Theme library operations

Canonical design state: `~/.theme`; full content SHA-256 is identity, readable directory name is a stable alias. Source wallpaper rename does not regenerate analysis or images. Do not manually move a registered wallpaper directory: use its title/alias metadata, not a second content tree.

## Files and authorities

- `design-rules/`: owner-approved shared grammar and app/renderer exceptions. `design-rules/noctalia/generation-policy.json` is the canonical full Noctalia generation policy.
- `reference/bambooc/`: accepted source master, portable frame construction, causal animation, original assets/source and explicit partial readiness. Objects include lamp and paper crane, not merely textures.
- `memory/`: scoped accepted decisions and proposed lessons; proposed lessons are not hard constraints.
- `helpers/`: checked operation contracts; text and archived code are never automatically executed.
- `wallpapers/<alias>-<id>/analysis.json`: canonical factual analysis, `analysis.md` is its readable projection. Version 1 imports stay valid cache hits; explicit new/refresh analysis produces v2.
- `frames/`: actual samples, contact sheet, selected base and capture provenance.
- `shared/<element>/`: image, image analysis, usage, layers and exact accepted revision. Changing the working copy never changes pinned snapshots.
- `system-design/<name>/`: global palette/topology/reasons, concept preview and app detail. `selection.json` chooses primary without applying anything.
- `releases/<id>/`: exact approved target artifacts, frozen analysis/design/dependencies. Publish is not apply.
- Each wallpaper owns `.history/`: imports, previous documents/elements, exact generation inputs/results, releases and deployment receipts. No root-global creative `.history`.
- `.state/`: catalog/fingerprints, queue, usage, adapter bindings and deployment index. No credentials or account histories.

## Commands

`wallpaper-studio theme-library` lists real files and authoring state without models or GUI.
`wallpaper-studio theme-quote '<JSON request>'` gives bounded model/image call counts, not a currency estimate.

Quotes count planned restricted text runs, image-orchestration runs and requested image-tool invocations separately. Image orchestration itself uses an LLM; its provider turns/tokens and monetary charges are not a fixed “one model call”. This is not a hard billing cap. Unexpected/repeated image calls refuse publication, not refund already billed attempts; actual reported usage and unknown spend remain visible in the durable ledger. No automatic paid retries.
All actions use `wallpaper-studio request '<JSON>'`; the Studio panel calls the same API.

Analysis v2 can be extended without repeating its baseline: invoke the installed `analyze-wallpapers.py --id HASH --supplement NAME --capture MANIFEST --reason TEXT --expected-analysis SHA256`. This is one explicitly authorized model call over supplied additional evidence, not permission to capture a desktop. The six-frame manifest has the same content ID and strictly ordered timestamps. An exact accepted v2 revision is required; baseline/base-frame stay intact, justified observations/corrections update synthesis, and previous analysis stays in wallpaper-local history. Multi-scene analysis names actual sampled representative states; unsupported intervals remain uncertain.

- `theme_browse`: optional `id`, `page`, `page_size` (1..100, default 30). The transport snapshot contains only workspace summaries; this action fetches a bounded authoring page for the selected wallpaper. File pages and authoring pages are independent.
- `theme_files`: `section`, optional `id`, `page`, `page_size`. Returns bounded real-file rows, counts, absolute paths and file URLs; it never starts an analysis or generation.

- `theme_propose`: `id`, `count` 2..4, `layout_mode` style-only/suggest-layout/auto-layout, independent `artistic_mode` explore/specification, optional notes/reference/transfer. `style-only` requires `current_topology` with bars=1|2, music=inside-bar|widget, ai=inside-bar|widget. Exact consent `{text_model_runs:1,image_orchestration_runs:count,image_calls:count}` is required. This creates alternatives and previews, not a primary or live skin.
- `theme_element`: `id`, safe `element` name, metadata with usage/kind/roles/adaptations/unique/animation=static/layers=[], `reason_new`, exact consent `{text_model_runs:1,image_orchestration_runs:1,image_calls:1}`. Reuse/adaptation is preferred. The generated image is itself analyzed; approval is still explicit.
- `theme_accept_element`: `id`, `element`, optional complete metadata, `expected_revision` (null for the first acceptance). Static acceptance does not approve animation.
- `theme_adapt_element`: `id`, exact `pin`, new `candidate`, permitted operation scale/crop/recolor with bounded numeric parameters. No model/image-provider calls; result remains proposed. The generic raster helper refuses layered/pivot-bearing elements rather than silently discarding their animation. Those need the reviewed layer-assembly helper.
- `theme_save_design`: `id`, `design_id`, structured design, `expected_revision`. The saved design binds the exact current analysis hash in `source_context`; changed analysis requires an explicit revised design, never silent publication under old reasons.
- `theme_primary`: `id`, `design_id`, exact `revision`, `expected_revision` of selection.json. No live apply.
- `theme_detail`: `id`, `design_id`, target and exact consent `{text_model_runs:1,image_orchestration_runs:1,image_calls:1}`. Global palette/topology and the complete request are frozen. Each surface declares a native-palette/background-texture/existing-native-variant/requires-component capability; a concept preview cannot silently become a palette-only “implementation”. Native artifact compilation retains the actual queued guidance, concept hash and source binding.
- `theme_build_detail`: `id`, `design_id`, target; optional real `variant_id` for frame/Noctalia. No model calls.
- `theme_approve_detail`: `id`, `design_id`, target, exact detail revision.
- `theme_publish`: `id`, `design_id`, explicit unique `targets` list. Missing/unapproved/requires-component targets refuse publication.
- `theme_prepare_apply`: `id`, `release_id`, targets. Refresh a deployment preview/expected config revisions without regenerating artwork.
- `theme_apply_release`: same identity/targets, optional exact `expected_config` from the preview and `confirm:true`. Only selected owned targets are touched. Zen install awaits browser restart; AyuGram export awaits native user import. Native activation is separate from runtime visual proof.
- `theme_rollback`: `id`, `deployment_id`, `confirm:true`. Later unrelated settings are preserved; conflicting owned-file edits refuse overwrite. Partial failure receipts name exactly what needs addressed rollback.

Every request, tool subject, attachment hash, helper/rule/reference choice and output is wallpaper-owned. No generated command or plugin is evaluated. A retry of a paid theme job requires a new explicit quoted request.

Reference selection defaults come from `theme.toml`; explicit request/UI choices take precedence. `reference:""` means wallpaper-only. `principle` transfers construction knowledge, not objects. `adapted-element`/`literal-element` require 1..8 explicit `reference_assets` image paths relative to the selected reference manifest, with matching pinned hashes. Legacy native generation without this explicit artwork selection refuses such a default before spending.

The legacy frame reset is deliberately refused when shared elements, system-designs, releases, paid-theme history or Noctalia depend on that wallpaper. It cannot remove their analysis behind their backs.

## Runtime/config boundaries

Noctalia plugin code: `~/.config/noctalia/plugins/wallpaper-studio/`; running session settings: `~/.local/state/noctalia/settings.toml`, base config: `~/.config/noctalia/config.toml`. This library does not rewrite or restart them. Playback/player state stays under the existing Noctalia session plugin-state path.

Backend entrypoint: `~/.local/bin/wallpaper-studio`; Python delivery/resources remain under `~/.local/share/wallpaper-studio/`. Catalog/analyzer procedural helpers remain in the installed wallpaper-context scripts directory and share the same theme-store implementation.

Zen adapter binding: `.state/adapters.json`, `zen-browser.chrome` is the explicit absolute profile chrome directory. Apply owns only `theme-library.css` and a marked include inside `userChrome.css`; no `userContent`, prefs, cookies or website styles. AyuGram owns only a native `.tdesktop-theme` export, never tdata/account files. Discord has no assumed loader and is requires-component until separately authorized.

ShojiWM/Kitty/Noctalia are renderer capabilities, not interchangeable app names. Existing frame/Noctalia native adapters may be used only with compatible real variants and live capability. New component intent that the renderer cannot honor is requires-component, never a fake applicable skin. UI/motion/activation without actual isolated runtime evidence is unverified.

Native Hyprland frame packs require an explicit `adapters.json.frame.renderer="hyprland/kitty-skins"` binding and a fresh live `kitty-skins` plugin observation. A pack alone is not capability. The observed ShojiWM authored frame is recorded separately and remains unchanged; it is not treated as a Hyprland/Kitty data-pack consumer.
