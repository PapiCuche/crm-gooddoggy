import { cleanup, configure, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { ContactCard } from "@/components/contacts/contact-card";
import type { Contact } from "@/lib/api/model";
import { mockApi } from "@/test-utils";

import { LinkedQuotes } from "./linked-quotes";
import { NOW, page, quote, serve, withTenant } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const rosa = {
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: null, branch_id: null, source: "WALK_IN" },
  ...{ created_by_user_id: "u1", version: 1, tags: [] },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-24T20:30:00Z" },
} as Contact;
const show = (link: { contact_id: string } | { opportunity_id: string }) =>
  withTenant(<LinkedQuotes link={link} contactId={ID} />, "quotes.view");

beforeEach(() => vi.useFakeTimers({ toFake: ["Date"], now: NOW }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("cotizaciones de una oportunidad y de un cliente (F5-09f3)", () => {
  it("pide las del vínculo y enseña número, estado, total y validez, enlazadas al detalle", async () => {
    const urls = serve(() =>
      page(quote("1"), quote("2", { status: "DRAFT", valid_until: null, number: "<b>X</b>" })),
    );
    show({ opportunity_id: "o9" });
    const rows = within(await screen.findByRole("list", { name: "Cotizaciones" })).getAllByRole(
      "listitem",
    );
    expect(Object.fromEntries(urls[0]!.searchParams)).toEqual({
      opportunity_id: "o9",
      limit: "50",
    });
    expect(rows[0]).toHaveTextContent("COT-000001");
    expect(rows[0]).toHaveTextContent("Enviada");
    expect(rows[0]).toHaveTextContent("S/ 500.00");
    expect(rows[0]).toHaveTextContent("Válida hasta 30 nov. 2026");
    expect(within(rows[0]!).getByRole("link", { name: "COT-000001" })).toHaveAttribute(
      "href",
      "/o/acme/cotizaciones/1",
    );
    expect(rows[1]).toHaveTextContent("Sin fecha de validez");
    expect(screen.getByText("<b>X</b>").tagName).toBe("BDI");
    expect(document.querySelector("main b, b")).toBeNull();
    expect(screen.getByRole("link", { name: "Ver todas las de este cliente" })).toHaveAttribute(
      "href",
      `/o/acme/cotizaciones?cliente=${ID}`,
    );
    expect(screen.queryByText(/primeras/)).toBeNull();
  });

  it("una enviada vencida dice «Vencida»; con más de las que caben se dice", async () => {
    const urls = serve(() => ({
      status: 200,
      body: { results: [quote("1", { valid_until: "2026-11-01T05:00:00Z" })], next: "c2" },
    }));
    show({ contact_id: ID });
    expect(await screen.findByText("Vencida")).toBeVisible();
    expect(Object.fromEntries(urls[0]!.searchParams)).toEqual({ contact_id: ID, limit: "50" });
    expect(screen.getByText(/Se muestran las primeras 50\./)).toBeVisible();
  });

  it("vacío, 403 explicado y un error se reintenta", async () => {
    serve(() => page());
    show({ contact_id: ID });
    expect(await screen.findByText("Todavía no hay cotizaciones.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Ver todas/ })).toBeNull();
    cleanup();
    serve(() => ({ status: 403, body: { code: "PERMISSION_DENIED" } }));
    show({ contact_id: ID });
    expect(await screen.findByText("No tienes permiso para ver las cotizaciones.")).toBeVisible();
    cleanup();
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    serve(() => reply);
    show({ contact_id: ID });
    expect(await screen.findByRole("alert")).toBeVisible();
    reply = page(quote("1"));
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("COT-000001")).toBeVisible();
  });

  it("la ficha tiene la pestaña solo con quotes.view y no pide nada hasta abrirla", async () => {
    const calls: string[] = [];
    const api = mockApi({
      [`GET /api/v1/o/acme/contacts/${ID}/`]: { status: 200, body: rosa },
      [`GET /api/v1/o/acme/quotes/?contact_id=${ID}&limit=50`]: page(quote("1")),
    });
    const tabs = () =>
      within(screen.getByRole("tablist"))
        .getAllByRole("tab")
        .map((tab) => tab.textContent);
    withTenant(<ContactCard contactId={ID} />, "contacts.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs()).toEqual(["Resumen", "Notas"]);
    cleanup();
    withTenant(<ContactCard contactId={ID} />, "contacts.view", "quotes.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs()).toEqual(["Resumen", "Notas", "Cotizaciones"]);
    calls.push(...api.mock.calls.map(([url]) => String(url)));
    expect(calls.some((url) => url.includes("/quotes/"))).toBe(false);
    fireEvent.click(screen.getByRole("tab", { name: "Cotizaciones" }));
    expect(await screen.findByText("COT-000001")).toBeVisible();
  });
});
