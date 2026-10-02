#pragma once

#include <array>
#include <expected>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace kitty_skins {

enum class Filter { nearest, linear };

// Where an anchored one-shot region is pinned inside the decorated outer box.
enum class Anchor { top_left, top_center, top_right,
                    bottom_left, bottom_center, bottom_right,
                    center_left, center_right };

// How a region participates in the adaptive layout. Rails and column shafts are
// the only flexible roles; everything else is placed once at its natural size.
enum class RegionRole {
    corner_top_left,
    corner_top_right,
    corner_bottom_left,
    corner_bottom_right,
    rail_top,
    rail_bottom,
    column_left_top,
    column_left_middle,
    column_left_bottom,
    column_right_top,
    column_right_middle,
    column_right_bottom,
    ornament,
};

// Which axis a flexible region repeats along instead of stretching.
enum class RepeatAxis { none, horizontal, vertical };

struct Insets { double left, right, top, bottom; };

struct SourceRect { int x, y, width, height; };

// One semantic region of an atlas. The rectangle stays in source-atlas pixels;
// the renderer samples it directly through UV coordinates.
struct RegionSpec {
    std::string id;
    bool        exactAtlas = false; // true: sample exact.png, false: adaptive.png
    SourceRect  rect;
    RegionRole  role = RegionRole::ornament;
    Anchor      anchor = Anchor::top_left;
    double      offsetX = 0.0; // logical pixels, applied at monitor scale
    double      offsetY = 0.0;
    RepeatAxis  repeat = RepeatAxis::none;
    int         zIndex = 0;
    // Flexible-run opt-in: the segment keeps its natural long-axis size
    // (sourceLength * scale) instead of stretching; rail/column-middle with
    // repeat none only. Fixed art is never tiled.
    bool        fixed = false;
};

struct ExactMode {
    double aspect = 1.5;
    double aspectTolerance = 0.06;
    double minWidth = 900.0;
    double minHeight = 600.0;
};

struct AdaptiveMode {
    double scale = 0.45;
    double minClientWidth = 560.0;
    double minClientHeight = 360.0;
};

// One flowing wax stream: a molten bead travelling down `x` from `startY` to
// `endY`, with its nominal bead width and loop timing. All values are effect
// pixels except `period` (seconds) and `phase` ([0, 1) loop offset).
struct WaxStream {
    double x{};
    double startY{};
    double endY{};
    double width{};
    double period{};
    double phase{};
};

// Optional flowing wax driven by a per-effect RGBA visibility mask. The mask is
// an exact-region raster: its alpha gates the wax only, leaving flame and light
// independent. Streams are authored in effect pixels.
struct CandleWaxSpec {
    std::filesystem::path  mask;
    std::vector<WaxStream> streams;
};

// Optional animated overlay: two independently moving flames plus the warm light
// they cast on the surrounding wax and stone. The effect is authored against one
// ornament region and its exact source rectangle; a pack without one stays purely
// static. `flameRects` are local to the effect raster and `wicks` anchor each
// flame inside its own rectangle, both in effect pixels. `wax` is absent unless
// the pack declares flowing streams.
struct CandleEffectSpec {
    std::string                            regionId;
    std::filesystem::path                  flames;
    std::filesystem::path                  light;
    std::array<SourceRect, 2>              flameRects;
    std::array<std::array<double, 2>, 2>   wicks;
    std::optional<CandleWaxSpec>           wax;
    int                                    width = 0;
    int                                    height = 0;
};

// One pulsing accent overlay: a localized colored RGBA highlight sized exactly
// to one existing one-shot ornament region, faded with a cosine whose loop
// timing is `period` seconds and `phase` ([0, 1) loop offset). `minOpacity`
// and `maxOpacity` are finite values in [0, 1] with min <= max.
struct AccentEffectSpec {
    std::string           regionId;
    std::filesystem::path texture;
    double                period     = 0.0;
    double                phase      = 0.0;
    double                minOpacity = 0.0;
    double                maxOpacity = 1.0;
};

// Optional silver material motion over the whole decoration: a full-atlas RGBA
// mask whose alpha gates the painted silver only, a smooth color cycle period
// in seconds and a restrained tint strength in [0, 1]. The atlases' alpha is
// never changed; only their RGB is gently modulated.
struct SilverEffectSpec {
    std::filesystem::path mask;
    double                period   = 12.0;
    double                strength = 0.25;
};

