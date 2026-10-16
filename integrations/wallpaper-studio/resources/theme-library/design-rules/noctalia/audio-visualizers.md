# Desktop audio references for wallpaper-driven skins

Six native Noctalia desktop modes, selected by the existing `fancy_audio_visualizer` widget's `visualization_mode` setting. Not bar widgets, not HTML overlays, not baked-in wallpaper animations.

| Mode id | Reference | Good fit | Actual input |
| --- | --- | --- | --- |
| `ribbons` | Three ribbons + fine highlight | Flowing horizontal negative space | Smoothed frequency bands |
| `contour` | Single spectral contour | Fine engraving, horizon, technical line | Smoothed frequency bands, not PCM |
| `bars` | Radial bars | Seal, instrument, halo | Mirrored frequency bands |
| `wave` | Continuous wave ring | Liquid rim, portal, eclipse | Interpolated polar spectrum, not PCM |
| `mist` | Three translucent ridges | Ink wash, fog, shallow landscape rhythm | Chronological RMS energy, 128 samples × 50 ms |
| `ornament` | Three anchored pendants | Jewelry, chimes, sparse vertical geometry | Low/mid/high spectral energy |

## Choosing a per-skin reference

`audio-visualizers.json` is the machine-readable source: geometry suggestions, signal meaning, material translations, idle/motion behavior and valid native settings for every mode. Choose the preset by its explicit native mode id. Use the desktop editor to create a Fancy Audio Visualizer, choose its mode, colors, output and placement. Keep the widget box in empty wallpaper space. Several independent desktop instances may choose different modes; capture remains shared through PipeWireSpectrum.

Color settings use Noctalia's `primary` and `secondary` palette roles so skins can recolor them without rewriting shaders. `sensitivity`, `wave_thickness` and mode-specific controls determine amplitude/line weight. Polar modes fit the smaller box dimension. Horizontal modes fill the box. `rotation_speed=0` avoids autonomous rotation; the global motion policy also disables it. New modes have no independent idle animation. `fade_when_idle=false` retains a quiet static baseline/ornament; true hides idle content.

Do not copy a face, furniture or background from a reference into a skin. Transfer spacing, rhythm, material, light and palette. Avoid reserving decorative space over controls, desktop icons or the wallpaper's focal subject. The catalogue does not choose a monitor or mutate a user's widget order/configuration.

## Generation integration and boundary

The modern Noctalia generator consumes both this document and the complete `audio-visualizers.json` through `theme_guidance.TARGETS['noctalia']`. Before model spend, the existing freeze mechanism saves their exact bytes and SHA-256 into the job's `pipeline/theme-guidance`; `noctalia_generator._subject` embeds that verified snapshot into the actual image-tool subject. `system-design` also inherits these references. Window-frame targets do not receive this desktop-only catalogue.

Choose a visualizer appropriate to the skin's material, rhythm and negative space, not a compulsory six-widget collage. In a whole-skin scene, depict a visualizer only on an actual desktop surface permitted by the native surface inventory. For a single-surface image, use this anatomy only when that surface is the matching desktop visualizer; do not add pendants or graphs to unrelated bar, dialog or application components. Keep the chosen native mode/settings as the reference; do not invent shader code, a new capture process, recipe keys or automatic placement.

The existing strict native Recipe v1 and its safe apply boundary remain unchanged. Generation knowledge does not authorize installation, overwrite user widget configuration or add executable recipe fields. Validate the installed native mode capability before manually applying a preset. The selected modern generator snapshot is included in this worktree; its original untracked checkout remains untouched.

## Preview provenance

[audio-visualizers.gif](audio-visualizers.gif) is the owner-approved six-concept illustration (1000×900, six seconds, synthetic periodic audio). It is **not a capture of the native widget** and is not evidence of PipeWire or Wayland behavior. Native renderer captures, when produced by the regression test, are separate local verification artifacts.

The ribbon idea is inspired by [tetoOS's desktop Visualizer.qml](https://github.com/saxxumm/tetoOS). The remaining concepts were locally designed; tetoOS is not claimed to implement these six modes. Native code uses Noctalia's existing GLES visualizer and shared audio service, not a copied QML/CAVA runtime.
