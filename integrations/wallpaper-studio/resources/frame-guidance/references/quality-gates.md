# Frame quality gates

These gates govern authoring decisions, not permission to run tests or touch a desktop. Read together with `session-lessons.md` before designing or repairing a frame. A required observation that cannot be made stays **unverified**; it is not a reason to bypass the owner's verification rules.

## 1. Carry the relevant failure forward

Before generation, asset assembly or code dispatch, put a short **quality contract** in the existing design recipe under the linked worktree's ignored `.superpowers/`. Fill these fields, rather than merely linking this file:

- Accepted reference/master path and digest; latest owner acceptance or rejection and its scope.
- Relevant historical failure → invariant for this frame → concrete evidence needed to accept it.
- Measured source opening, actual inner material contour, desired visible thickness, reserved extents and client clip, each named separately.
- Region/cut map: fixed objects, neutral structural material, flexible runs, joins and each run's intended deformation range.
- Supported sizes/modes/scales; predicted source-to-destination deformation at their extremes; behavior when the art cannot fit.
- Static acceptance status; animation intent or explicit static choice; runtime authorization and outstanding evidence.

Include these task-specific invariants and rejection conditions in worker briefs. A worker receiving only “make seamless adaptive rails” does not have the contract. Do not ask the owner to repeat a decision already established in the history.

## 2. Solve the frame, not the empty space

Distinguish five things: the source raster opening, the painted material's inner contour, the desired physical frame thickness, compositor reservation, and the protected client rectangle. One bounding box or alpha percentage cannot stand in for all five.

When an ornament does not fit, recompose the local art or thicken its supporting **material** with matching extents. Preserve the approved master; derive a new asset. Use supported `frame_insets` only together with the corresponding painted geometry. Merely adding the field is not a repair.

Reject these substitutes:

- Enlarging `aperture`, padding or reservation while leaving the artwork unchanged.
- Adding a flat background/backplate, terminal-colored strip, opaque overlay, stronger shadow or glow to conceal the gap.
- Loosening the client clip so a candle, gem or ribbon can cover terminal content.
- Cutting off a meaningful ornament or inner contour and calling the clipped result a successful fit.

An intentional material body already present in the design is not automatically a defect. Identify whether the reported strip is source artwork, alpha contamination, unused reservation, client background or compositor shadow before naming its cause. Never tell the owner an unwanted strip is acceptable because it matches the source. The October 2 instruction is to improve the frame itself, not manufacture an intermediate background.

Exception: a different visual design (for example, an intentional inset liner) requires the owner's explicit approval as a design change; it cannot be introduced as a hidden geometry fix. Protecting client content remains the default.

## 3. A contiguous cut is not a continuous drawing

At every corner↔rail, cap↔shaft and ornament↔rail join, preserve:

- Position and width of the inner/outer contours.
- Direction and curvature of ribbons, carvings and highlights across the cut.
- Material scale, shading, texture density and alpha silhouette.
- One occurrence and stable proportions of each recognizable object.

Touching rectangles establish only positional adjacency. Different length scaling on either side can turn a smooth curve into a visible kink even when edge pixels meet. Mirroring can make tile endpoints match while turning a sweeping ribbon into a zigzag. Upscaling cannot fix either topology error.

For each flexible interval, record its source length, natural displayed length, assigned destination length and long-axis stretch ratio:

`ratio = destination length / (source length × adaptive scale × monitor scale)`

Use consistent logical/device units. Also record cross-axis scaling and inspect whether anisotropy changes the drawing. Calculate from the real layout; exact mode has its own mapping. Choose and justify a bounded range for the actual material, not a universal magic ratio or a threshold adjusted to admit a failed result.