// One independently moving ornament petal inside a layered flower effect. The
// texture is the extracted real petal raster with transparent swept padding;
// `pivot` is the rotation centre in effect pixels and `angle` the signed peak
// displacement in degrees (never zero, |angle| <= 12). `phase` is a per-petal
// loop offset in [0, 1).
struct FlowerPetalSpec {
    std::filesystem::path  texture;
    std::array<double, 2>  pivot{};
    double                 angle = 0.0;
    double                 phase = 0.0;
};

// One growing/bursting pearl, optionally combined with independently rocking
// petals: a bubble may be declared alone or alongside zero to six petals.
// The texture is an RGBA canvas exactly the referenced region's pixel
// dimensions holding only the original pearl body at its source position.
// `center` is the pearl centre and `radii` the enclosing axis-aligned source
// radii, both in region-local pixels. `outward` is a non-zero direction vector
// the runtime normalizes; `spread` is the additional outward droplet travel
// beyond the pearl radius, in region pixels with spread > 0 and <= 128.
struct BubbleSpec {
    std::filesystem::path texture;
    std::array<double, 2> center{};
    std::array<double, 2> radii{};
    std::array<double, 2> outward{};
    double                spread = 0.0;
};

// One layered flower: a repaired stationary ornament raster plus fixed
// foreground (roots/opals) composited by the runtime with the moving petals.
// All rasters are RGBA and exactly the referenced region's pixel dimensions.
// `period` is the loop length in seconds and `phase` its [0, 1) offset.
// `petals` and `bubble` are independent: a bubble may be declared with up to
// six petals, a petal-only effect declares 1..6 petals, and at least one of the
// two is always present. `petals` is capped at six regardless of the bubble.
struct FlowerEffectSpec {
    std::string                  regionId;
    std::filesystem::path        background;
    std::filesystem::path        foreground;
    double                       period = 0.0;
    double                       phase = 0.0;
    std::vector<FlowerPetalSpec> petals;
    std::optional<BubbleSpec>    bubble;
    int                          width = 0;
    int                          height = 0;
};

struct SkinPack {
    int         schema = 2;
    std::string id;
    std::string name;
    Filter      filter = Filter::nearest;

    // Pinned source geometry. Every region rectangle is expressed against it, and
    // both generated atlases must match it exactly.
    int         sourceWidth = 0;
    int         sourceHeight = 0;
    std::string sourceSha256;

    // Frame band widths. Also the source aperture profile: the bands plus the
    // aperture opening tile the source exactly.
    Insets aperture;

    // Optional physical frame thickness, in the same source-pixel units as the
    // aperture. Absent for legacy packs, where the aperture bands double as the
    // frame. When present, `aperture` still records the measured source bands and
    // this only states how far the stone must reach at that scale.
    std::optional<Insets> frameInsets;

    ExactMode    exact;
    AdaptiveMode adaptive;

    std::vector<RegionSpec> regions;

    // Animated overlay, present only for packs that request it.
    std::optional<CandleEffectSpec> candleEffect;

    // Optional pulsing accents (at most 8), each bound to an existing one-shot
    // ornament region and preloaded as its own small RGBA texture.
    std::vector<AccentEffectSpec> accentEffects;

    // Optional silver material motion; absent means a purely static pack.
    std::optional<SilverEffectSpec> silverEffect;

    // Opt-in layered frame: in exact mode every ornament region is drawn once
    // through its anchor transform (uniform source scale), instead of the legacy
    // whole-atlas or effect-only behaviour. Only meaningful together with
    // frameInsets; absent on legacy packs, which keep their existing layout.
    bool layeredOrnaments = false;

    // Optional layered flower motions (at most 4), each bound to one ornament
    // region whose stationary parts the runtime composites itself. Empty for
    // every pack without flowers.
    std::vector<FlowerEffectSpec> flowerEffects;

    // Resolved absolute paths, confined to the pack root.
    std::filesystem::path exactAtlas;
    std::filesystem::path adaptiveAtlas;
    std::filesystem::path root;
    std::filesystem::path kittyConfig;
};

struct ValidationError {
    std::filesystem::path path;
    std::string field;
    std::string message;
};

template<class T>
using Result = std::expected<T, ValidationError>;

}
