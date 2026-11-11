import { cleanup, configure, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { detail, line, NOW, serve, withTenant } from "./test-support";
import { QuoteDetail } from "./quote-detail";
import { ratePercent } from "./quote-parts";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const show = () => withTenant(<QuoteDetail quoteId="1" />, "quotes.view");
const value = (label: string) =>
  screen.getAllByRole("term").find((node) => node.textContent === label)!.nextElementSibling!;

beforeEach(() => vi.useFakeTimers({ toFake: ["Date"], now: NOW }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Cotizaciones: detalle (F5-09f)", () => {
  it("cabecera, datos, líneas congeladas con todos sus importes y totales con el IGV", async () => {
    serve(() => ({
      status: 200,
      body: detail({
        revision: 3,
        opportunity_id: "o9",
        items: [
          line("l1"),
          line("l2", {
            product_name: "Correa",
            variant_name: "Roja",
            sku: "COR-R",
            quantity: 1,
            promotion_name: "Cyber",
            unit_price: "100.00",
            discount_amount: "10.00",
            final_unit_price: "90.00",
            line_total: "90.00",
          }),
        ],
      }),
    }));
    show();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(
      "COT-000001Revisión 3",
    );
    expect(screen.getByText("Enviada")).toBeVisible();
    expect(
      within(value("Cliente") as HTMLElement).getByRole("link", { name: "Rosa Díaz" }),
    ).toHaveAttribute("href", "/o/acme/clientes/c1");
    expect(within(value("Oportunidad") as HTMLElement).getByRole("link")).toHaveAttribute(
      "href",
      "/o/acme/oportunidades/o9",
    );
    expect(value("IGV")).toHaveTextContent("18 % incluido en los precios");
    expect(value("Válida hasta")).toHaveTextContent("30 nov. 2026");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("CollarTalla M · COL-M2S/ 130.00——S/ 130.00S/ 260.00");
    expect(rows[1]).toHaveTextContent("CorreaRoja · COR-R1S/ 130.00CyberS/ 10.00S/ 90.00S/ 90.00");
    const totals = within(screen.getByRole("region", { name: "Totales" }));
    expect(totals.getByText("Subtotal").nextElementSibling).toHaveTextContent("S/ 423.73");
    expect(totals.getByText("IGV incluido").nextElementSibling).toHaveTextContent("S/ 76.27");
    expect(totals.getByText("Ahorro frente a la lista").nextElementSibling).toHaveTextContent(
      "S/ 20.00",
    );
    expect(totals.getByText("Total").nextElementSibling).toHaveTextContent("S/ 500.00");
    expect(document.body.textContent).not.toMatch(/costo|margen/i);
  });

  it("un cliente sin nombre visible se dice sin enlace; una enviada vencida se marca", async () => {
    serve(() => ({
      status: 200,
      body: detail({
        contact: { id: "c9", display_name: null },
        valid_until: "2026-11-01T05:00:00Z",
      }),
    }));
    show();
    expect(await screen.findByText("Sin permiso para ver su nombre")).toBeVisible();
    expect(within(value("Cliente") as HTMLElement).queryByRole("link")).toBeNull();
    expect(value("Válida hasta")).toHaveTextContent("1 nov. 2026 · Vencida");
    expect(screen.getByText("Vencida")).toBeVisible(); // el estado también lo dice
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveTextContent("Revisión"); // la 1.ª no se dice
    cleanup();
    serve(() => ({
      status: 200,
      body: detail({ status: "ACCEPTED", valid_until: "2026-11-01T05:00:00Z" }),
    }));
    show();
    expect(await screen.findByText("Aceptada")).toBeVisible();
    expect(value("Válida hasta")).not.toHaveTextContent("Vencida"); // solo enviadas o vistas
    cleanup();
    serve(() => ({ status: 200, body: detail({ valid_until: "2026-11-12T05:00:00Z" }) }));
    show();
    expect(await screen.findByText("Enviada")).toBeVisible();
    vi.setSystemTime(Date.parse("2026-11-13T15:00:00Z")); // la pestaña seguía abierta y pasó la validez
    fireEvent.focus(window);
    expect(await screen.findByText("Vencida")).toBeVisible();
  });

  it("las notas, el motivo y los nombres son texto aislado; solo salen los que hay", async () => {
    serve(() => ({
      status: 200,
      body: detail({
        status: "CANCELLED",
        cancelled_at: "2026-11-08T10:00:00Z",
        notes_to_customer: "<b>Gracias</b>\nlínea 2",
        internal_notes: "",
        cancel_reason: "‮oculto <i>no</i>",
        contact: { id: "c1", display_name: "<u>Rosa</u>" },
        items: [
          line("l1", {
            product_name: "<img src=x onerror=alert(1)>",
            promotion_name: "<s>Oferta</s>",
          }),
        ],
      }),
    }));
    show();
    const mine = await screen.findByRole("region", { name: "Notas para el cliente" });
    expect(within(mine).getByText(/<b>Gracias<\/b>/).tagName).toBe("BDI");
    expect(screen.queryByRole("region", { name: /Notas internas/ })).toBeNull();
    expect(mine).not.toHaveTextContent("Cancelada el"); // la fecha es solo del motivo
    const cancel = screen.getByRole("region", { name: "Motivo de la cancelación" });
    expect(within(cancel).getByText(/<i>no<\/i>/).tagName).toBe("BDI");
    expect(cancel).toHaveTextContent("Cancelada el 8 nov. 2026");
    expect(screen.getByText("Cancelada")).toBeVisible();
    expect(screen.getByText("<u>Rosa</u>").tagName).toBe("BDI");
    expect(screen.getByText("<img src=x onerror=alert(1)>").tagName).toBe("BDI");
    expect(screen.getByText("<s>Oferta</s>").tagName).toBe("BDI");
    expect(document.querySelector("b, i, u, img, s")).toBeNull();
  });

  it("sin líneas, un estado o una tasa raros no rompen la pantalla; las notas internas salen si hay", async () => {
    serve(() => ({
      status: 200,
      body: detail({
        status: "constructor",
        items: [],
        internal_notes: "Solo para el equipo",
        tax_rate: "0.1850",
        total: undefined,
      }),
    }));
    show();
    expect(await screen.findByText("Esta cotización no tiene líneas.")).toBeVisible();
    expect(screen.getByText("Estado desconocido")).toBeVisible();
    expect(screen.queryByText("constructor")).toBeNull();
    expect(value("IGV")).toHaveTextContent("18.5 % incluido");
    expect(screen.getByRole("region", { name: /Notas internas/ })).toHaveTextContent(
      "Solo para el equipo",
    );
    expect(screen.getByText("Total").nextElementSibling).toHaveTextContent("—");
  });

  it("404 es «no encontrada»; 403 se explica; un error se reintenta", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const thrown = new Promise<unknown>((resolve) =>
      window.addEventListener("error", (event) => resolve(event.error), { once: true }),
    );
    serve(() => ({ status: 404, body: { code: "NOT_FOUND" } }));
    show();
    expect(String(await thrown)).toContain("NEXT_NOT_FOUND");
    errors.mockRestore();
    cleanup();
    serve(() => ({ status: 403, body: { code: "PERMISSION_DENIED" } }));
    show();
    expect(await screen.findByText("No tienes permiso para ver esta cotización.")).toBeVisible();
    cleanup();
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    serve(() => reply);
    show();
    expect(await screen.findByRole("alert")).toBeVisible();
    reply = { status: 200, body: detail() };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("COT-000001");
  });

  it("el IGV se dice según el modo de la cotización: incluido, no incluido o solo la tasa", async () => {
    for (const [mode, fact, label] of [
      ["INCLUDED", "18 % incluido en los precios", "IGV incluido"],
      ["EXCLUDED", "18 % no incluido en los precios", "IGV"],
      ["NUEVO", "18 %", "IGV"],
    ] as const) {
      serve(() => ({ status: 200, body: detail({ tax_mode: mode }) }));
      show();
      await screen.findByRole("heading", { level: 1 });
      expect(value("IGV")).toHaveTextContent(fact);
      expect(
        within(screen.getByRole("region", { name: "Totales" })).getByText(label),
      ).toBeVisible();
      cleanup();
    }
  });

  it("la tabla de líneas es una región con nombre que recibe el foco (se desplaza en pantallas estrechas)", async () => {
    serve(() => ({ status: 200, body: detail() }));
    show();
    const region = await screen.findByRole("region", { name: "Líneas de la cotización" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("overflow-x-auto");
  });

  it("la tasa se dice moviendo la coma sobre el texto", () => {
    expect([
      ratePercent("0.1800"),
      ratePercent("0.18"),
      ratePercent("0.0500"),
      ratePercent("0.1825"),
      ratePercent("0"),
    ]).toEqual(["18", "18", "5", "18.25", "0"]);
  });
});
