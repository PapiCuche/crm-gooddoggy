import { describe, expect, it } from "vitest";

import { dayRange, instantAt } from "./task-time";

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

  it("la medianoche de un día de cambio de hora al este de UTC también es la de la zona", () => {
    // Sídney adelantó el reloj el 4 de octubre de 2026 a las 02:00: sus 00:00 son las 14:00Z del 3.
    expect(new Date(instantAt("Australia/Sydney", 2026, 10, 4)).toISOString()).toBe(
      "2026-10-03T14:00:00.000Z",
    );
    const { start, next } = dayRange(Date.parse("2026-10-04T05:00:00Z"), "Australia/Sydney");
    expect((next - start) / 3_600_000).toBe(23);
  });
});
