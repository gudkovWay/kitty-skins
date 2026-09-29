#pragma once

#include <array>
#include <expected>
#include <filesystem>
#include <string>
#include <vector>

namespace kitty_skins {

enum class Filter { nearest, linear };
enum class EdgeMode { tile, stretch, mirror };
enum class Anchor { top_left, top_center, top_right,
                    bottom_left, bottom_center, bottom_right };

struct Insets { double left, right, top, bottom; };
struct LogicalSize { double width, height; };
struct FrameSpec {
    std::filesystem::path asset;
    Insets slices;
    std::array<EdgeMode, 4> edges; // left, right, top, bottom
};
struct TierSpec {
    std::string id;
    LogicalSize minimum;
    double scale;
    Insets extents;
    std::vector<std::string> visibleSprites;
};
struct SpriteSpec {
    std::string id;
    std::filesystem::path asset;
    Anchor anchor;
    double offsetX;
    double offsetY;
    double scale;
    int zIndex;
};
struct SkinPack {
    int schema;
    std::string id;
    std::string name;
    Filter filter;
    FrameSpec frame;
    std::vector<TierSpec> tiers;
    std::vector<SpriteSpec> sprites;
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
