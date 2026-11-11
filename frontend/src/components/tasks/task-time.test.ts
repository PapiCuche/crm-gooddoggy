import { describe, expect, it } from "vitest";

import type { Task } from "@/lib/api/model";

import { dayRange, instantAt, isOverdue } from "./task-time";

const task = (changes: Partial<Task>) =>
  ({ status: "PENDING", due_at: null, is_all_day: false, ...changes }) as Task;

describe("instantes de las tareas", () => {
  it("las 00:00 de un día en Lima son las 05:00 UTC", () => {
    expect(new Date(instantAt("America/Lima", 2026, 11, 11)).toISOString()).toBe(
      "2026-11-11T05:00:00.000Z",
    );
    expect(new Date(instantAt("America/Lima", 2026, 11, 11, 14, 30)).toISOString()).toBe(
      "2026-11-11T19:30:00.000Z",
    );
    expect(instantAt("America/Lima", 2026, 11, 31)).toBe(instantAt("America/Lima", 2026, 12, 1)); // día de más
  });

  it("no depende de la zona del navegador y sigue el cambio de hora de otras zonas", () => {
    // Nueva York cambió de hora el 8 de marzo de 2026: ese día dura 23 horas.
    const { start, next } = dayRange(Date.parse("2026-03-08T18:00:00Z"), "America/New_York");
    expect(new Date(start).toISOString()).toBe("2026-03-08T05:00:00.000Z");
    expect((next - start) / 3_600_000).toBe(23);
    // hoy acaba a medianoche de la zona: a las 22:00 del 10 en Lima, hoy es el 10
    const lima = dayRange(Date.parse("2026-11-11T03:00:00Z"), "America/Lima");
    expect([lima.start, lima.next].map((at) => new Date(at).toISOString())).toEqual([
      "2026-11-10T05:00:00.000Z",
      "2026-11-11T05:00:00.000Z",
    ]);
  });

  it("la medianoche de un día de cambio de hora al este de UTC también es la de la zona", () => {
    // Sídney adelantó el reloj el 4 de octubre de 2026 a las 02:00: sus 00:00 son las 14:00Z del 3.
    expect(new Date(instantAt("Australia/Sydney", 2026, 10, 4)).toISOString()).toBe(
      "2026-10-03T14:00:00.000Z",
    );
    const { start, next } = dayRange(Date.parse("2026-10-04T05:00:00Z"), "Australia/Sydney");
    expect((next - start) / 3_600_000).toBe(23);
  });

  it("una de todo el día vence al acabar su día; una con hora, a su hora; lo cerrado no", () => {
    const now = Date.parse("2026-11-11T15:00:00Z");
    const start = Date.parse("2026-11-11T05:00:00Z");
    const open = (due_at: string, is_all_day: boolean) => task({ due_at, is_all_day });
    expect(isOverdue(open("2026-11-11T05:00:00Z", true), start, now)).toBe(false); // hoy
    expect(isOverdue(open("2026-11-10T05:00:00Z", true), start, now)).toBe(true); // ayer
    expect(isOverdue(open("2026-11-11T14:00:00Z", false), start, now)).toBe(true);
    expect(isOverdue(open("2026-11-11T16:00:00Z", false), start, now)).toBe(false);
    expect(isOverdue(task({ due_at: "2026-11-01T05:00:00Z", status: "DONE" }), start, now)).toBe(
      false,
    );
    expect(isOverdue(task({}), start, now)).toBe(false);
  });
});
