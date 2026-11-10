import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import OpportunityPage from "./page";

const open = (opportunityId: string) =>
  OpportunityPage({ params: Promise.resolve({ orgSlug: "acme", opportunityId }) });

describe("ruta del detalle de una oportunidad", () => {
  it.each(["no-es-uuid", "../x", "6F1C2A4E-0B7D-4C8E-9A51-3D2F7E8B9C10", "123"])(
    "%s no es un id: no encontrada, sin pedir nada",
    async (id) => {
      await expect(open(id)).rejects.toThrow("NEXT_NOT_FOUND");
    },
  );

  it("un UUID en minúsculas abre el detalle", async () => {
    await expect(open("6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10")).resolves.toBeTruthy();
  });
});