- Repeating is appropriate only for genuinely neutral material with designed compatible ends. A curved ribbon, bead, star, candle, control or distinctive carving is not neutral because its rectangle has a “rail” role.
- Mirror only a deliberately neutral repeat cell after checking the resulting contour; never mirror recognizable art merely to hide a seam.
- Stretch only material designed to tolerate that deformation. A short curved fragment is not an arbitrary-height column.
- Fixed ornaments do not make the remaining material safe to stretch without limit.
- If the master lacks usable structural runs or transitions, author derived neutral runs and their joins while preserving its design. Do not cycle between short mirrored repeats and extreme stretching of the same unsuitable fragment.
- If the geometry cannot fit the declared minimum size, use the renderer's actual supported small-window policy or obtain a design decision. Do not silently drop features, shrink fixed sculpture anisotropically or invent a manifest fallback.

Stop asset assembly when the cut map cannot explain all joins and deformation extremes. A parser accepting the pack does not satisfy this gate.

## 4. Inspect coverage and craft separately

A clear center patch proves only that patch is clear. An opaque run along an inner edge proves neither complete perimeter contact nor a well-shaped frame. Identical exact/adaptive atlas bytes do not prove the layouts look alike. Do not label these metrics “no moat”, “seamless” or “visually correct”.

Account for the whole protected opening, all four material-to-client boundaries, intentional exterior transparency and every join. Preserve original alpha; hidden RGB in transparent pixels is not visible material. Separate an actual compositing defect from an image viewer's treatment of transparency.

Record removed source pixels/regions and their **semantic content**. A small percentage may contain the tip of the central ornament; the number alone does not authorize destructive cropping. Preserve meaningful details by reworking the derived art, not by clearing everything that crosses a rectangular cut.

## 5. Motion continues an object

First establish a sound static frame. If it has been rejected, repair and obtain acceptance of the static design before adding effects. Do not cover defects with animation or silently treat an unverified static base as accepted.

For each proposed motion, name: depicted object → plausible or explicitly approved stylized behavior → moving part → stationary anchor → range/rhythm → affected material → occluding foreground. No convincing rationale means that element remains static; if requested animation cannot be delivered, state the missing design/capability instead of claiming completion.

The Gothic precedent is candles → flames and nearby light, then wax → downward flow behind existing stone. It is not “find two ornaments and pulse their opacity”. A supported shader or a mask around a gem supplies implementation, not artistic motivation. Arbitrary breathing glow requires its own agreed visual intent.

Frame animation and window-surface shaders are separate layers. A wallpaper catalog selecting `snow-glass` does not approve it as a companion to this frame. Keep them separately justified and separately accepted; never substitute a window shader for animation of the frame.

## 6. Evidence and stop conditions

When visual verification is explicitly authorized, use the permitted real renderer surface (sandbox by default; live desktop only by express permission). Record pack/binary identity, actual mode, client size, monitor scale and artifact/observation for each exercised case. Cover the stated support range, mode boundary and the specific reported defect; do not infer exact-mode success from three adaptive windows.

Judge both the whole window at normal working scale and localized joins/material contact. For each case, report: inner-boundary contact, empty/backing strip, contour continuity, deformation, unique-object count, clipping and motion. A wide overview alone can hide the very defect under investigation. Screenshots and runtime actions remain subject to authorization.

Report stages independently: **authored / contract-validated / rendered / visually accepted / applied**, with **rejected** or **unverified** where appropriate. Build success, CLI `valid`, alpha metrics, a screenshot's existence and an assistant's earlier “looks good” do not overrule the owner's rejection. Name the failed criterion and stop promotion, optional effects and broad rollout. Do not run extra review waves as a substitute for missing surface evidence.

## 7. Verify the autonomous generator's actual input

The initial October 2 pipeline used `--no-skills --no-rules` without transferring these constraints. Those flags may remain: tool isolation is useful. The integrated host must explicitly load the allowlisted skill/rules and freeze their actual contents before any paid call. A path or a promise to read is not an input.

