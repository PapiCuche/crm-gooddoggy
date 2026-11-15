import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { QuoteDetail } from "./quote-detail";
import { QuoteEditor } from "./quote-editor";
import { BASE, detail, line, page, withTenant } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeEach(() => {
  document.cookie = "csrftoken=t"; // sin ella, la primera escritura pide el token
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Reply = { status: number; body?: unknown };
type Call = { method: string; path: string; body: unknown };
// El servidor de la prueba por «MÉTODO ruta» (sin la consulta); guarda las escrituras.
function api(routes: Record<string, Reply | ((body: unknown) => Reply)>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://x");
      const method = (init?.method ?? "GET").toUpperCase();
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      if (method !== "GET") calls.push({ method, path: url.pathname, body });
      const route = routes[`${method} ${url.pathname}`] ?? { status: 404, body: { code: "X" } };
      const { status, body: out } = typeof route === "function" ? route(body) : route;
      return new Response(out === undefined ? null : JSON.stringify(out), { status });
    }),
  );
  return calls;
}
const variants = page({ id: "v2", name: "Roja", sku: "COR-R", product_name: "Correa" });
const field = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
const type = (name: string, value: string) => fireEvent.change(field(name), { target: { value } });

describe("Cotizaciones: editor (F5-09g)", () => {
  it("crea un borrador de la oportunidad con variante, cantidad, validez y notas; sin precio", async () => {
    const calls = api({
      "GET /api/v1/o/acme/variants/": variants,
      [`POST ${BASE}`]: { status: 201, body: detail({ id: "q9", status: "DRAFT" }) },
    });
    const onSaved = vi.fn();
    withTenant(
      <QuoteEditor link={{ opportunity_id: "o1" }} onCancel={() => {}} onSaved={onSaved} />,
      "quotes.create",
    );
    expect(screen.getByText(/El precio lo pone la lista de precios/)).toBeVisible();
    type("Variante 1", "corr");
    fireEvent.click(await screen.findByRole("button", { name: "Correa · Roja (COR-R)" }));
    type("Cantidad", "3");
    type("Válida hasta (opcional)", "2026-11-30");
    type("Notas para el cliente (opcional)", "Entrega en 48 h");
    fireEvent.click(screen.getByRole("button", { name: "Crear borrador" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(calls).toEqual([
      {
        method: "POST",
        path: BASE,
        body: {
          opportunity_id: "o1",
          items: [{ variant_id: "v2", quantity: 3 }],
          valid_until: "2026-12-01T04:59:00.000Z", // fin del día en Lima (UTC-5)
          notes_to_customer: "Entrega en 48 h",
          internal_notes: "",
        },
      },
    ]);
  });

  it("sin variante o con una cantidad no entera no envía nada y lo dice", async () => {
    const calls = api({});
    withTenant(<QuoteEditor link={{ contact_id: "c1" }} onCancel={() => {}} onSaved={() => {}} />);
    type("Cantidad", "1.5");
    fireEvent.click(screen.getByRole("button", { name: "Crear borrador" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Elige una variante en cada línea");
    expect(field("Cantidad")).toHaveAttribute("aria-invalid", "true");
    expect(calls).toEqual([]);
  });

  it.each([
    [{ code: "VALIDATION_ERROR", fields: { variant_id: ["x"] } }, 400, "no tiene precio vigente"],
    [{ code: "VALIDATION_ERROR", fields: { _: ["x"] } }, 400, "El cliente o la oportunidad"],
    [{ code: "QUOTE_REFUSED" }, 409, "no admite ese cambio"],
  ])("un error de la API se explica por su código: %j", async (body, status, text) => {
    api({ "GET /api/v1/o/acme/variants/": variants, [`POST ${BASE}`]: { status, body } });
    withTenant(<QuoteEditor link={{ contact_id: "c1" }} onCancel={() => {}} onSaved={() => {}} />);
    type("Variante 1", "corr");
    fireEvent.click(await screen.findByRole("button", { name: "Correa · Roja (COR-R)" }));
    fireEvent.click(screen.getByRole("button", { name: "Crear borrador" }));
    expect(await screen.findByText(new RegExp(text))).toHaveAttribute("role", "alert");
  });

  it("al editar solo viaja lo cambiado, con la versión leída; un 409 se explica", async () => {
    const calls = api({
      [`PATCH ${BASE}1/`]: { status: 409, body: { code: "STALE_VERSION" } },
    });
    const quote = detail({ status: "DRAFT", version: 4, valid_until: null, internal_notes: "a" });
    withTenant(<QuoteEditor quote={quote} onCancel={() => {}} onSaved={() => {}} />);
    expect(screen.getByText(/se quitan sus descuentos/)).toBeVisible();
    expect(screen.getByText(/Variante 1: Collar · Talla M \(COL-M\)/)).toBeVisible();
    type("Notas internas (opcional; no las ve el cliente)", "b");
    fireEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByText(/Otra persona cambió la cotización/)).toHaveAttribute(
      "role",
      "alert",
    );
    expect(calls).toEqual([
      { method: "PATCH", path: `${BASE}1/`, body: { version: 4, internal_notes: "b" } },
    ]);
    type("Cantidad", "5");
    fireEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]?.body).toEqual({
      version: 4,
      items: [{ variant_id: "v1", quantity: 5 }],
      internal_notes: "b",
    });
  });
});

describe("Cotizaciones: borrador en el detalle (F5-09g)", () => {
  const draft = detail({ status: "DRAFT", version: 2 });
  const show = (...codes: string[]) =>
    withTenant(<QuoteDetail quoteId="1" />, "quotes.view", ...codes);

  it("sin `quotes.update` y `quotes.discount`, ni editar ni descontar; fuera de borrador, tampoco", async () => {
    api({ [`GET ${BASE}1/`]: { status: 200, body: draft } });
    show("quotes.update");
    expect(await screen.findByRole("button", { name: "Editar" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Descuento de Collar" })).toBeNull();
    cleanup();
    api({ [`GET ${BASE}1/`]: { status: 200, body: detail({ status: "APPROVED" }) } });
    show("quotes.update", "quotes.discount");
    expect(await screen.findByText("Aprobada")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Descuento de Collar" })).toBeNull();
  });

  it("un descuento sobre el límite deja la cotización por aprobar y lo dice", async () => {
    const pending = detail({
      status: "PENDING_APPROVAL",
      version: 3,
      items: [line("l1", { discount_type: "PERCENT", discount_amount: "39.00" })],
    });
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: draft },
      [`POST ${BASE}1/items/l1/discount/`]: { status: 200, body: pending },
    });
    show("quotes.update", "quotes.discount");
    fireEvent.click(await screen.findByRole("button", { name: "Descuento de Collar" }));
    const form = within(screen.getByRole("form", { name: "Descuento de Collar" }));
    expect(form.getByText(/Si pasa de tu límite de descuento/)).toBeVisible();
    const percent = (value: string) =>
      fireEvent.change(form.getByLabelText("Porcentaje (%)"), { target: { value } });
    percent("1e2");
    fireEvent.click(form.getByRole("button", { name: "Aplicar" }));
    expect(form.getByRole("alert")).toHaveTextContent("hasta 2 decimales");
    expect(calls).toEqual([]);
    percent("30");
    fireEvent.click(form.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText(/pasa de tu límite: la cotización queda/)).toBeVisible();
    expect(screen.getByText(/Espera aprobación/)).toBeVisible();
    expect(screen.getByText("Por aprobar")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expect(calls).toEqual([
      {
        method: "POST",
        path: `${BASE}1/items/l1/discount/`,
        body: { version: 2, discount_type: "PERCENT", discount_value: "30" },
      },
    ]);
  });

  it("un 400 del valor se explica en el formulario; quitarlo no envía valor", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: draft },
      [`POST ${BASE}1/items/l1/discount/`]: (body) =>
        (body as { discount_type: string }).discount_type === "NONE"
          ? { status: 200, body: draft }
          : { status: 400, body: { code: "VALIDATION_ERROR", fields: { _: ["x"] } } },
    });
    show("quotes.update", "quotes.discount");
    fireEvent.click(await screen.findByRole("button", { name: "Descuento de Collar" }));
    const form = within(screen.getByRole("form", { name: "Descuento de Collar" }));
    fireEvent.change(form.getByLabelText("Tipo"), { target: { value: "AMOUNT" } });
    fireEvent.change(form.getByLabelText("Importe por unidad, con IGV (PEN)"), {
      target: { value: "130" },
    });
    fireEvent.click(form.getByRole("button", { name: "Aplicar" }));
    expect(await form.findByText(/no puede quedar en cero/)).toHaveAttribute("role", "alert");
    fireEvent.change(form.getByLabelText("Tipo"), { target: { value: "NONE" } });
    fireEvent.click(form.getByRole("button", { name: "Aplicar" }));
    expect(await screen.findByText("Descuento aplicado.")).toBeVisible();
    expect(calls.map((c) => c.body)).toEqual([
      { version: 2, discount_type: "AMOUNT", discount_value: "130" },
      { version: 2, discount_type: "NONE" },
    ]);
  });
});
