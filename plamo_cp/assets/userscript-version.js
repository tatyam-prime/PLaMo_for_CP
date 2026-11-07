// Shared by the management page and the Tampermonkey bundle.
// null means that a missing or unfamiliar version cannot be compared safely.
function compareUserscriptVersions(installed, available) {
  const parts = value => typeof value === 'string' && value.length <= 32 && value.trim() === value && /^\d+(?:\.\d+){0,3}$/.test(value)
    ? value.split('.').map(Number) : null;
  const left = parts(installed), right = parts(available);
  if (!left || !right || [...left, ...right].some(value => !Number.isSafeInteger(value))) return null;
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    const difference = (left[index] || 0) - (right[index] || 0);
    if (difference) return difference < 0 ? -1 : 1;
  }
  return 0;
}