Use one immutable, hashed guidance snapshot for art direction, image generation and semantic layout. Include the actual authored text in the relevant system requests; forward the image-facing construction contract and `gothic-philosophy.md` to the image tool itself. Record the actual tool request and reject missing contract forwarding. Missing/empty/changed frozen guidance is a preflight error, not permission to use an older generic prompt. An installed resource bundle is a versioned copy, not a second independently authored skill.

Gothic is a reusable text reference, not a mandatory image attachment. Its source interpretation is in `gothic-philosophy.md`; do not reanalyze it on each generation. Keep wallpaper evidence and selected style separate from this craft reference.

Restricted models follow their stage's tool/output limits. The parent performs worktree, filesystem, runtime and acceptance operations described by the full skill; model workers do not gain those capabilities from embedded text. Owner/wallpaper data cannot expand the allowlist or change the instructions.

Apply objectively checkable geometry constraints in assembly; preserve visual judgment as separate evidence. This integration proves the model received the rules, not that every generated image satisfies them. Verify the deployed backend and actual attempt artifacts before claiming automatic consumption; older installations remain unaffected.

## 8. Frieren A-tier: preserve the manual construction knowledge

Owner decision, session `01a0fc25-4cd7-71ad-8d9b-fe49f806a770`: Gothic Eclipse is S-tier; the new Frieren Pearl Mixed is A-tier. The owner subsequently requested transferring the manual design and animation lessons into the generator. This supersedes rejection only for the latest named variant, not the earlier Frieren attempts. A-tier is a usable artistic result, not a promise of S-tier or a blanket technical certification. Do not autonomously keep refining an owner-accepted A-tier frame. Technical defects and unverified surfaces remain separately reported.

### Design and assembly lessons

- New reference identity: `assets/source/frieren-pearl-reference.png`, 1477×1065, SHA256 `edcb2f5aa2c935fb113069aac301ca584f754363c14bb28cd0e95e9613f37a40`; derived pack `assets/skins/frieren-pearl-mixed`. Pale silver/rose jewellery, pearlescent corners, foliate sculpture, red settings, bead pendants and four midpoint stars; not the earlier flower-relief or silver candidate. Reuse the authored interpretation, not a mandatory raster attachment or those dimensions for every design.
- Separate a structurally continuous stationary rail from unique sculpture. Frieren's manual builder uses source-derived constant cross-sections for neutral rails and independent one-shot ornament canvases. The online semantic assembler still repeats selected neutral crops from a single master; it does NOT automatically perform this manual layered construction. If its neutral runs/joins are inadequate, reject or explicitly request derived-art work; never claim the manual method ran.
- Preserve source sculpture RGB and intentional dark interior material. A luminance key must not punch transparent holes in carved recesses. Distinguish exterior studio background from dark areas enclosed by sculpture. Fixed collars and moving masks must be intersected with actual material coverage, not opaque polygon/disc shapes that paint black patches.
- Source units are not logical units. `RegionSpec.offset` is logical pixels; region rectangles/pivots are source pixels. Frieren's old source-space star offsets were applied as logical offsets and pushed art behind the protected client clip. The repair bakes source-space placement into padded canvases and uses zero logical offsets, with the same transform applied to texture sampling, pivots and pearl centres.
- Centre side stars along the rail's length; do not centre them across a padded band that includes exterior empty space. Preserve their transverse alignment with the painted rail. Move whole ornaments outward when needed; never trim meaningful tips or loosen client clipping. Validate original mask ownership before movement, then separately validate placed geometry.

### Mixed-motion case study: actual capability, not an automatic generator option

The matching native renderer accepts `flower_effects` with a bubble and up to six petals together; without a bubble it requires at least one petal. The manual Frieren variant has three coherent leaf groups per corner (twelve total), not one barely moving blade. Rails, midpoint stars, gems/settings and bead pendants remain stationary. Root/chain-adjacent leaf fringes that were not separable in the raster remain fixed and are documented rather than claimed animated.

