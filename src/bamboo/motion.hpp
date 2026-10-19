#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <numbers>

namespace bamboo02 {

inline constexpr double kInset = 52.0;
inline constexpr double kStoryPeriod = 120.0;
inline constexpr double kAmbientPeriod = 96.0;
inline constexpr double kTau = 2.0 * std::numbers::pi;

constexpr double smooth(double x) {
    x = std::clamp(x, 0.0, 1.0);
    return x * x * (3.0 - 2.0 * x);
}
constexpr double creepDistance(double distance, double scale) {
    return std::min(24.0 * scale, distance * 0.08);
}
inline double landingAt(double distance, double scale) {
    return 105.0 + std::clamp(std::sqrt(2.0 * std::max(0.0, distance - creepDistance(distance, scale)) / 3600.0), 0.4, 0.9);
}
constexpr double revealAt(double t, double landing) {
    if (t < 100.0 || t >= 114.2) return 0.0;
    if (t < 105.0) return 0.12 * smooth((t - 102.0) / 3.0);
    if (t < landing) return 0.12 + 0.88 * smooth((t - 105.0) / (landing - 105.0));
    if (t < 111.55) return 1.0;
    return 1.0 - smooth((t - 111.55) / 2.65);
}
inline double bambooAngleAt(double t, double landing) {
    if (t >= 105.0 && t < landing)
        return 0.025 * std::sin(std::numbers::pi * (t - 105.0) / (landing - 105.0));
    if (t >= landing && t < landing + 0.6) {
        const double u = (t - landing) / 0.6;
        return 0.016 * std::sin(kTau * u) * (1.0 - u) * (1.0 - u);
    }
    if (t >= 111.2 && t < 114.5)
        return -0.008 * std::sin(std::numbers::pi * (t - 111.2) / 3.3);
    return 0.0;
}
inline double bottomAt(double angle, double reveal, double scale) {
    return scale * (1.5 + 82.75 * std::abs(std::sin(angle)) + (17.0 + 26.0 * reveal) * std::cos(angle));
}
inline double recoilAt(double t, double start, double duration) {
    if (t < start || t >= start + duration) return 0.0;
    const double u = (t - start) / duration;
    return std::sin(std::numbers::pi * u) * (1.0 - u) * (1.0 - u);
}
inline double offsetAt(double t, double distance, double scale, double landing) {
    if (distance <= 0.0 || t < 100.0 || t >= 115.0) return 0.0;
    double y;
    const double creep = creepDistance(distance, scale);
    if (t < 105.0)
        y = creep * (0.25 * smooth((t - 100.0) / 1.7) + 0.30 * smooth((t - 102.0) / 1.0) + 0.45 * smooth((t - 103.6) / 1.4));
    else if (t < landing) {
        const double u = (t - 105.0) / (landing - 105.0);
        y = creep + (distance - creep) * u * u;
    } else if (t < 111.2) {
        const double elapsed = t - landing;
        const bool second = elapsed >= 0.4;
        const double u = std::min(1.0, (elapsed - (second ? 0.4 : 0.0)) / (second ? 0.2 : 0.4));
        const double height = std::min(24.0 * scale, distance * 0.12) * (second ? 0.25 : 1.0);
        y = distance - 4.0 * height * u * (1.0 - u);
    } else if (t < 114.5) {
        const double u = (t - 111.2) / 3.3;
        y = distance * (1.0 - u * u);
    } else
        y = 4.0 * scale * recoilAt(t, 114.5, 0.5);
    return std::max(0.0, std::min(y, distance + 44.5 * scale - bottomAt(bambooAngleAt(t, landing), revealAt(t, landing), scale)));
}
constexpr double buttonAt(double t) {
    if (t < 110.6 || t >= 111.2) return 0.0;
    if (t < 110.72) return smooth((t - 110.6) / 0.12);
    if (t < 111.0) return 1.0;
    return 1.0 - smooth((t - 111.0) / 0.2);
}
constexpr double impactAt(double t, double start, double end) {
    if (t < start || t >= end) return 0.0;
    const double u = (t - start) / (end - start);
    return (1.0 - u) * (1.0 - u);
}
inline double gustAt(double t, double phase) {
    return 0.58 * std::sin(t * kTau * 2.0 / 20.0 + phase) +
        0.27 * std::sin(t * kTau * 5.0 / 20.0 + 1.7 + phase * 0.6) +
        0.15 * std::sin(t * kTau * 9.0 / 20.0 + 0.6 + phase * 1.3);
}
inline double slowGustAt(double t, double phase) {
    return 0.62 * std::sin(t * kTau / 20.0 + phase) +
        0.26 * std::sin(t * kTau * 3.0 / 20.0 + 2.2 + phase) +
        0.12 * std::sin(t * kTau * 7.0 / 20.0 + 0.4 + phase);
}
inline double noiseAt(double t) {
    return 0.62 * std::sin(t * kTau * 3.0 / 20.0 + 0.3) + 0.38 * std::sin(t * kTau * 8.0 / 20.0 + 2.1);
}

struct Layer {
    std::array<float, 4> cell;
    std::array<float, 2> size, pivot, offset;
    bool right = false, bottom = false;
};
// Exact measured metadata from frame-02-art.ts; order is source-over order.
inline constexpr std::array<Layer, 10> kLayers = {{
    {{339,1612,331,52}, {165.5F,26}, {82.75F,-17}, {40.5F,18.5F}},
    {{4,1612,331,68}, {165.5F,34}, {82.75F,17}, {40.5F,-15.5F}},
    {{674,1612,38,114}, {19,57}, {9.5F,2}, {61,-17}},
    {{716,1612,38,114}, {19,57}, {9.5F,2}, {165,-17}},
    {{1243,1032,264,189}, {66,47.306F}, {32.918F,0}, {0.582F,48}, true},
    {{428,424,176,49}, {44,12.146F}, {41.762F,10.372F}, {-8.262F,37.628F}, true},
    {{424,1032,576,576}, {144,144}, {70.9F,9.2F}, {-40.9F,-159.2F}, true,true},
    {{1567,1032,222,146}, {55.44F,36.435F}, {40.635F,23.415F}, {-10.635F,-173.415F}, true,true},
    {{1688,4,235,509}, {58.695F,127.365F}, {28.14F,2.1F}, {1.86F,-152.1F}, true,true},
    {{1004,1032,235,509}, {58.695F,127.365F}, {28.14F,2.1F}, {1.86F,-152.1F}, true,true},
}};

struct Motion {
    std::array<float, 32> positions{}, sizes{}, pivots{};
    std::array<float, 64> cells{};
    std::array<float, 16> angles{}, reveals{}, modes{}, emissions{}, deforms{};
    std::array<float, 8> scenePositions{};
    std::array<float, 4> girlCell{};
    std::array<float, 2> girlPos{}, girlSize{}, buttonCenter{}, buttonSize{}, anchor0{}, anchor1{}, end0{}, end1{};
    float wind = 0, girlOpacity = 0, girlFacing = 1, buttonPress = 0, floorImpact = 0, ceilingImpact = 0;
    float sceneScale = 0, cordDiameter = 0, cordPayout = 0, cordSlack = 0;
};

inline Motion evaluate(double elapsed, double rootW, double rootH, bool reduced) {
    Motion m;
    const double t = std::fmod(std::max(0.0, elapsed), kStoryPeriod);
    const double s = std::min({1.0, rootW / 480.0, rootH / 320.0});
    const double cw = std::max(0.0, rootW - 104.0), ch = std::max(0.0, rootH - 104.0);
    const double floor = rootH - 53.0;
    const double fall = std::max(0.0, floor - 44.5 * s);
    const double landing = landingAt(fall, s), settled = landing + 0.6;
    const double gs = std::min({0.2, std::max(0.0, ch - 2.0) / 512.0, std::max(0.0, cw - 2.0) / 384.0});
    const double ss = gs / 0.2;
    const double bx = 52.0 + 92.0 * ss, pressX = bx - 11.0 * gs, parkX = 52.0 + cw + 384.0 * gs;
    const double rev = reduced ? 0.0 : revealAt(t, landing), angle = reduced ? 0.0 : bambooAngleAt(t, landing);
    const double y = reduced ? 0.0 : offsetAt(t, fall, s, landing);
    const double lampAngle = reduced ? 0.0 : 0.017 * slowGustAt(t - 2.2, 0.8);
    const double craneAngle = reduced ? 0.0 : 0.024 * slowGustAt(t - 0.9, 0.35);
    const double energy = reduced ? 1.0 : 1.0 + 0.20 * noiseAt(t);
    for (size_t i = 0; i < kLayers.size(); ++i) {
        const auto& l = kLayers[i];
        std::copy(l.cell.begin(), l.cell.end(), m.cells.begin() + i * 4);
        for (size_t axis = 0; axis < 2; ++axis) {
            m.sizes[i * 2 + axis] = l.size[axis] * s;
            m.pivots[i * 2 + axis] = l.pivot[axis] * s;
        }
        m.positions[i * 2] = (l.right ? rootW : 0.0) + l.offset[0] * s;
        m.positions[i * 2 + 1] = (l.bottom ? rootH : 0.0) + l.offset[1] * s + (i < 2 ? y : 0.0);
        m.angles[i] = i < 2 ? angle : i == 4 ? craneAngle : (i == 6 || i >= 8) ? lampAngle : 0.0;
        m.reveals[i] = i == 0 ? rev : 1.0;
        m.modes[i] = i == 0 ? 1.0 : 0.0;
        m.emissions[i] = (i == 6 || i == 9) ? energy : 1.0;
        if (i < 4) {
            m.scenePositions[i * 2] = m.positions[i * 2] - 52.0;
            m.scenePositions[i * 2 + 1] = m.positions[i * 2 + 1] - 52.0;
        }
    }
    double gx = parkX;
    if (!reduced && t > settled) {
        if (t < 110.0) gx = parkX + (pressX - parkX) * (t - settled) / (110.0 - settled);
        else if (t < 116.9) gx = pressX;
        else if (t < 119.4) gx = pressX + (parkX - pressX) * (t - 116.9) / 2.5;
    }
    int frame = 0;
    const double stride = std::max(1.0, 8.0 * ss);
    if (!reduced && t > settled) {
        if (t < 110.0) frame = static_cast<int>(std::floor((parkX - gx) / stride)) % 8;
        else if (t < 110.35) frame = 9;
        else if (t < 110.6) frame = 10;
        else if (t < 111.0) frame = 11;
        else if (t < 111.9) frame = 12;
        else if (t < 115.0) frame = 8;
        else if (t < 115.7) frame = 13;
        else if (t < 116.2) frame = 14;
        else if (t < 116.9) frame = 15;
        else if (t < 119.4) frame = static_cast<int>(std::floor((gx - pressX) / stride)) % 8;
        if (t < settled + 0.5) m.girlOpacity = smooth((t - settled) / 0.5);
        else if (t < 118.9) m.girlOpacity = 1.0;
        else if (t < 119.4) m.girlOpacity = 1.0 - smooth((t - 118.9) / 0.5);
    }
    m.girlCell = {float((frame % 4) * 384), float((frame / 4) * 512), 384, 512};
    m.girlSize = {float(384.0 * gs), float(512.0 * gs)};
    m.girlPos = {float(gx - 52.0), float(floor - 505.0 * gs - 52.0)};
    m.girlFacing = reduced || t < 116.9 ? 1.0 : -1.0;
    m.buttonCenter = {float(bx - 52.0), float(floor - (505.0 - 180.0) * gs - 52.0)};
    m.buttonSize = {float(18.0 * ss), float(10.0 * ss)};
    m.buttonPress = reduced ? 0.0 : buttonAt(t);
    m.floorImpact = reduced ? 0.0 : impactAt(t, landing, settled);
    m.ceilingImpact = reduced ? 0.0 : impactAt(t, 114.5, 115.0);
    m.sceneScale = ss;
    m.wind = reduced ? 0.0 : gustAt(t, 0.0);
    m.anchor0 = {float(70.5 * s - 52.0), float(22.0 * s - 52.0)};
    m.anchor1 = {float(174.5 * s - 52.0), float(22.0 * s - 52.0)};
    const auto end = [&](double dx) -> std::array<float, 2> {
        const double dy = 15.0 + 26.0 * rev;
        return {float(s * (123.25 + dx * std::cos(angle) - dy * std::sin(angle)) - 52.0),
                float(y + s * (1.5 + dx * std::sin(angle) + dy * std::cos(angle)) - 52.0)};
    };
    m.end0 = end(-52.75); m.end1 = end(51.25);
    m.cordDiameter = 4.5 * s;
    m.cordPayout = std::clamp(y / std::max(0.001, 12.0 * s), 0.0, 1.0);
    m.cordSlack = m.floorImpact;
    return m;
}

// Pure boundary contracts; compilation is owned by the integrating agent.
static_assert(revealAt(0.0, 105.6) == 0.0 && revealAt(120.0, 105.6) == 0.0);
static_assert(revealAt(111.2, 105.6) == 1.0 && revealAt(114.2, 105.6) == 0.0);
static_assert(buttonAt(110.5) == 0.0 && buttonAt(110.8) == 1.0 && buttonAt(111.2) == 0.0);
static_assert(impactAt(105.6, 105.6, 106.2) == 1.0 && impactAt(106.2, 105.6, 106.2) == 0.0);
static_assert(kStoryPeriod == 6.0 * 20.0 && kAmbientPeriod != kStoryPeriod);

} // namespace bamboo02
