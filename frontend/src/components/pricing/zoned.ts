// La hora de pared de un instante en una zona IANA, y al revés. El proveedor de `next-intl`
// trae la zona (America/Lima, la predeterminada de `organizations.timezone`).
export const ZONE = "America/Lima";
type Wall = Record<"year" | "month" | "day" | "hour" | "minute" | "second", string>;

export function wallClock(date: Date, zone: string): Wall {
  const parts = new Intl.DateTimeFormat("en-US", {
    ...{ timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit" },
    ...{ hour: "2-digit", minute: "2-digit", second: "2-digit" },
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value])) as Wall;
}

// «dd/mm/aaaa hh:mm», o «dd/mm hh:mm» sin el año.
export function stamp(iso: string, zone: string, year = true) {
  const at = wallClock(new Date(iso), zone);
  return `${at.day}/${at.month}${year ? `/${at.year}` : ""} ${at.hour}:${at.minute}`;
}

// «aaaa-mm-ddThh:mm» de un `datetime-local`: el instante en que esa hora de pared ocurre en
// `zone`, o `Invalid Date` si no se lee o su año no está entre 1000 y 9998. Dos pasadas: la segunda corrige el desfase si la zona cambia de hora entre medias.
export function zonedInstant(local: string, zone: string): Date {
  const wall = Date.parse(`${local.slice(0, 16)}:00Z`);
  // Ilegible, o un año que `Date.UTC` no convierte (0-99 pasan a 19xx; 9999 se desborda).
  const year = /^\d{4}-/.test(local) ? Number(local.slice(0, 4)) : NaN;
  if (Number.isNaN(wall) || !(year >= 1000 && year <= 9998)) return new Date(NaN);
  const offset = (at: number) => {
    const w = wallClock(new Date(at), zone);
    return Date.UTC(+w.year, +w.month - 1, +w.day, +w.hour, +w.minute, +w.second) - at;
  };
  return new Date(wall - offset(wall - offset(wall)));
}

// El valor de un `datetime-local` para un instante (su `min`).
export function localValue(date: Date, zone: string) {
  const at = wallClock(date, zone);
  return `${at.year}-${at.month}-${at.day}T${at.hour}:${at.minute}`;
}
