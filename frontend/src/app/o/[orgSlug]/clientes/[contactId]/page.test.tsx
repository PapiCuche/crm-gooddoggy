import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { ContactCard } from "@/components/contacts/contact-card";

import ContactPage from "./page";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const open = (contactId: string) =>
  ContactPage({ params: Promise.resolve({ orgSlug: "acme", contactId }) });

describe("ContactPage", () => {
  // Lo que la API no reconocería como contacto no se pide: ni se arma con ello otra ruta.
  it.each([
    ...["..", "..%2F", "c1", ID.toUpperCase(), `${ID}x`, `x${ID}`, ID.replaceAll("-", "")],
    "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9../", // 36 caracteres, con `../` en el último grupo
    "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9%2F", // 36 caracteres, con una barra codificada
  ])("la dirección %j es «no encontrada» sin preguntar a la API", async (contactId) => {
    await expect(open(contactId)).rejects.toThrow("NEXT_NOT_FOUND");
  });

  it("con un identificador válido monta la ficha", async () => {
    const page = await open(ID);
    expect(page.type).toBe(ContactCard);
    expect(page.props).toEqual({ contactId: ID });
  });
});
