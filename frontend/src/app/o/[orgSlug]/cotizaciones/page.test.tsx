import { describe, expect, it } from "vitest";

import QuotesPage from "./page";

const UUID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const contactOf = async (cliente?: string | string[]) =>
  (await QuotesPage({ searchParams: Promise.resolve({ cliente }) })).props.contactId;

describe("ruta de la lista de cotizaciones", () => {
  it("un UUID en `?cliente=` filtra; cualquier otra cosa se ignora", async () => {
    expect(await contactOf(UUID)).toBe(UUID);
    for (const bad of ["no-es-uuid", "../x", UUID.toUpperCase(), [UUID, UUID], undefined]) {
      expect(await contactOf(bad)).toBeUndefined();
    }
  });
});
