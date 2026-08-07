// Shared lot-accounting utilities — used by the settlement processor,
// execution gateway, and broker position sync. Extracted to prevent
// logic duplication across backend functions.

export function nowIso() { return new Date().toISOString(); }

export function genId(prefix) { return `${prefix}-${crypto.randomUUID()}`; }

// Parse closure_fill_ids — handles both legacy string array and new object array.
// Old format: ["fill-1", "fill-2"] — qty unknown
// New format: [{ fill_id: "fill-1", qty: 4 }, { fill_id: "fill-2", qty: 6 }]
export function parseClosureFills(closure_fill_ids) {
  const parsed = JSON.parse(closure_fill_ids || '[]');
  if (parsed.length === 0) return [];
  if (typeof parsed[0] === 'string') {
    return parsed.map((fid) => ({ fill_id: fid, qty: null }));
  }
  return parsed;
}