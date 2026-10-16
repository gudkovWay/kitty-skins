/** Scalar kinematics for the existing 120s scene. No clock, state or allocations.
 * Geometry is in the authored roll/slat's logical space (frame-02-art.ts):
 * common root pivot [123.25, 1.5], half-width 82.75, roll half-height 17,
 * extending slat height 26. Bindings remain on the beam, outside this motion.
 */
function smooth(value: number): number {
  const u = Math.max(0, Math.min(1, value));
  return u * u * (3 - 2 * u);
}

function creepDistance(distance: number, scale: number): number {
  return Math.min(24 * scale, distance * 0.08);
}

/** Height-aware fall, leaving room for settling before the existing walk. */
export function bambooDropEnd(distance: number, scale: number): number {
  const travel = Math.max(0, distance - creepDistance(distance, scale));
  return 105 + Math.max(0.4, Math.min(0.9, Math.sqrt(2 * travel / 3600)));
}

/** Slat release is delayed relative to creep, then retracts during the lift. */
export function bambooRevealAt(t: number, landing: number): number {
  if (t < 100 || t >= 114.2) return 0;
  if (t < 105) return 0.12 * smooth((t - 102) / 3);
  if (t < landing) return 0.12 + 0.88 * smooth((t - 105) / (landing - 105));
  if (t < 111.55) return 1;
  return 1 - smooth((t - 111.55) / 2.65);
}

/** A rigid bundle: slight imbalance at release, damped rocking after contact. */
export function bambooAngleAt(t: number, landing: number): number {
  if (t >= 105 && t < landing) {
    return 0.025 * Math.sin(Math.PI * (t - 105) / (landing - 105));
  }
  if (t >= landing && t < landing + 0.6) {
    const u = (t - landing) / 0.6;
    return 0.016 * Math.sin(2 * Math.PI * u) * (1 - u) * (1 - u);
  }
  if (t >= 111.2 && t < 114.5) {
    return -0.008 * Math.sin(Math.PI * (t - 111.2) / 3.3);
  }
  return 0;
}

/** Lowest point of the rotated exposed rectangle, relative to its home root. */
export function bambooBottomAt(angle: number, reveal: number, scale: number): number {
  return scale * (1.5 + 82.75 * Math.abs(Math.sin(angle)) +
    (17 + 26 * reveal) * Math.cos(angle));
}

/** One short rigid recoil; zero displacement at both ends, no rubber scaling. */
function recoil(t: number, start: number, duration: number): number {
  if (t < start || t >= start + duration) return 0;
  const u = (t - start) / duration;
  return Math.sin(Math.PI * u) * (1 - u) * (1 - u);
}

export function bambooOffsetAt(t: number, distance: number, scale: number, landing: number): number {
  if (distance <= 0 || t < 100 || t >= 115) return 0;
  let y: number;
  const creep = creepDistance(distance, scale);
  if (t < 105) {
    // Two short slips separated by a hold, followed by the final release.
    y = creep * (0.25 * smooth((t - 100) / 1.7) +
      0.30 * smooth((t - 102.0) / 1.0) + 0.45 * smooth((t - 103.6) / 1.4));
  } else if (t < landing) {
    const u = (t - 105) / (landing - 105);
    y = creep + (distance - creep) * u * u;
  } else if (t < 111.2) {
    // Two ballistic hops: 24px / 0.4s, then 6px / 0.2s at full scale.
    // Halving flight time and quartering height keeps the same acceleration;
    // both contacts still settle within the existing 0.6s story interval.
    const elapsed = t - landing;
    const secondHop = elapsed >= 0.4;
    const u = Math.min(1, (elapsed - (secondHop ? 0.4 : 0)) / (secondHop ? 0.2 : 0.4));
    const height = Math.min(24 * scale, distance * 0.12) * (secondHop ? 0.25 : 1);
    y = distance - 4 * height * u * (1 - u);
  } else if (t < 114.5) {
    const u = (t - 111.2) / 3.3;
    y = distance * (1 - u * u);
  } else {
    y = 4 * scale * recoil(t, 114.5, 0.5);
  }
  // Contact follows the actual tilted lower silhouette, not just its centre.
  const bottom = bambooBottomAt(bambooAngleAt(t, landing), bambooRevealAt(t, landing), scale);
  return Math.max(0, Math.min(y, distance + 44.5 * scale - bottom));
}

/** Rope enters the front bearing and disappears 2px inside the lower edge. */
export function bambooCordX(side: number, reveal: number, angle: number, scale: number): number {
  const dx = side === 0 ? -52.75 : 51.25;
  const dy = 15 + 26 * reveal;
  return scale * (123.25 + dx * Math.cos(angle) - dy * Math.sin(angle));
}

export function bambooCordY(side: number, offset: number, reveal: number, angle: number, scale: number): number {
  const dx = side === 0 ? -52.75 : 51.25;
  const dy = 15 + 26 * reveal;
  return offset + scale * (1.5 + dx * Math.sin(angle) + dy * Math.cos(angle));
}
