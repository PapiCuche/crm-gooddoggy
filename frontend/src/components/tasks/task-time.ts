import type { Task } from "@/lib/api/model";

// Los instantes de las tareas (F5-08c). «Hoy», «vencida» y «todo el día» se calculan en la zona
// con que se muestra todo lo demás (`useTimeZone`), no en la del navegador: el servidor no usa
// zonas para eso (D-F5-08b). `due_at` con `is_all_day` es el instante de las 00:00 de ese día.
type Wall = { year: number; month: number; day: number; hour: number; minute: number };

const wall = (at: number, zone: string): Wall =>
  Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      ...{ timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric" },
      ...{ hour: "numeric", minute: "numeric" },
    })
      .formatToParts(at)
      .map(({ type, value }) => [type, Number(value)]),
  ) as Wall;

// El instante en que el reloj de `zone` marca ese día y esa hora (el día puede pasarse de rango:
// el 32 de un mes es el 1 del siguiente).
export function instantAt(zone: string, y: number, m: number, d: number, h = 0, min = 0): number {
  const target = Date.UTC(y, m - 1, d, h, min);
  let at = target;
  for (let pass = 0; pass < 2; pass++) {
    const shown = wall(at, zone);
    at += target - Date.UTC(shown.year, shown.month - 1, shown.day, shown.hour, shown.minute);
  }
  return at;
}

// Desde las 00:00 de hoy hasta las 00:00 de mañana.
export function dayRange(now: number, zone: string) {
  const { year, month, day } = wall(now, zone);
  return { start: instantAt(zone, year, month, day), next: instantAt(zone, year, month, day + 1) };
}

export const isOpen = (task: Task) => task.status === "PENDING" || task.status === "IN_PROGRESS";

// Vencida: abierta y con la fecha ya pasada. Una de todo el día vence cuando acaba su día, no a
// las 00:00 (el servidor, que no sabe de días, la cuenta vencida desde las 00:00).
export function isOverdue(task: Task, startOfToday: number, now: number): boolean {
  if (!isOpen(task) || !task.due_at) return false;
  const due = Date.parse(task.due_at);
  return task.is_all_day ? due < startOfToday : due < now;
}
