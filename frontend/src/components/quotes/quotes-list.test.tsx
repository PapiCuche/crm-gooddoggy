import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NOW, page, quote, serve, withTenant } from "./test-support";
import { QuotesList } from "./quotes-list";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const show = (contactId?: string) =>
  withTenant(<QuotesList contactId={contactId} />, "quotes.view");
const params = (urls: URL[]) => Object.fromEntries(urls.at(-1)!.searchParams);

beforeEach(() => vi.useFakeTimers({ toFake: ["Date"], now: NOW }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Cotizaciones: lista (F5-09f)", () => {
  it("enseña número, estado, cliente, total con la moneda y fechas, y enlaza al detalle", async () => {
    serve(() =>
      page(quote("1", { revision: 2 }), quote("2", { status: "DRAFT", valid_until: null })),
    );
    show();
    const rows = within(await screen.findByRole("list", { name: "Cotizaciones" })).getAllByRole(
      "listitem",
    );
    expect(rows[0]).toHaveTextContent("COT-000001");
    expect(rows[0]).toHaveTextContent("Revisión 2");
    expect(rows[0]).toHaveTextContent("Enviada");
    expect(rows[0]).toHaveTextContent("Rosa Díaz");
    expect(rows[0]).toHaveTextContent("S/ 500.00");
    expect(rows[0]).toHaveTextContent("Válida hasta 30 nov. 2026");
    expect(within(rows[0]!).getByRole("link", { name: "COT-000001" })).toHaveAttribute(
      "href",
      "/o/acme/cotizaciones/1",
    );
    expect(rows[1]).toHaveTextContent("Borrador");
    expect(rows[1]).toHaveTextContent("Sin fecha de validez");
    expect(rows[1]).not.toHaveTextContent("Revisión");
  });

  it("un contacto sin nombre visible se dice sin inventarlo; el texto ajeno va aislado", async () => {
    serve(() =>
      page(
        quote("1", { contact: { id: "c9", display_name: null } }),
        quote("2", { number: "<b>X</b>", contact: { id: "c2", display_name: "<i>Luis</i>" } }),
      ),
    );
    show();
    expect(await screen.findByText("Cliente (sin permiso para ver su nombre)")).toBeVisible();
    expect(screen.getByText("<i>Luis</i>").tagName).toBe("BDI");
    expect(screen.getByText("<b>X</b>").tagName).toBe("BDI");
    expect(document.querySelector("i, b")).toBeNull();
  });

  it("una enviada con la validez pasada se marca como vencida; las demás no", async () => {
    serve(() =>
      page(
        quote("1", { valid_until: "2026-11-01T05:00:00Z" }),
        quote("2", { status: "ACCEPTED", valid_until: "2026-11-01T05:00:00Z" }),
        quote("3", { valid_until: "2026-12-01T05:00:00Z" }),
      ),
    );
    show();
    const rows = within(await screen.findByRole("list", { name: "Cotizaciones" })).getAllByRole(
      "listitem",
    );
    expect(rows[0]).toHaveTextContent("· Vencida");
    expect(within(rows[0]!).queryByText("Enviada")).toBeNull(); // el estado también dice «Vencida»
    expect(rows[1]).not.toHaveTextContent("· Vencida");
    expect(rows[2]).not.toHaveTextContent("· Vencida");
  });

  it("los filtros viajan a la API: estado, mías, vencidas, número exacto y cliente de la dirección", async () => {
    const urls = serve(() => page(quote("1")));
    show("c1");
    await screen.findByText("COT-000001");
    expect(params(urls)).toEqual({ contact_id: "c1" });
    fireEvent.change(screen.getByRole("combobox", { name: "Estado" }), {
      target: { value: "ACCEPTED" },
    });
    await waitFor(() =>
      expect(params(urls)).toMatchObject({ status: "ACCEPTED", contact_id: "c1" }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Solo las que creé" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /^Vencidas/ }));
    await waitFor(() => expect(params(urls)).toMatchObject({ mine: "true", expired: "true" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Número" }), {
      target: { value: " COT-000007 " },
    });
    await waitFor(() => expect(params(urls)).toMatchObject({ number: "COT-000007" }));
    fireEvent.click(screen.getByRole("button", { name: "Quitar el filtro" }));
    await waitFor(() => expect(params(urls)).not.toHaveProperty("contact_id"));
  });

  it("vacío con y sin filtros, 403 explicado, error con reintento y «Cargar más»", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const urls = serve((url) =>
      url.searchParams.get("cursor")
        ? page(quote("2"))
        : { status: 200, body: { results: [quote("1")], next: "c2" } },
    );
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("COT-000002")).toBeVisible();
    expect(urls.at(-1)!.searchParams.get("cursor")).toBe("c2");
    cleanup();
    serve(() => page());
    show();
    expect(await screen.findByText("Todavía no hay cotizaciones.")).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "Estado" }), {
      target: { value: "SENT" },
    });
    expect(await screen.findByText("Ninguna cotización coincide con los filtros.")).toBeVisible();
    cleanup();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    serve(() => reply);
    show();
    expect(await screen.findByRole("alert")).toBeVisible();
    reply = page(quote("1"));
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("COT-000001")).toBeVisible();
    cleanup();
    serve(() => ({ status: 403, body: { code: "PERMISSION_DENIED" } }));
    show();
    expect(await screen.findByText(/No tienes permiso para ver las cotizaciones/)).toBeVisible();
  });
});
