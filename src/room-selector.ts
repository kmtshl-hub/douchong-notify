type Room = { room_id: number; anchor_name: string };
const normalized = (s: string) => s.normalize('NFKC').replace(/\s+/gu, '').toLocaleLowerCase();

/** Match literal text, never execute caller-supplied regular expressions. */
export function selectRooms<T extends Room>(rooms: T[], query: string): T[] {
  const value = normalized(query.trim());
  if (!value) return [];
  const unique = [...new Map(rooms.map(r => [Number(r.room_id), r])).values()];
  if (/^\d+$/.test(value)) return unique.filter(r => String(r.room_id) === value);
  const exact = unique.filter(r => normalized(r.anchor_name || '') === value);
  return exact.length ? exact : unique.filter(r => normalized(r.anchor_name || '').includes(value));
}
