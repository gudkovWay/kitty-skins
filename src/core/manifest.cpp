#include "kitty_skins/manifest.hpp"

#include <png.h>
#include <setjmp.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <optional>
#include <regex>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include <glaze/glaze.hpp>

namespace kitty_skins {
namespace {

struct JsonInsets {
    double left{};
    double right{};
    double top{};
    double bottom{};
};

struct JsonSource {
    int         width{};
    int         height{};
    std::string sha256;
};

struct JsonExact {
    std::string atlas;
    double      aspect{};
    double      aspect_tolerance{};
    double      min_width{};
    double      min_height{};
};

struct JsonAdaptive {
    std::string atlas;
    double      scale{};
    double      min_client_width{};
    double      min_client_height{};
};

struct JsonRegion {
    std::string          id;
    std::string          atlas;
    std::array<int, 4>   rect{};
    std::string          role;
    std::string          anchor;
    std::array<double, 2> offset{};
    std::string          repeat;
    int                  z{};
    bool                 fixed = false;
};

struct JsonWaxStream {
    double x{};
    double start_y{};
    double end_y{};
    double width{};
    double period{};
    double phase{};
};

struct JsonWax {
    std::string                     mask;
    std::vector<JsonWaxStream>      streams;
};

struct JsonCandleEffect {
    std::string                          region;
    std::string                          flames;
    std::string                          light;
    std::array<std::array<int, 4>, 2>    flame_rects{};
    std::array<std::array<double, 2>, 2> wicks{};
    std::optional<JsonWax>               wax;
};

struct JsonAccentEffect {
    std::string region;
    std::string texture;
    double      period{};
    double      phase{};
    double      min_opacity{};
    double      max_opacity{};
};

struct JsonSilverEffect {
    std::string mask;
    double      period{};
    double      strength{};
};

struct JsonFlowerPetal {
    std::string           texture;
    std::array<double, 2> pivot{};
    double                angle{};
    double                phase{};
};

struct JsonBubble {
    std::string           texture;
    std::array<double, 2> center{};
    std::array<double, 2> radii{};
    std::array<double, 2> outward{};
    double                spread{};
};

struct JsonFlowerEffect {
    std::string                  region;
    std::string                  background;
    std::string                  foreground;
    double                       period{};
    double                       phase{};
    std::vector<JsonFlowerPetal> petals;
    std::optional<JsonBubble>    bubble;
};

struct JsonManifest {
    int                    schema{};
    std::string            id;
    std::string            name;
    std::string            filter;
    JsonSource             source;
    JsonInsets             aperture;
    std::optional<JsonInsets> frame_insets;
    JsonExact              exact;
    JsonAdaptive           adaptive;
    std::vector<JsonRegion> regions;
    std::optional<JsonCandleEffect> candle_effect;
    std::vector<JsonAccentEffect>   accent_effects;
    std::optional<JsonSilverEffect> silver_effect;
    bool                            layered_ornaments = false;
    std::vector<JsonFlowerEffect>   flower_effects;
};

// Schema 1 carries keys schema 2 no longer has (frame, tiers, sprites), so a
// strict decode would fail on the first unknown key before the version could be
// read. The probe tolerates unknown keys purely to recover the version number;
// the manifest itself is then decoded strictly.
struct JsonSchemaProbe {
    int schema{};
};

struct TolerantReadOpts : glz::opts {
    bool error_on_unknown_keys = false;
};

glz::error_ctx readSchemaProbe(JsonSchemaProbe& probe, const std::string& buffer) {
    glz::context ctx{};
    return glz::read<TolerantReadOpts{}>(probe, buffer, ctx);
}

ValidationError fail(std::filesystem::path path, std::string field, std::string message) {
    return ValidationError{std::move(path), std::move(field), std::move(message)};
}

bool finite(double value) noexcept {
    return std::isfinite(value);
}

bool isWithin(const std::filesystem::path& root, const std::filesystem::path& target) {
    auto rootIt = root.begin();
    auto targetIt = target.begin();
    for (; rootIt != root.end(); ++rootIt, ++targetIt) {
        if (targetIt == target.end() || *rootIt != *targetIt)
            return false;
    }
    return true;
}

bool decodeFilter(const std::string& value, Filter& out) {
    if (value == "nearest") {
        out = Filter::nearest;
        return true;
    }
    if (value == "linear") {
        out = Filter::linear;
        return true;
    }
    return false;
}

bool decodeAnchor(const std::string& value, Anchor& out) {
    if (value == "top-left") {
        out = Anchor::top_left;
        return true;
    }
    if (value == "top-center") {
        out = Anchor::top_center;
        return true;
    }
    if (value == "top-right") {
        out = Anchor::top_right;
        return true;
    }
    if (value == "bottom-left") {
        out = Anchor::bottom_left;
        return true;
    }
    if (value == "bottom-center") {
        out = Anchor::bottom_center;
        return true;
    }
    if (value == "bottom-right") {
        out = Anchor::bottom_right;
        return true;
    }
    if (value == "center-left") {
        out = Anchor::center_left;
        return true;
    }
    if (value == "center-right") {
        out = Anchor::center_right;
        return true;
    }
    return false;
}

bool decodeRole(const std::string& value, RegionRole& out) {
    if (value == "corner-top-left") {
        out = RegionRole::corner_top_left;
        return true;
    }
    if (value == "corner-top-right") {
        out = RegionRole::corner_top_right;
        return true;
    }
    if (value == "corner-bottom-left") {
        out = RegionRole::corner_bottom_left;
        return true;
    }
    if (value == "corner-bottom-right") {
        out = RegionRole::corner_bottom_right;
        return true;
    }
    if (value == "rail-top") {
        out = RegionRole::rail_top;
        return true;
    }
    if (value == "rail-bottom") {
        out = RegionRole::rail_bottom;
        return true;
    }
    if (value == "column-left-top") {
        out = RegionRole::column_left_top;
        return true;
    }
    if (value == "column-left-middle") {
        out = RegionRole::column_left_middle;
        return true;
    }
    if (value == "column-left-bottom") {
        out = RegionRole::column_left_bottom;
        return true;
    }
    if (value == "column-right-top") {
        out = RegionRole::column_right_top;
        return true;
    }
    if (value == "column-right-middle") {
        out = RegionRole::column_right_middle;
        return true;
    }
    if (value == "column-right-bottom") {
        out = RegionRole::column_right_bottom;
        return true;
    }
    if (value == "ornament") {
        out = RegionRole::ornament;
        return true;
    }
    return false;
}

bool decodeRepeat(const std::string& value, RepeatAxis& out) {
    if (value == "none") {
        out = RepeatAxis::none;
        return true;
    }
    if (value == "x") {
        out = RepeatAxis::horizontal;
        return true;
    }
    if (value == "y") {
        out = RepeatAxis::vertical;
        return true;
    }
    return false;
}

// Roles that the adaptive layout always needs exactly one of.
bool isSingleCorner(RegionRole role) {
    return role == RegionRole::corner_top_left || role == RegionRole::corner_top_right ||
           role == RegionRole::corner_bottom_left || role == RegionRole::corner_bottom_right;
}

bool isFlexible(RegionRole role) {
    switch (role) {
        case RegionRole::rail_top:
        case RegionRole::rail_bottom:
        case RegionRole::column_left_middle:
        case RegionRole::column_right_middle:
            return true;
        default:
            return false;
    }
}

enum class PngProbe { ok, notPng, readError };

PngProbe probePngRgba(const std::filesystem::path& file, bool& rgba, int& width, int& height) {
    rgba   = false;
    width  = 0;
    height = 0;

    std::FILE* handle = std::fopen(file.c_str(), "rb");
    if (handle == nullptr)
        return PngProbe::readError;

    png_structp png = png_create_read_struct(PNG_LIBPNG_VER_STRING, nullptr, nullptr, nullptr);
    if (png == nullptr) {
        std::fclose(handle);
        return PngProbe::readError;
    }

    png_infop info = png_create_info_struct(png);
    if (info == nullptr) {
        png_destroy_read_struct(&png, nullptr, nullptr);
        std::fclose(handle);
        return PngProbe::readError;
    }

    if (setjmp(png_jmpbuf(png)) != 0) {
        png_destroy_read_struct(&png, &info, nullptr);
        std::fclose(handle);
        return PngProbe::readError;
    }

    png_init_io(png, handle);

    unsigned char signature[8] = {};
    const size_t  read         = std::fread(signature, 1, sizeof(signature), handle);
    if (read != sizeof(signature) || png_sig_cmp(signature, 0, sizeof(signature)) != 0) {
        png_destroy_read_struct(&png, &info, nullptr);
        std::fclose(handle);
        return PngProbe::notPng;
    }

    png_set_sig_bytes(png, static_cast<int>(sizeof(signature)));
    png_read_info(png, info);
    rgba   = png_get_color_type(png, info) == PNG_COLOR_TYPE_RGBA;
    width  = static_cast<int>(png_get_image_width(png, info));
    height = static_cast<int>(png_get_image_height(png, info));

    png_destroy_read_struct(&png, &info, nullptr);
    std::fclose(handle);
    return PngProbe::ok;
}

bool isSha256(const std::string& value) {
    if (value.size() != 64)
        return false;
    return std::all_of(value.begin(), value.end(), [](unsigned char character) {
        return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
    });
}

}

Result<SkinPack> loadAndValidatePack(const std::filesystem::path& root) {
    const std::filesystem::path skinJson = root / "skin.json";

    std::error_code ec;
    const std::filesystem::path canonicalRoot = std::filesystem::weakly_canonical(root, ec);
    if (ec)
        return std::unexpected(fail(root, "root", "cannot resolve pack root: " + ec.message()));

    std::ifstream stream(skinJson, std::ios::binary);
    if (!stream)
        return std::unexpected(fail(skinJson, "skin.json", "cannot open manifest for reading"));

    std::string buffer((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    if (stream.bad())
        return std::unexpected(fail(skinJson, "skin.json", "cannot read manifest"));

    JsonSchemaProbe probe{};
    if (const glz::error_ctx probeError = readSchemaProbe(probe, buffer))
        return std::unexpected(fail(skinJson, "skin.json",
                                    "invalid manifest: " + glz::format_error(probeError, buffer)));

    if (probe.schema == 1)
        return std::unexpected(
            fail(skinJson, "schema", "schema 1 is obsolete and no longer supported; migrate the pack to schema 2"));
    if (probe.schema != 2)
        return std::unexpected(
            fail(skinJson, "schema", "unsupported schema " + std::to_string(probe.schema) + "; expected 2"));

    JsonManifest manifest{};
    const glz::error_ctx parse = glz::read_json(manifest, buffer);
    std::string parseMessage;
    if (parse)
        parseMessage = glz::format_error(parse, buffer);
    buffer.clear();
    buffer.shrink_to_fit();
    if (parse)
        return std::unexpected(fail(skinJson, "skin.json", "invalid manifest: " + parseMessage));

    static const std::regex idPattern(R"(^[a-z0-9]+(?:-[a-z0-9]+)*$)");
    if (!std::regex_match(manifest.id, idPattern))
        return std::unexpected(fail(skinJson, "id", "must match ^[a-z0-9]+(?:-[a-z0-9]+)*$"));

    SkinPack pack{};
    pack.schema = manifest.schema;
    pack.id     = manifest.id;

    if (manifest.name.empty())
        return std::unexpected(fail(skinJson, "name", "must not be empty"));
    pack.name = manifest.name;

    if (!decodeFilter(manifest.filter, pack.filter))
        return std::unexpected(fail(skinJson, "filter", "must be nearest or linear"));

    if (manifest.source.width < 1 || manifest.source.height < 1)
        return std::unexpected(fail(skinJson, "source", "width and height must be positive"));
    if (!isSha256(manifest.source.sha256))
        return std::unexpected(fail(skinJson, "source.sha256", "must be a 64-character lowercase hex digest"));
    pack.sourceWidth  = manifest.source.width;
    pack.sourceHeight = manifest.source.height;
    pack.sourceSha256 = manifest.source.sha256;

    const std::array<std::pair<const char*, double>, 4> bands{{{"left", manifest.aperture.left},
                                                              {"right", manifest.aperture.right},
                                                              {"top", manifest.aperture.top},
                                                              {"bottom", manifest.aperture.bottom}}};
    for (const auto& [name, value] : bands) {
        if (!finite(value) || value < 0.0)
            return std::unexpected(fail(skinJson, std::string("aperture.") + name, "must be finite and non-negative"));
    }
    // Aperture plus bands must exactly cover the source: the opening is whatever
    // is left between the bands, so the bands may not meet or overlap.
    if (manifest.aperture.left + manifest.aperture.right >= static_cast<double>(manifest.source.width))
        return std::unexpected(fail(skinJson, "aperture", "left and right bands leave no aperture opening"));
    if (manifest.aperture.top + manifest.aperture.bottom >= static_cast<double>(manifest.source.height))
        return std::unexpected(fail(skinJson, "aperture", "top and bottom bands leave no aperture opening"));
    pack.aperture = Insets{manifest.aperture.left, manifest.aperture.right, manifest.aperture.top,
                           manifest.aperture.bottom};

    // Optional physical frame thickness. The aperture keeps its meaning as the
    // measured source bands; frame_insets only says how far the stone should
    // reach, in the same source-pixel units. A side with no source band carries no
    // material to scale, so it can never be grown.
    if (manifest.frame_insets) {
        const JsonInsets& insets = *manifest.frame_insets;
        const std::array<std::pair<const char*, double>, 4> targets{{{"left", insets.left},
                                                                    {"right", insets.right},
                                                                    {"top", insets.top},
                                                                    {"bottom", insets.bottom}}};
        for (const auto& [name, value] : targets) {
            if (!finite(value) || value < 0.0)
                return std::unexpected(
                    fail(skinJson, std::string("frame_insets.") + name, "must be finite and non-negative"));
        }
        if (insets.left + insets.right >= static_cast<double>(manifest.source.width))
            return std::unexpected(fail(skinJson, "frame_insets", "left and right insets leave no aperture opening"));
        if (insets.top + insets.bottom >= static_cast<double>(manifest.source.height))
            return std::unexpected(fail(skinJson, "frame_insets", "top and bottom insets leave no aperture opening"));

        const std::array<std::pair<const char*, std::pair<double, double>>, 4> growable{{
            {"left", {manifest.aperture.left, insets.left}},
            {"right", {manifest.aperture.right, insets.right}},
            {"top", {manifest.aperture.top, insets.top}},
            {"bottom", {manifest.aperture.bottom, insets.bottom}},
        }};
        for (const auto& [name, side] : growable) {
            if (side.first == 0.0 && side.second != 0.0)
                return std::unexpected(fail(skinJson, std::string("frame_insets.") + name,
                                            "cannot grow a side with no source band to scale"));
        }

        pack.frameInsets = Insets{insets.left, insets.right, insets.top, insets.bottom};
    }

    // The layered ornament path is an exact-mode behaviour of the physical frame:
    // without frame_insets the legacy whole-atlas path already contains every
    // ornament, so the flag would silently do nothing. Reject it instead.
    if (manifest.layered_ornaments && !pack.frameInsets)
        return std::unexpected(fail(skinJson, "layered_ornaments", "requires frame_insets"));
    pack.layeredOrnaments = manifest.layered_ornaments;

    if (!finite(manifest.exact.aspect) || manifest.exact.aspect <= 0.0)
        return std::unexpected(fail(skinJson, "exact.aspect", "must be finite and greater than zero"));
    if (!finite(manifest.exact.aspect_tolerance) || manifest.exact.aspect_tolerance < 0.0)
        return std::unexpected(fail(skinJson, "exact.aspect_tolerance", "must be finite and non-negative"));
    if (!finite(manifest.exact.min_width) || manifest.exact.min_width <= 0.0 ||
        !finite(manifest.exact.min_height) || manifest.exact.min_height <= 0.0)
        return std::unexpected(fail(skinJson, "exact.min_width", "minimum outer size must be finite and positive"));
    if (manifest.exact.atlas.empty())
        return std::unexpected(fail(skinJson, "exact.atlas", "must not be empty"));
    pack.exact = ExactMode{manifest.exact.aspect, manifest.exact.aspect_tolerance, manifest.exact.min_width,
                           manifest.exact.min_height};

    if (!finite(manifest.adaptive.scale) || manifest.adaptive.scale <= 0.0)
        return std::unexpected(fail(skinJson, "adaptive.scale", "must be finite and greater than zero"));
    if (!finite(manifest.adaptive.min_client_width) || manifest.adaptive.min_client_width <= 0.0 ||
        !finite(manifest.adaptive.min_client_height) || manifest.adaptive.min_client_height <= 0.0)
        return std::unexpected(fail(skinJson, "adaptive.min_client_width", "minimum client size must be finite and positive"));
    if (manifest.adaptive.atlas.empty())
        return std::unexpected(fail(skinJson, "adaptive.atlas", "must not be empty"));
    pack.adaptive = AdaptiveMode{manifest.adaptive.scale, manifest.adaptive.min_client_width,
                                 manifest.adaptive.min_client_height};

    const auto resolveAsset = [&](const std::string& relative, const std::string& field,
                                  std::filesystem::path& out) -> std::optional<ValidationError> {
        const std::filesystem::path asset{relative};
        if (asset.is_absolute())
            return fail(skinJson, field, "asset path must be relative to the pack root");
        std::error_code assetEc;
        const std::filesystem::path resolved = std::filesystem::weakly_canonical(canonicalRoot / asset, assetEc);
        if (assetEc)
            return fail(canonicalRoot / asset, field, "cannot resolve asset path: " + assetEc.message());
        if (!isWithin(canonicalRoot, resolved))
            return fail(canonicalRoot / asset, field, "asset path escapes the pack root");
        out = resolved;
        return std::nullopt;
    };

    if (const auto error = resolveAsset(manifest.exact.atlas, "exact.atlas", pack.exactAtlas))
        return std::unexpected(*error);
    if (const auto error = resolveAsset(manifest.adaptive.atlas, "adaptive.atlas", pack.adaptiveAtlas))
        return std::unexpected(*error);

    // Both atlases are source-pixel rasters: region rectangles index them directly.
    const auto checkAtlas = [&](const std::filesystem::path& asset, const std::string& field)
        -> std::optional<ValidationError> {
        std::error_code fileEc;
        if (!std::filesystem::is_regular_file(asset, fileEc))
            return fail(asset, field, "PNG atlas does not exist");

        bool rgba = false;
        int  width = 0;
        int  height = 0;
        switch (probePngRgba(asset, rgba, width, height)) {
        case PngProbe::ok:
            break;
        case PngProbe::notPng:
            return fail(asset, field, "atlas is not a PNG image");
        case PngProbe::readError:
            return fail(asset, field, "cannot decode PNG header");
        }
        if (!rgba)
            return fail(asset, field, "PNG color type must be RGBA");
        if (width != manifest.source.width || height != manifest.source.height)
            return fail(asset, field, "atlas is " + std::to_string(width) + "x" + std::to_string(height) +
                                          ", expected " + std::to_string(manifest.source.width) + "x" +
                                          std::to_string(manifest.source.height));
        return std::nullopt;
    };

    if (const auto error = checkAtlas(pack.exactAtlas, "exact.atlas"))
        return std::unexpected(*error);
    if (const auto error = checkAtlas(pack.adaptiveAtlas, "adaptive.atlas"))
        return std::unexpected(*error);

    // Region roles the adaptive layout cannot be built without.
    std::array<int, 13> roleCounts{};
    const auto roleIndex = [](RegionRole role) { return static_cast<size_t>(role); };

    std::unordered_set<std::string> regionIds;
    pack.regions.reserve(manifest.regions.size());

    for (size_t index = 0; index < manifest.regions.size(); ++index) {
        const JsonRegion& json = manifest.regions[index];
        const std::string prefix = "regions[" + std::to_string(index) + "]";

        if (json.id.empty())
            return std::unexpected(fail(skinJson, prefix + ".id", "must not be empty"));
        if (!regionIds.insert(json.id).second)
            return std::unexpected(fail(skinJson, prefix + ".id", "duplicate region id " + json.id));

        bool exactAtlas = false;
        if (json.atlas == "exact")
            exactAtlas = true;
        else if (json.atlas != "adaptive")
            return std::unexpected(fail(skinJson, prefix + ".atlas", "must be exact or adaptive"));

        const SourceRect rect{json.rect[0], json.rect[1], json.rect[2], json.rect[3]};
        if (rect.width <= 0 || rect.height <= 0)
            return std::unexpected(fail(skinJson, prefix + ".rect", "width and height must be positive"));
        if (rect.x < 0 || rect.y < 0 || rect.x + rect.width > manifest.source.width ||
            rect.y + rect.height > manifest.source.height)
            return std::unexpected(fail(skinJson, prefix + ".rect", "rectangle is not inside the source atlas"));

        RegionRole role{};
        if (!decodeRole(json.role, role))
            return std::unexpected(fail(skinJson, prefix + ".role", "unknown region role " + json.role));

        Anchor anchor{};
        if (!decodeAnchor(json.anchor, anchor))
            return std::unexpected(fail(skinJson, prefix + ".anchor", "unknown anchor " + json.anchor));

        RepeatAxis repeat{};
        if (!decodeRepeat(json.repeat, repeat))
            return std::unexpected(fail(skinJson, prefix + ".repeat", "must be none, x or y"));

        // Only declared-flexible roles may repeat; a repeated corner or ornament
        // would tile architecture, which the spec forbids.
        if (repeat != RepeatAxis::none && !isFlexible(role))
            return std::unexpected(fail(skinJson, prefix + ".repeat", "only rail and column shaft regions may repeat"));

        // A fixed segment is a contiguous, non-tiled piece of artwork: it needs
        // a flexible run to live in and must never repeat.
        if (json.fixed && (!isFlexible(role) || repeat != RepeatAxis::none))
            return std::unexpected(fail(skinJson, prefix + ".fixed", "only rail and column shaft regions with repeat none may be fixed"));

        if (!finite(json.offset[0]) || !finite(json.offset[1]))
            return std::unexpected(fail(skinJson, prefix + ".offset", "offsets must be finite"));

        if (isSingleCorner(role) && ++roleCounts[roleIndex(role)] > 1)
            return std::unexpected(fail(skinJson, prefix + ".role", "corner role declared more than once"));

        if (role == RegionRole::column_left_top || role == RegionRole::column_left_bottom ||
            role == RegionRole::column_right_top || role == RegionRole::column_right_bottom) {
            if (++roleCounts[roleIndex(role)] > 1)
                return std::unexpected(fail(skinJson, prefix + ".role", "column cap role declared more than once"));
        }

        if (role == RegionRole::rail_top || role == RegionRole::rail_bottom ||
            role == RegionRole::column_left_middle || role == RegionRole::column_right_middle)
            ++roleCounts[roleIndex(role)];

        pack.regions.push_back(RegionSpec{json.id, exactAtlas, rect, role, anchor, json.offset[0], json.offset[1],
                                          repeat, json.z, json.fixed});
    }

    const std::array<std::pair<RegionRole, const char*>, 10> required{{
        {RegionRole::corner_top_left, "corner-top-left"},
        {RegionRole::corner_top_right, "corner-top-right"},
        {RegionRole::corner_bottom_left, "corner-bottom-left"},
        {RegionRole::corner_bottom_right, "corner-bottom-right"},
        {RegionRole::rail_top, "rail-top"},
        {RegionRole::rail_bottom, "rail-bottom"},
        {RegionRole::column_left_top, "column-left-top"},
        {RegionRole::column_left_bottom, "column-left-bottom"},
        {RegionRole::column_right_top, "column-right-top"},
        {RegionRole::column_right_bottom, "column-right-bottom"},
    }};
    for (const auto& [role, name] : required) {
        if (roleCounts[roleIndex(role)] < 1)
            return std::unexpected(fail(skinJson, "regions", std::string("missing required region role ") + name));
    }
    if (roleCounts[roleIndex(RegionRole::column_left_middle)] < 1 ||
        roleCounts[roleIndex(RegionRole::column_right_middle)] < 1)
        return std::unexpected(fail(skinJson, "regions", "each column needs at least one middle shaft region"));

    // Optional candle effect. Absence is the normal static case: nothing below
    // runs, so a pack without one pays neither validation nor allocation.
    if (manifest.candle_effect) {
        const JsonCandleEffect& effect = *manifest.candle_effect;

        if (effect.region.empty())
            return std::unexpected(fail(skinJson, "candle_effect.region", "must not be empty"));

        // The effect is authored against exactly one existing ornament region;
        // region ids are already unique, so a plain lookup is exact.
        const RegionSpec* region = nullptr;
        for (const RegionSpec& candidate : pack.regions) {
            if (candidate.id == effect.region) {
                region = &candidate;
                break;
            }
        }
        if (region == nullptr)
            return std::unexpected(fail(skinJson, "candle_effect.region",
                                        "references unknown region " + effect.region));
        if (region->role != RegionRole::ornament)
            return std::unexpected(fail(skinJson, "candle_effect.region", "region role must be ornament"));
        if (region->repeat != RepeatAxis::none)
            return std::unexpected(fail(skinJson, "candle_effect.region", "region must not repeat"));

        // Effect rasters follow the ornament's source rectangle; bound them so the
        // offscreen effect framebuffer stays small.
        const int effectWidth  = region->rect.width;
        const int effectHeight = region->rect.height;
        if (effectWidth > 2048 || effectHeight > 2048)
            return std::unexpected(
                fail(skinJson, "candle_effect.region", "referenced region is larger than 2048 pixels on a side"));

        std::array<SourceRect, 2> flameRects{};
        for (size_t index = 0; index < flameRects.size(); ++index) {
            const std::array<int, 4>& raw   = effect.flame_rects[index];
            const std::string         field = "candle_effect.flame_rects[" + std::to_string(index) + "]";
            const SourceRect          rect{raw[0], raw[1], raw[2], raw[3]};
            if (rect.width <= 0 || rect.height <= 0)
                return std::unexpected(fail(skinJson, field, "width and height must be positive"));
            if (rect.x < 0 || rect.y < 0 || rect.x > effectWidth || rect.y > effectHeight ||
                rect.width > effectWidth - rect.x || rect.height > effectHeight - rect.y)
                return std::unexpected(fail(skinJson, field, "rectangle is not inside the effect region"));
            flameRects[index] = rect;
        }

        const SourceRect& firstFlame  = flameRects[0];
        const SourceRect& secondFlame = flameRects[1];
        if (firstFlame.x < secondFlame.x + secondFlame.width && secondFlame.x < firstFlame.x + firstFlame.width &&
            firstFlame.y < secondFlame.y + secondFlame.height && secondFlame.y < firstFlame.y + firstFlame.height)
            return std::unexpected(fail(skinJson, "candle_effect.flame_rects", "flame rectangles must not overlap"));

        std::array<std::array<double, 2>, 2> wicks{};
        for (size_t index = 0; index < wicks.size(); ++index) {
            const std::array<double, 2>& wick  = effect.wicks[index];
            const std::string            field = "candle_effect.wicks[" + std::to_string(index) + "]";
            if (!finite(wick[0]) || !finite(wick[1]))
                return std::unexpected(fail(skinJson, field, "coordinates must be finite"));

            const SourceRect& rect = flameRects[index];
            if (wick[0] < static_cast<double>(rect.x) || wick[0] > static_cast<double>(rect.x + rect.width) ||
                wick[1] < static_cast<double>(rect.y) || wick[1] > static_cast<double>(rect.y + rect.height))
                return std::unexpected(fail(skinJson, field, "wick must lie inside its flame rectangle"));
            wicks[index] = wick;
        }

        if (effect.flames.empty())
            return std::unexpected(fail(skinJson, "candle_effect.flames", "must not be empty"));
        if (effect.light.empty())
            return std::unexpected(fail(skinJson, "candle_effect.light", "must not be empty"));

        CandleEffectSpec spec{};
        spec.regionId   = region->id;
        spec.width      = effectWidth;
        spec.height     = effectHeight;
        spec.flameRects = flameRects;
        spec.wicks      = wicks;

        if (const auto error = resolveAsset(effect.flames, "candle_effect.flames", spec.flames))
            return std::unexpected(*error);
        if (const auto error = resolveAsset(effect.light, "candle_effect.light", spec.light))
            return std::unexpected(*error);

        // Effect rasters are exact rasterisations of the referenced region: RGBA
        // and matching its pixel dimensions, like the atlases against the source.
        const auto checkEffectImage = [&](const std::filesystem::path& asset, const std::string& field)
            -> std::optional<ValidationError> {
            std::error_code fileEc;
            if (!std::filesystem::is_regular_file(asset, fileEc))
                return fail(asset, field, "PNG image does not exist");

            bool rgba      = false;
            int  imageWide = 0;
            int  imageTall = 0;
            switch (probePngRgba(asset, rgba, imageWide, imageTall)) {
            case PngProbe::ok:
                break;
            case PngProbe::notPng:
                return fail(asset, field, "image is not a PNG");
            case PngProbe::readError:
                return fail(asset, field, "cannot decode PNG header");
            }
            if (!rgba)
                return fail(asset, field, "PNG color type must be RGBA");
            if (imageWide != effectWidth || imageTall != effectHeight)
                return fail(asset, field, "image is " + std::to_string(imageWide) + "x" + std::to_string(imageTall) +
                                              ", expected " + std::to_string(effectWidth) + "x" +
                                              std::to_string(effectHeight));
            return std::nullopt;
        };

        if (const auto error = checkEffectImage(spec.flames, "candle_effect.flames"))
            return std::unexpected(*error);
        if (const auto error = checkEffectImage(spec.light, "candle_effect.light"))
            return std::unexpected(*error);

        // Optional flowing wax. Declaring it requires a mask plus one to four
        // streams; a pack without wax skips this and keeps legacy flame behavior.
        if (effect.wax) {
            const JsonWax& json = *effect.wax;

            if (json.streams.empty() || json.streams.size() > 4)
                return std::unexpected(
                    fail(skinJson, "candle_effect.wax.streams", "must declare between 1 and 4 streams"));
            if (json.mask.empty())
                return std::unexpected(fail(skinJson, "candle_effect.wax.mask", "must not be empty"));

            std::vector<WaxStream> streams;
            streams.reserve(json.streams.size());
            for (size_t index = 0; index < json.streams.size(); ++index) {
                const JsonWaxStream& raw   = json.streams[index];
                const std::string    field = "candle_effect.wax.streams[" + std::to_string(index) + "]";

                if (!finite(raw.x) || !finite(raw.start_y) || !finite(raw.end_y) || !finite(raw.width) ||
                    !finite(raw.period) || !finite(raw.phase))
                    return std::unexpected(fail(skinJson, field, "all stream numbers must be finite"));

                if (raw.width <= 0.0 || raw.width > static_cast<double>(effectWidth))
                    return std::unexpected(
                        fail(skinJson, field + ".width", "must be positive and no wider than the effect"));

                // The bead body is width wide and centred on x; keep it inside the
                // effect region so no sample falls off the mask.
                if (raw.x - raw.width / 2.0 < 0.0 || raw.x + raw.width / 2.0 > static_cast<double>(effectWidth))
                    return std::unexpected(
                        fail(skinJson, field + ".x", "stream body must stay inside the effect region"));

                if (raw.start_y < 0.0 || raw.start_y >= raw.end_y || raw.end_y >= static_cast<double>(effectHeight))
                    return std::unexpected(
                        fail(skinJson, field, "require 0 <= start_y < end_y < effect height"));

                if (raw.period < 1.0)
                    return std::unexpected(fail(skinJson, field + ".period", "must be at least one second"));

                if (raw.phase < 0.0 || raw.phase >= 1.0)
                    return std::unexpected(fail(skinJson, field + ".phase", "must be within [0, 1)"));

                streams.push_back(WaxStream{raw.x, raw.start_y, raw.end_y, raw.width, raw.period, raw.phase});
            }

            CandleWaxSpec waxSpec;
            waxSpec.streams = std::move(streams);
            if (const auto error = resolveAsset(json.mask, "candle_effect.wax.mask", waxSpec.mask))
                return std::unexpected(*error);
            if (const auto error = checkEffectImage(waxSpec.mask, "candle_effect.wax.mask"))
                return std::unexpected(*error);
            spec.wax = std::move(waxSpec);
        }

        pack.candleEffect = std::move(spec);
    }

    // Optional pulsing accents. Absence is the normal static case; at most eight
    // are accepted so a pathological manifest cannot balloon runtime resources.
    if (manifest.accent_effects.size() > 8)
        return std::unexpected(fail(skinJson, "accent_effects", "must declare at most 8 accent effects"));

    if (!manifest.accent_effects.empty()) {
        pack.accentEffects.reserve(manifest.accent_effects.size());

        for (size_t index = 0; index < manifest.accent_effects.size(); ++index) {
            const JsonAccentEffect& json  = manifest.accent_effects[index];
            const std::string       field = "accent_effects[" + std::to_string(index) + "]";

            if (json.region.empty())
                return std::unexpected(fail(skinJson, field + ".region", "must not be empty"));

            // The accent is authored against exactly one existing one-shot
            // ornament region; region ids are already unique, so a plain lookup
            // is exact.
            const RegionSpec* region = nullptr;
            for (const RegionSpec& candidate : pack.regions) {
                if (candidate.id == json.region) {
                    region = &candidate;
                    break;
                }
            }
            if (region == nullptr)
                return std::unexpected(fail(skinJson, field + ".region", "references unknown region " + json.region));
            if (region->role != RegionRole::ornament)
                return std::unexpected(fail(skinJson, field + ".region", "region role must be ornament"));
            if (region->repeat != RepeatAxis::none)
                return std::unexpected(fail(skinJson, field + ".region", "region must not repeat"));

            if (!finite(json.period) || json.period < 2.0 || json.period > 60.0)
                return std::unexpected(fail(skinJson, field + ".period", "must be finite and within [2, 60] seconds"));
            if (!finite(json.phase) || json.phase < 0.0 || json.phase >= 1.0)
                return std::unexpected(fail(skinJson, field + ".phase", "must be finite and within [0, 1)"));
            if (!finite(json.min_opacity) || json.min_opacity < 0.0 || json.min_opacity > 1.0)
                return std::unexpected(fail(skinJson, field + ".min_opacity", "must be finite and within [0, 1]"));
            if (!finite(json.max_opacity) || json.max_opacity < 0.0 || json.max_opacity > 1.0)
                return std::unexpected(fail(skinJson, field + ".max_opacity", "must be finite and within [0, 1]"));
            if (json.min_opacity > json.max_opacity)
                return std::unexpected(fail(skinJson, field, "min_opacity must not exceed max_opacity"));
            if (json.texture.empty())
                return std::unexpected(fail(skinJson, field + ".texture", "must not be empty"));

            AccentEffectSpec spec{};
            spec.regionId   = region->id;
            spec.period     = json.period;
            spec.phase      = json.phase;
            spec.minOpacity = json.min_opacity;
            spec.maxOpacity = json.max_opacity;

            if (const auto error = resolveAsset(json.texture, field + ".texture", spec.texture))
                return std::unexpected(*error);

            // The accent texture is a localized highlight, never a replacement of
            // the base: it must be an RGBA raster confined to the referenced
            // region's exact pixel dimensions.
            const auto checkAccentImage = [&](const std::filesystem::path& asset) -> std::optional<ValidationError> {
                std::error_code fileEc;
                if (!std::filesystem::is_regular_file(asset, fileEc))
                    return fail(asset, field + ".texture", "PNG image does not exist");

                bool rgba      = false;
                int  imageWide = 0;
                int  imageTall = 0;
                switch (probePngRgba(asset, rgba, imageWide, imageTall)) {
                case PngProbe::ok:
                    break;
                case PngProbe::notPng:
                    return fail(asset, field + ".texture", "image is not a PNG");
                case PngProbe::readError:
                    return fail(asset, field + ".texture", "cannot decode PNG header");
                }
                if (!rgba)
                    return fail(asset, field + ".texture", "PNG color type must be RGBA");
                if (imageWide != region->rect.width || imageTall != region->rect.height)
                    return fail(asset, field + ".texture",
                                "image is " + std::to_string(imageWide) + "x" + std::to_string(imageTall) +
                                    ", expected the region's " + std::to_string(region->rect.width) + "x" +
                                    std::to_string(region->rect.height));
                return std::nullopt;
            };
            if (const auto error = checkAccentImage(spec.texture))
                return std::unexpected(*error);

            pack.accentEffects.push_back(std::move(spec));
        }
    }

    // Optional layered flower motions. Declaring any requires the opt-in layered
    // frame with physical insets: the runtime composites each full region itself,
    // so the ordinary ornament draw must be suppressed for that region.
    if (manifest.flower_effects.size() > 4)
        return std::unexpected(fail(skinJson, "flower_effects", "must declare at most 4 flower effects"));

    if (!manifest.flower_effects.empty()) {
        if (!manifest.layered_ornaments)
            return std::unexpected(fail(skinJson, "flower_effects", "requires layered_ornaments"));
        if (!pack.frameInsets)
            return std::unexpected(fail(skinJson, "flower_effects", "requires frame_insets"));

        pack.flowerEffects.reserve(manifest.flower_effects.size());
        std::unordered_set<std::string> flowerRegions;

        for (size_t index = 0; index < manifest.flower_effects.size(); ++index) {
            const JsonFlowerEffect& json  = manifest.flower_effects[index];
            const std::string       field = "flower_effects[" + std::to_string(index) + "]";

            if (json.region.empty())
                return std::unexpected(fail(skinJson, field + ".region", "must not be empty"));

            // The effect is authored against exactly one existing one-shot
            // ornament region; region ids are already unique, so a plain lookup
            // is exact.
            const RegionSpec* region = nullptr;
            for (const RegionSpec& candidate : pack.regions) {
                if (candidate.id == json.region) {
                    region = &candidate;
                    break;
                }
            }
            if (region == nullptr)
                return std::unexpected(fail(skinJson, field + ".region", "references unknown region " + json.region));
            if (region->role != RegionRole::ornament)
                return std::unexpected(fail(skinJson, field + ".region", "region role must be ornament"));
            if (region->repeat != RepeatAxis::none)
                return std::unexpected(fail(skinJson, field + ".region", "region must not repeat"));
            if (!flowerRegions.insert(region->id).second)
                return std::unexpected(fail(skinJson, field + ".region", "each flower effect needs a unique region"));

            // The compositor emits the whole region, so no other effect may share
            // it: a second owner would double-draw the stationary ornament.
            if (pack.candleEffect && pack.candleEffect->regionId == region->id)
                return std::unexpected(fail(skinJson, field + ".region", "region is already used by the candle effect"));
            for (const AccentEffectSpec& accent : pack.accentEffects)
                if (accent.regionId == region->id)
                    return std::unexpected(fail(skinJson, field + ".region", "region is already used by an accent effect"));

            if (!finite(json.period) || json.period < 2.0 || json.period > 60.0)
                return std::unexpected(fail(skinJson, field + ".period", "must be finite and within [2, 60] seconds"));
            if (!finite(json.phase) || json.phase < 0.0 || json.phase >= 1.0)
                return std::unexpected(fail(skinJson, field + ".phase", "must be finite and within [0, 1)"));

            // Petals and bubble are independent: a bubble may carry zero to six
            // petals, a petal-only effect needs 1..6, and the six-petal cap holds
            // whether or not a bubble is declared.
            if (json.petals.size() > 6)
                return std::unexpected(fail(skinJson, field + ".petals", "must declare at most 6 petals"));
            if (!json.bubble && json.petals.empty())
                return std::unexpected(fail(skinJson, field, "must declare a bubble or between 1 and 6 petals"));

            const int effectWidth  = region->rect.width;
            const int effectHeight = region->rect.height;
            if (effectWidth > 2048 || effectHeight > 2048)
                return std::unexpected(
                    fail(skinJson, field + ".region", "referenced region is larger than 2048 pixels on a side"));

            if (json.background.empty())
                return std::unexpected(fail(skinJson, field + ".background", "must not be empty"));
            if (json.foreground.empty())
                return std::unexpected(fail(skinJson, field + ".foreground", "must not be empty"));

            FlowerEffectSpec spec{};
            spec.regionId = region->id;
            spec.period   = json.period;
            spec.phase    = json.phase;
            spec.width    = effectWidth;
            spec.height   = effectHeight;

            if (const auto error = resolveAsset(json.background, field + ".background", spec.background))
                return std::unexpected(*error);
            if (const auto error = resolveAsset(json.foreground, field + ".foreground", spec.foreground))
                return std::unexpected(*error);

            // Every effect raster is a rasterisation of the referenced region:
            // RGBA and matching its exact pixel dimensions, like the candle and
            // accent rasters. Petal textures share that canvas so the declared
            // pivot is directly a region-pixel coordinate.
            const auto checkRegionImage = [&](const std::filesystem::path& asset, const std::string& name)
                -> std::optional<ValidationError> {
                std::error_code fileEc;
                if (!std::filesystem::is_regular_file(asset, fileEc))
                    return fail(asset, name, "PNG image does not exist");

                bool rgba      = false;
                int  imageWide = 0;
                int  imageTall = 0;
                switch (probePngRgba(asset, rgba, imageWide, imageTall)) {
                case PngProbe::ok:
                    break;
                case PngProbe::notPng:
                    return fail(asset, name, "image is not a PNG");
                case PngProbe::readError:
                    return fail(asset, name, "cannot decode PNG header");
                }
                if (!rgba)
                    return fail(asset, name, "PNG color type must be RGBA");
                if (imageWide != effectWidth || imageTall != effectHeight)
                    return fail(asset, name, "image is " + std::to_string(imageWide) + "x" + std::to_string(imageTall) +
                                                  ", expected " + std::to_string(effectWidth) + "x" +
                                                  std::to_string(effectHeight));
                return std::nullopt;
            };

            if (const auto error = checkRegionImage(spec.background, field + ".background"))
                return std::unexpected(*error);
            if (const auto error = checkRegionImage(spec.foreground, field + ".foreground"))
                return std::unexpected(*error);

            if (json.bubble) {
                const JsonBubble& raw = *json.bubble;
                const std::string  bfield = field + ".bubble";

                if (raw.texture.empty())
                    return std::unexpected(fail(skinJson, bfield + ".texture", "must not be empty"));
                if (!finite(raw.center[0]) || !finite(raw.center[1]))
                    return std::unexpected(fail(skinJson, bfield + ".center", "coordinates must be finite"));
                if (!finite(raw.radii[0]) || !finite(raw.radii[1]))
                    return std::unexpected(fail(skinJson, bfield + ".radii", "radii must be finite"));
                if (raw.radii[0] <= 0.0 || raw.radii[1] <= 0.0)
                    return std::unexpected(fail(skinJson, bfield + ".radii", "radii must be positive"));
                if (!finite(raw.outward[0]) || !finite(raw.outward[1]))
                    return std::unexpected(fail(skinJson, bfield + ".outward", "direction must be finite"));
                const double outwardNorm = std::hypot(raw.outward[0], raw.outward[1]);
                if (!finite(outwardNorm) || outwardNorm <= 0.0)
                    return std::unexpected(fail(skinJson, bfield + ".outward", "direction norm must be finite and non-zero"));
                if (!finite(raw.spread) || raw.spread <= 0.0 || raw.spread > 128.0)
                    return std::unexpected(fail(skinJson, bfield + ".spread", "must be finite and within (0, 128]"));

                // The runtime keeps every drop inside a conservative circular
                // bound; the whole excursion must fit the referenced region, so a
                // valid pack never needs clamping at the canvas edge.
                const double excursion = std::max(raw.radii[0], raw.radii[1]) + raw.spread + 6.0;
                if (raw.center[0] < excursion || raw.center[0] > static_cast<double>(effectWidth) - excursion ||
                    raw.center[1] < excursion || raw.center[1] > static_cast<double>(effectHeight) - excursion)
                    return std::unexpected(
                        fail(skinJson, bfield + ".center", "bubble excursion does not fit inside the region"));

                BubbleSpec bubble{};
                bubble.center  = {raw.center[0], raw.center[1]};
                bubble.radii   = {raw.radii[0], raw.radii[1]};
                bubble.outward = {raw.outward[0], raw.outward[1]};
                bubble.spread  = raw.spread;
                if (const auto error = resolveAsset(raw.texture, bfield + ".texture", bubble.texture))
                    return std::unexpected(*error);
                if (const auto error = checkRegionImage(bubble.texture, bfield + ".texture"))
                    return std::unexpected(*error);

                spec.bubble = std::move(bubble);
            }

            spec.petals.reserve(json.petals.size());
            for (size_t petalIndex = 0; petalIndex < json.petals.size(); ++petalIndex) {
                const JsonFlowerPetal& raw    = json.petals[petalIndex];
                const std::string      pfield = field + ".petals[" + std::to_string(petalIndex) + "]";

                if (!finite(raw.pivot[0]) || !finite(raw.pivot[1]))
                    return std::unexpected(fail(skinJson, pfield + ".pivot", "coordinates must be finite"));
                if (raw.pivot[0] < 0.0 || raw.pivot[0] >= static_cast<double>(effectWidth) || raw.pivot[1] < 0.0 ||
                    raw.pivot[1] >= static_cast<double>(effectHeight))
                    return std::unexpected(fail(skinJson, pfield + ".pivot", "pivot must lie inside the effect region"));
                if (!finite(raw.angle) || raw.angle == 0.0 || std::abs(raw.angle) > 12.0)
                    return std::unexpected(
                        fail(skinJson, pfield + ".angle", "must be finite, non-zero and at most 12 degrees"));
                if (!finite(raw.phase) || raw.phase < 0.0 || raw.phase >= 1.0)
                    return std::unexpected(fail(skinJson, pfield + ".phase", "must be finite and within [0, 1)"));
                if (raw.texture.empty())
                    return std::unexpected(fail(skinJson, pfield + ".texture", "must not be empty"));

                FlowerPetalSpec petal{};
                petal.pivot = {raw.pivot[0], raw.pivot[1]};
                petal.angle = raw.angle;
                petal.phase = raw.phase;
                if (const auto error = resolveAsset(raw.texture, pfield + ".texture", petal.texture))
                    return std::unexpected(*error);
                if (const auto error = checkRegionImage(petal.texture, pfield + ".texture"))
                    return std::unexpected(*error);

                spec.petals.push_back(std::move(petal));
            }

            pack.flowerEffects.push_back(std::move(spec));
        }
    }

    // Optional silver material motion. Absence is the normal static case: a pack
    // without one pays neither validation nor runtime resources.
    if (manifest.silver_effect) {
        const JsonSilverEffect& json = *manifest.silver_effect;

        if (json.mask.empty())
            return std::unexpected(fail(skinJson, "silver_effect.mask", "must not be empty"));
        if (!finite(json.period) || json.period <= 0.0)
            return std::unexpected(fail(skinJson, "silver_effect.period", "must be finite and greater than zero"));
        if (!finite(json.strength) || json.strength < 0.0 || json.strength > 1.0)
            return std::unexpected(fail(skinJson, "silver_effect.strength", "must be finite and within [0, 1]"));

        SilverEffectSpec spec{};
        spec.period   = json.period;
        spec.strength = json.strength;

        if (const auto error = resolveAsset(json.mask, "silver_effect.mask", spec.mask))
            return std::unexpected(*error);

        // The mask is a full-atlas raster, exactly like the atlases themselves:
        // RGBA and matching the declared source pixel dimensions, so the shader
        // can sample both with the same UVs without any coordinate mapping.
        std::error_code fileEc;
        if (!std::filesystem::is_regular_file(spec.mask, fileEc))
            return std::unexpected(fail(spec.mask, "silver_effect.mask", "PNG mask does not exist"));
        bool rgba      = false;
        int  maskWide  = 0;
        int  maskTall  = 0;
        switch (probePngRgba(spec.mask, rgba, maskWide, maskTall)) {
        case PngProbe::ok:
            break;
        case PngProbe::notPng:
            return std::unexpected(fail(spec.mask, "silver_effect.mask", "mask is not a PNG image"));
        case PngProbe::readError:
            return std::unexpected(fail(spec.mask, "silver_effect.mask", "cannot decode PNG header"));
        }
        if (!rgba)
            return std::unexpected(fail(spec.mask, "silver_effect.mask", "PNG color type must be RGBA"));
        if (maskWide != manifest.source.width || maskTall != manifest.source.height)
            return std::unexpected(fail(spec.mask, "silver_effect.mask",
                                        "mask is " + std::to_string(maskWide) + "x" + std::to_string(maskTall) +
                                            ", expected " + std::to_string(manifest.source.width) + "x" +
                                            std::to_string(manifest.source.height)));

        pack.silverEffect = std::move(spec);
    }

    const std::filesystem::path kittyConf = canonicalRoot / "kitty.conf";
    std::error_code kittyEc;
    const std::filesystem::path resolvedKittyConf = std::filesystem::weakly_canonical(kittyConf, kittyEc);
    if (kittyEc)
        return std::unexpected(fail(kittyConf, "kitty.conf", "cannot resolve kitty.conf: " + kittyEc.message()));
    if (!isWithin(canonicalRoot, resolvedKittyConf))
        return std::unexpected(fail(kittyConf, "kitty.conf", "kitty.conf escapes the pack root"));
    if (!std::filesystem::is_regular_file(resolvedKittyConf, kittyEc))
        return std::unexpected(fail(resolvedKittyConf, "kitty.conf", "kitty.conf does not exist"));

    pack.root        = canonicalRoot;
    pack.kittyConfig = resolvedKittyConf;
    return pack;
}

}
