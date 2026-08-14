export function newYorkDateString(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

export function formatDateOnly(dateString, options = {}) {
  const [year, month, day] = String(dateString || '').split('-').map(Number);
  if (!year || !month || !day) return 'Invalid date';
  return new Date(year, month - 1, day).toLocaleDateString('en-US', options);
}
