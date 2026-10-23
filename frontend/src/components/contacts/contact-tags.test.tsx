import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ notFound: () => {} }));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext, Tag } from "@/lib/api/model";
import { mockApi, renderApp, renderIntl } from "@/test-utils";

import { ContactCard } from "./contact-card";
import { contrast, inkOn, TagChip } from "./contact-tags";
import { ContactsList } from "./contacts-list";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const LIST = "GET /api/v1/o/acme/contacts/";
const TAGS = "GET /api/v1/o/acme/tags/?limit=200";
const viewer: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "contacts.view", scopes: [] }],
};
const tag = (id: string, name: string, color: string, is_active = true): Tag => ({
  ...{ id, name, color, is_active, applies_to: "CONTACT" },
});
const vip = tag("t1", "VIP", "#ffdb5b");
const old = tag("t2", "Antiguo", "#202020", false);
const contact = (id: string, changes: Partial<Contact> = {}): Contact => ({
  ...{ id, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: "u1", branch_id: null, source: "MANUAL" },
  ...{ created_by_user_id: "u1", created_at: "2026-10-23T10:00:00Z" },
  ...{ updated_at: "2026-10-23T10:00:00Z", version: 1, tags: [] },
  ...changes,
});
const rosa = contact("c1", { tags: [vip, old] });
const luis = contact("c2", { display_name: "Luis Paz" });
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
const list = () =>
  renderApp(
    <TenantProvider value={viewer}>
      <ContactsList />
    </TenantProvider>,
  );
const hex = (n: number) => `#${n.toString(16).padStart(6, "0")}`;

afterEach(() => vi.unstubAllGlobals());

describe("el color de una etiqueta", () => {
  it("el texto es negro o blanco, el que llega a 4,5:1 sobre cualquier color", () => {
    expect(inkOn("#ffdb5b")).toBe("#000000"); // miel
    expect(inkOn("#ffffff")).toBe("#000000");
    expect(inkOn("#202020")).toBe("#ffffff"); // tinta
    expect(inkOn("#1a7f37")).toBe("#ffffff");
    expect(inkOn("#b42318")).toBe("#ffffff");
    expect(inkOn("#777777")).toBe("#000000"); // un gris medio: con tinta no llegaría
    expect(contrast("#777777", "#202020")).toBeLessThan(4.5);
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21);
    // Valores conocidos de WCAG, calculados fuera: no se mide `contrast` con `contrast`.
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
    expect(contrast("#ff0000", "#000000")).toBeCloseTo(5.25, 2);
    expect(contrast("#00ff00", "#000000")).toBeCloseTo(15.3, 1);
    expect(contrast("#010101", "#000000")).toBeCloseTo(1.006, 3); // tramo lineal de sRGB
    // Una muestra de todo el cubo de color, más los grises.
    const colors = [
      ...Array.from({ length: 4096 }, (_, at) => hex(at * 4097)),
      ...Array.from({ length: 256 }, (_, at) => hex(at * 0x010101)),
    ];
    const worst = Math.min(...colors.map((color) => contrast(color, inkOn(color))));
    expect(worst).toBeGreaterThanOrEqual(4.5);
  });

  it("el nombre siempre se escribe; una desactivada lo dice con texto", () => {
    renderIntl(
      <>
        <TagChip tag={vip} />
        <TagChip tag={old} />
        <TagChip tag={{ name: "Rara", color: "rojo", is_active: true }} />
      </>,
    );
    const chip = screen.getByText("VIP").parentElement!;
    expect(chip).toHaveStyle({ backgroundColor: "#ffdb5b", color: "#000000" });
    expect(chip).toHaveClass("border", "border-foreground/20");
    expect(chip).not.toHaveClass("border-dashed");
    const gone = screen.getByText("Antiguo").parentElement!;
    expect(gone).toHaveTextContent("Antiguo· desactivada");
    expect(gone).toHaveClass("border-dashed");
    expect(gone).toHaveStyle({ color: "#ffffff" });
    const odd = screen.getByText("Rara").parentElement!; // un color que no es #rrggbb
    expect(odd.getAttribute("style")).toBeNull();
    expect(odd).toHaveClass("bg-surface", "text-foreground");
  });
});

