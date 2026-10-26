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
