---
name: kitty-skin-authoring
description: "Use when creating, regenerating, adapting or repairing a Kitty/Hyprland application frame: wallpaper-driven skin art, atlas cuts, thickness, background gaps, seams, stretching or ornament animation. Not ordinary terminal colors, wallpaper analysis or a generic app-theme installer."
---

# HARD GATE

**Decorate around the client; improve the frame, never hide its defects with a background or padding.** Preserve the protected client opening, distinguish source geometry from physical thickness, and keep unique art out of repeat/stretch material. Read `references/session-lessons.md` and `references/quality-gates.md` **before any frame design, assembly or repair**, not only before animation. A rejected frame stays rejected until the failed visual criteria are resolved; generation, validation and deployment are different states.

OMP-global procedure. No bundled art-generation executable: use `skill://media-use` and the available image tools for assets; use the code delegation pipeline only if renderer/tool code must change. Subagent binding: **nothing**, trigger routing only.

Scope boundary: restricted Wallpaper Studio workers keep `--no-skills --no-rules`; the integrated host must explicitly freeze and embed this skill, `references/quality-gates.md`, `references/gothic-philosophy.md` and `references/image-generation-rules.md`. Check the attempt's guidance snapshot and actual stage/tool requests, not flag names. Unintegrated/older installations still do not read these files. Operational orchestration steps belong to the parent agent; a restricted model applies the supplied artistic constraints within its own tool/output contract, without opening linked files.

## Procedure

1. **Resolve inputs.** Read `skill://wallpaper-context` and its `references/metadata-contract.md`. Obtain a valid visual record, current appearance JSON and selected output/content policy. Missing analysis → collect it there, not by guessing from a filename. Preview-only is a declared limitation, not full motion evidence. Unknown palette → retain known terminal colors or block system-sync; never invent primary/secondary values.
2. **Resolve renderer.** Discover the actual kitty-skins checkout, installed CLI/plugin version and pack schema. The dated paths below are leads. Read `references/renderer-contract.md` before choosing a layout. A pack-only change must use capabilities of the loaded renderer; an unsupported effect is a code change, not a new imaginary JSON field.
3. **Write the design recipe and quality contract** using `references/design-recipe.md` in the linked worktree's ignored `.superpowers/specs/`. Carry the relevant historical failure → invariant → required evidence into the recipe and every worker brief. Choose 2–3 motifs/materials, hierarchy and palette ownership; record source/variant identity. Supply the cut map, supported sizes and predicted deformation extremes before asset/code dispatch. A generic “make seamless” instruction is insufficient.
   **Reuse the authored reference.** Read `references/gothic-philosophy.md` for the one-time interpretation of Gothic's construction, hierarchy and material depth. Transfer its craft, not its objects. Do not attach/analyze the Gothic raster on every run; refresh the distillation only when reference or owner decisions change. For image generation, pass `references/image-generation-rules.md` and the philosophy as actual image-model input, not only as instructions to an intermediary.
   **Retain the A-tier precedent.** `references/quality-gates.md` §8 preserves Frieren Pearl Mixed's new design, manual layer/mask repairs, combined leaf/pearl animation and the missing automation stages. `references/image-generation-rules.md` carries the image-facing projection. Use these actual embedded texts, not only a memory link. Gothic S/Frieren A are owner grades of named variants, not automatic scores or a mandate to copy their motifs.
4. **Choose color authority.** Default system-sync mode: Noctalia owns Kitty background/foreground/ANSI/selection/cursor colors; the skin config must not override those. Frame accents use observed system roles; artwork colors inform materials/lighting separately. An explicitly chosen self-contained theme may own both. Inspect include order before writing a config; “included” does not mean “authoritative”.
5. **Preserve the master; author a usable structure.** Record source/license/hash and generation provenance. Keep the approved master unchanged; improve derived art locally rather than regenerating the whole design unasked. Generate art, not fake terminal screenshots. Preserve alpha independently of RGB upscale. Measure the opening and the actual material contour separately; neither a center-alpha patch nor a bounding rectangle certifies the whole frame.
6. **Pass the geometry gate before assembly.** Follow `references/quality-gates.md`: justify each flexible run, its deformation range and every join's contour, tangent, material and shading. Use neutral structural runs and one-shot objects; mirror only proven neutral cells, not curved ribbons. Contiguous source cuts and fixed ornaments do not prevent distortion. If suitable runs are absent, improve the derived artwork and transitions; do not alternate short repeats with extreme stretch. Any thickness increase must paint supporting material to the client boundary with matching extents, not add a background strip or relax the client clip.
7. **Establish the static frame, then add motivated motion.** Static is default. After rejection, obtain acceptance of the repaired static design before optional effects. Explain object → behavior → moving part/anchor → occlusion; do not assign a pulse merely because a gem or mask exists. Candles/flame/wax are an example, not universal animation. Use only actual renderer capabilities, remove static duplicates and preserve foreground occlusion. Window-surface shaders are separate, explicitly justified choices; a catalog mapping is not artistic approval.
8. **Produce a complete pack.** `skin.json`, exact/adaptive RGBA atlases, all referenced masks/effect textures, Kitty config where required, and source/provenance metadata. Follow the real parser, not invented fields. Preserve the approved pack; create a new variant until adoption is explicit. Other applications need explicit targeting and their own acceptance criteria: reuse an applicable compositor renderer, but do not assume Kitty configuration applies to them.
9. **Check only within authorization; never upgrade the evidence.** Inspect contract/paths/diff. Tests, rendering and screenshots require the owner's explicit request under local rules. When authorized, use the permitted real renderer surface and the case/evidence checklist in `references/quality-gates.md`; record actual modes, sizes, scale, pack/binary and both overview and defect-local observations. CLI `valid`, alpha percentages and a screenshot's existence are not visual acceptance. Without required evidence, mark **unverified** and stop promotion/rollout; owner rejection blocks acceptance even after a successful build or earlier favorable report.
10. **Apply only when asked.** Follow `references/renderer-contract.md` deployment boundary. `terminal-skin use`/`reload` mutate the live setup; they are not validators. Binary changes require ABI/version agreement and safe loading; a successful unload message or new on-disk hash does not prove the new DSO is active. Never restart the graphical session without explicit permission.