describe("ContactsList: etiquetas", () => {
  it("cada fila enseña las suyas; sin etiquetas, nada", async () => {
    mockApi({ [LIST]: page([rosa, luis]), [TAGS]: page([vip, old]) });
    list();
    const rows = [...(await screen.findByRole("list", { name: "Clientes" })).children];
    const first = rows.find((row) => row.textContent?.includes("Rosa Díaz")) as HTMLElement;
    const chips = within(first).getByRole("list", { name: "Etiquetas" });
    expect(
      within(chips)
        .getAllByRole("listitem")
        .map((one) => one.textContent),
    ).toEqual(["VIP", "Antiguo· desactivada"]);
    const second = rows.find((row) => row.textContent?.includes("Luis Paz")) as HTMLElement;
    expect(within(second).queryByRole("list", { name: "Etiquetas" })).toBeNull();
  });

  it("filtra por etiqueta, también con «Cargar más»; vacía lo dice; «Todas» la quita", async () => {
    const api = mockApi({
      [LIST]: page([rosa, luis]),
      [TAGS]: page([vip, old]),
      [`${LIST}?tag=t1`]: page([rosa], "p2"),
      [`${LIST}?tag=t1&cursor=p2`]: page([contact("c3", { display_name: "Ana Sol" })]),
      [`${LIST}?tag=t2`]: page([]),
    });
    list();
    const filter = await screen.findByRole("combobox", { name: "Etiqueta" });
    await waitFor(() =>
      expect([...(filter as HTMLSelectElement).options].map((one) => one.text)).toEqual([
        "Todas las etiquetas",
        "VIP",
        "Antiguo (desactivada)",
      ]),
    );
    expect(filter).toHaveClass("text-[16px]", "h-11"); // 390 px: sin zoom y al alcance del dedo
    fireEvent.change(filter, { target: { value: "t1" } });
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    expect(screen.queryByText("Luis Paz")).toBeNull();
    expect(await screen.findByText("Ana Sol")).toBeVisible();
    expect(urls(api)).toContain("/api/v1/o/acme/contacts/?tag=t1&cursor=p2");
    fireEvent.change(filter, { target: { value: "t2" } });
    expect(
      await screen.findByText("Ningún cliente que puedas ver lleva la etiqueta «Antiguo»."),
    ).toBeVisible();
    fireEvent.change(filter, { target: { value: "" } });
    expect(await screen.findByText("Luis Paz")).toBeVisible();
    expect(filter).toHaveValue("");
  });

  it("si el catálogo no llega: 403 lo dice sin reintento; otro fallo, con «Reintentar»", async () => {
    let reply: { status: number; body: object } = {
      status: 403,
      body: { code: "PERMISSION_DENIED" },
    };
    mockApi({ [LIST]: page([luis]), [TAGS]: () => reply });
    const view = list();
    const filter = await screen.findByRole("combobox", { name: "Etiqueta" });
    await waitFor(() =>
      expect(filter).toHaveAccessibleDescription("No tienes permiso para ver las etiquetas."),
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
    view.unmount();
    reply = { status: 500, body: {} };
    list();
    fireEvent.click(await screen.findByRole("button", { name: "Reintentar" }));
    reply = page([vip]);
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Etiqueta" })).toHaveTextContent("VIP"),
    );
  });
});

describe("ContactCard: etiquetas", () => {
  const card = (body: Contact) => {
    mockApi({ [`GET /api/v1/o/acme/contacts/${ID}/`]: { status: 200, body } });
    renderApp(
      <TenantProvider value={viewer}>
        <ContactCard contactId={ID} />
      </TenantProvider>,
    );
  };

  it("«Resumen» enseña las suyas, también la desactivada", async () => {
    card({ ...rosa, id: ID });
    const box = await screen.findByRole("region", { name: "Etiquetas" });
    expect(within(box).getByRole("list", { name: "Etiquetas" })).toHaveTextContent(
      "VIPAntiguo· desactivada",
    );
  });

  it("sin etiquetas, lo dice", async () => {
    card({ ...luis, id: ID });
    const box = await screen.findByRole("region", { name: "Etiquetas" });
    expect(box).toHaveTextContent("Sin etiquetas.");
    expect(within(box).queryByRole("list")).toBeNull();
  });
});
