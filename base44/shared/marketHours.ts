// US equity market session helpers.
// Regular trading hours: 9:30 AM - 4:00 PM Eastern, Monday-Friday.
//
// NOTE: This does NOT account for US market holidays (NYSE/NASDAQ closures).
// For production-grade correctness, pair this with a trading-calendar API.
// The cron schedules and backend both gate on this; the backend is the
// authoritative gate (cron timing alone is insufficient per the audit).

function easternDate(date: Date = new Date()): Date {
  // Build a Date whose wall-clock fields represent America/New_York.
  const s = date.toLocaleString('en-US', { timeZone: 'America/New_York' });
  return new Date(s);
}

export function isUsMarketOpen(date: Date = new Date()): boolean {
  const et = easternDate(date);
  const day = et.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) return false;
  const minutes = et.getHours() * 60 + et.getMinutes();
  return minutes >= 570 && minutes < 960; // 9:30 = 570, 16:00 = 960
}

export function usMarketSession(date: Date = new Date()): string {
  const et = easternDate(date);
  const day = et.getDay();
  if (day === 0 || day === 6) return 'weekend';
  const minutes = et.getHours() * 60 + et.getMinutes();
  if (minutes >= 570 && minutes < 960) return 'regular';
  if (minutes < 570) return 'premarket';
  if (minutes >= 960 && minutes < 1200) return 'after_hours';
  return 'closed';
}