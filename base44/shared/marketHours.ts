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

// NY session date string (YYYY-MM-DD) in America/New_York.
// The US equity trading day runs 9:30 AM – 4:00 PM ET with extended hours
// until 8:00 PM ET. A trade at 7 PM ET on Monday belongs to Monday's session,
// not Tuesday (which UTC would imply). (Fixes Rev.13 #20, #26.)
export function nySessionDateStr(date: Date = new Date()): string {
  const nyStr = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(date);
  const match = nyStr.match(/(\d{2})\/(\d{2})\/(\d{4})/);
  if (!match) return date.toISOString().slice(0, 10);
  const [, month, day, year] = match;
  return `${year}-${month}-${day}`;
}

// UTC timestamp for midnight ET on the current NY session date.
// Used for daily trade/P&L boundaries — a trade at 8 PM ET on Monday counts
// as Monday's trade, not Tuesday's (which server-local midnight would imply).
// Robust against DST: constructs noon UTC on the NY date, then backs up to
// midnight NY by subtracting the NY wall-clock offset. (Fixes Rev.13 #20.)
export function nyDayStart(date: Date = new Date()): Date {
  const dateStr = nySessionDateStr(date);
  const [year, month, day] = dateStr.split('-').map(Number);
  // Noon UTC on the NY date — safe from DST edge cases at midnight
  const noonUtc = new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
  // Get the NY wall-clock time at that instant
  const nyTime = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(noonUtc);
  const [hourStr, minuteStr] = nyTime.split(':');
  let nyHour = parseInt(hourStr, 10) % 24;
  const nyMinute = parseInt(minuteStr, 10);
  // Midnight NY = noon UTC - (nyHour hours + nyMinute minutes)
  return new Date(noonUtc.getTime() - (nyHour * 60 + nyMinute) * 60000);
}