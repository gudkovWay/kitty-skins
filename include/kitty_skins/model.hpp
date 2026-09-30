#pragma once

#include <array>
#include <expected>
#include <filesystem>
#include <string>
#include <vector>

namespace kitty_skins {

enum class Filter { nearest, linear };

// Where an anchored one-shot region is pinned inside the decorated outer box.
enum class Anchor { top_left, top_center, top_right,
                    bottom_left, bottom_center, bottom_right };

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

    ExactMode    exact;
    AdaptiveMode adaptive;

    std::vector<RegionSpec> regions;

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
