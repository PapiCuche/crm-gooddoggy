import { describe, expect, it } from "vitest";

import { zonedInstant } from "./zoned";

describe("zonedInstant", () => {
  it.each(["0001-01-01T00:00", "0050-06-15T10:00", "0999-12-31T23:59", "9999-12-31T23:59"])(
    "rechaza el año de %s: no lo convierte mal en silencio",
    (local) => {
      expect(Number.isNaN(zonedInstant(local, "America/Lima").getTime())).toBe(true);
    },
  );

  it("acepta los bordes 1000 y 9998", () => {
    for (const local of ["1000-01-01T00:00", "9998-12-31T23:59"])
      expect(Number.isNaN(zonedInstant(local, "America/Lima").getTime())).toBe(false);
  });

  it.each(["", "12345-01-01T00:00", "nada", "2026-13-40T00:00"])("rechaza %j", (local) => {
    expect(Number.isNaN(zonedInstant(local, "America/Lima").getTime())).toBe(true);
  });
});