## Required result

Deliver the actual pack path, recipe and filled quality contract, provenance, variant identity, palette authority, supported renderer, and specific verification/owner-acceptance evidence. Distinguish **authored**, **contract-validated**, **rendered**, **owner-approved**, **applied**, **rejected** and **unverified**. For each reported defect, name its criterion and current evidence; do not silently close it because another defect disappeared. A skill-only task delivers instructions, not a repaired pack or automatic generator enforcement.

## Failure modes

| Symptom | Cause → action |
|---|---|
| Kitty blank except fullscreen | Client FBO/surface replacement → return to native decoration; never patch text rendering. |
| Empty/background strip beside client | Reservation, artwork, alpha or shadow mismatch → identify the layer, then improve material/geometry; never pad or add a backplate to conceal it. |
| Repeated or elongated artwork, kinks at touching cuts | Non-neutral repeat cell or incompatible deformation → redesign structural runs and transitions; inspect tangents, not just rectangle adjacency. |
| Frame detached during workspace motion | Missing assigned geometry/offset transforms → inspect renderer contract, not bigger art offsets. |
| New manifest rejected or change absent | Old mapped DSO/ABI/schema → deploy matching code+assets; don't retry reload blindly. |
| Double flame / disappearing wax | Static duplicate or wrong visibility mask/loop endpoint → fix base art and foreground occlusion. |
| Noctalia palette has no effect | Later skin include overrides colors → choose one authority. |
| All numerical checks pass, owner still sees defects | Checks prove syntax/coverage, not craft → keep the visual criterion rejected; inspect local joins and whole-frame composition when authorized. |
| Rule exists but automated generations ignore it | Consumer does not load the skill → name the prompt/assembly boundary; do not claim the file edit fixed the pipeline. |

## Current state

| Date | Observation / status |
|---|---|
| 2026-09-30 | Reference implementation: `/home/q/dev/qol/.worktrees/kitty-skins/feature/kitty-skins`; native Hyprland decoration, schema 2, optional frame_insets and candle_effect. Verify current source/loaded binary before reuse. |
| 2026-09-30 | Gothic Eclipse final appearance explicitly approved by owner. This is visual acceptance, not measured FPS/GPU cost. Latest geometry/filter values in renderer reference supersede earlier drafts. |
| 2026-10-02 | Frieren frame rejected after both repeat-based and contiguous-stretch attempts. Added mandatory historical/quality gates and evidence contract; no frame repair or generator integration is implied by this skill update. |
| 2026-10-03 | Owner graded Gothic Eclipse S-tier and Frieren Pearl Mixed A-tier; requested design and animation knowledge for future autonomous generation. Captured in frozen quality gates §8 and image-stage rules. Mixed native effect/manual builder exist; automatic layer extraction and mixed-motion controls remain separate implementation work. |

## Session mistakes

- Client-FBO precomposition broke ordinary Kitty; the stable boundary is native compositor decoration with no client interception.
- “Thicker frame” was initially implemented as empty space. The owner meant thicker **stone**, touching the actual terminal boundary.
- Early nearest-filter/old geometry notes became stale after approved upscale/linear-filter/physical-thickness changes. Latest actual source + owner feedback wins.
- Reload/unload reports were mistaken for proof of new code. A mapped old/deleted DSO can survive; loaded identity matters.
- Frieren: short mirrored curves became zigzags; replacing them with contiguous cuts produced extreme stretch. Source adjacency is not shape continuity.
- Frieren: inner-edge alpha runs and a clear center patch were treated as quality proof; material/backing strips and local joins still failed owner review.
- Existing lessons were available but loaded as background advice, not task-specific stop conditions. They now precede all frame work and must enter the recipe/worker contract.
- A wallpaper-selected window shader and arbitrary ornament pulses were substituted for animation motivated by depicted objects. Keep those design decisions separate.
- Frieren mixed: source-pixel offsets treated as logical pixels hid midpoint stars; opaque mask collars painted background; narrow group masks lost foliage; thresholding each antialiased half misreported missing pixels. Preserve coordinates, material alpha and summed ownership before adding motion.

## Scripts

No bundled executable. Existing commands (resolve their installed paths/version before use): `terminal-skin list`, `current`, `validate <id-or-path>` are inspection; `use <id>` and `reload` are live actions. Wallpaper helpers are owned by `skill://wallpaper-context`; do not duplicate them here.

Self-check: historical failure mapped to a filled quality contract; material reaches the protected client boundary without a concealment strip; deformation and joins justified; motion motivated; no invented renderer capability; acceptance/rejection/unverified states and the agent-versus-generator boundary reported honestly.