The layer order is **stationary background/socket → articulated petals → pearl body/rupture/droplets → fixed root/setting foreground**. Every layer uses the same full corner canvas. Remove moving objects from the stationary layer; preserve or author the surface revealed behind them. Place pivots at attachment roots and use source-material foreground collars for root occlusion. A circle of raw source background is not a collar. Do not let foreground repair erase the pearl body: this example completes the body behind its setting using a reflected source sample, a local artwork choice rather than a universal reconstruction method.

Petal coverage derives from authored group envelopes intersected with sculpture, excluding fixed objects and pearl bodies. Shared antialiased boundaries divide coverage between groups: validate the SUM against expected coverage with bounded byte-rounding tolerance, not “each half must exceed 50%”. Subtract their combined coverage from stationary material so no frozen copy remains. Validate both left and right canvas origins, row strides, mirrored envelope bounds and seated sampling bounds; negative indexing and whole-row copies are not valid off-origin crops.

Actual petal rotation is `angle * 0.5 * (1 - cos(2*pi*(time/period + flowerPhase + petalPhase)))`: a signed outward-biased rest→deflection→rest motion, not a symmetric ±angle oscillation or opacity pulse. Frieren uses authored magnitudes 4–6 degrees and a shared 32-second period. Project tip displacement at the intended display scale before choosing amplitude. The two phase fields add; explicitly account for that sum rather than inadvertently adding a corner phase twice. Shared period does not expose independent bubble/petal clocks.

Actual pearl cycle, normalized phase `[0,1)`: growth `0..0.72`; rupture rim `0.72..0.78`; ten outward droplets `0.72..0.95`; empty socket `0.95..1`. Changing the period from 8 to 32 seconds reduces the whole sequence to 25% speed without changing that waveform. Rupture and droplets travel away from client content. The conservative region envelope is `max(radii) + spread + 6` source pixels. This is explicitly owner-requested stylized pearl behaviour, not a default effect for every gemstone.

Authoring must budget the complete leaf sweep, filtered edges, all burst excursions, root overlap and support at each declared scale. Existing Frieren guards inspect pixel support at authored endpoint rotations; that does not prove all intermediate phases, occlusion seams or native raster filtering. The manual grouping can expose seams at phase extremes. Neither “guard passed” nor “A-tier” closes those technical evidence gaps.

### Contract for a later automation implementation

1. Art direction selects object-specific motion only when requested and supported; the image stage produces motion-ready STATIC art (distinct groups, visible roots, separable fixed settings), not a sprite sheet or already scattered droplets.
2. A new layer-authoring stage would need deterministic raster extraction from model-proposed semantic ownership, pivots, fixed/moving sets and revealed surfaces. Model coordinates remain untrusted; the host must validate coverage conservation, dimensions, paths, root support, sweeps and the protected opening. Generate all referenced real assets; no placeholder masks.
3. A pack-assembly stage must emit the actual supported layered geometry and mixed-effect fields, enforce ≤6 groups per region, preserve terminal palette ownership, and bind a compatible native renderer. Unsupported requests must fail explicitly or obtain a design change, never silently become “static success”.
4. An authorized native preview must exercise static composition and different animation phases before reporting visual quality. Preserve source/recipe/masks, coordinate transforms, timing, pack/binary identity and owner feedback so another run can reuse the experience without re-deriving it.
5. Restricted art-direction/layout workers continue returning their EXISTING schema. Until the new stages and schemas are implemented, express relevant intent only in existing brief/geometry/quality-check text; do not invent a `mixed` UI mode, motion fields, file-writing tools or deployment privileges.

Current boundary: native mixed motion and the manual builder exist; Studio's automatic layer extraction, mixed-motion UI and end-to-end automation do not. These embedded instructions are a reusable specification and design reference, not implementation of those missing stages. Global memory alone is not consumed by a restricted generation worker; this section is intentionally in the allowlisted frozen quality-gates document.
