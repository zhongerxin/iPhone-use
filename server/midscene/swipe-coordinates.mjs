// Compatibility with the pinned @midscene/core prepatch: locator centers are
// converted to device points before action.call, but relative Swipe.distance
// (documented to the planner in screenshot pixels) is left unchanged.
export function swipeInDevicePoints(param, context) {
  if (param.end !== undefined || param.distance === undefined) return param;
  const ratio = context?.uiContext?.shrunkShotToLogicalRatio;
  if (!Number.isFinite(ratio) || ratio <= 0)
    throw new Error('Missing screenshot-to-device ratio for relative swipe');
  // Keep the original parameters intact for the screenshot-based report.
  return { ...param, distance: param.distance / ratio };
}
