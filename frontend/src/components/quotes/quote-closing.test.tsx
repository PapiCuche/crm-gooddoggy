import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
  useRouter: () => ({ push }),
}));

import { QuoteDetail } from "./quote-detail";
import { api, BASE, detail, NOW, withTenant } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeEach(() => {
  document.cookie = "csrftoken=t";
  vi.useFakeTimers({ toFake: ["Date"], now: NOW }); // 11 nov 2026; la validez llega al 29 nov
  push.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const SELLER = ["quotes.send", "quotes.create", "quotes.update", "quotes.cancel"];
const at = (status: string, changes: Record<string, unknown> = {}) =>
  detail({ status, version: 3, created_by_user_id: "u1", ...changes });
const show = (...codes: string[]) =>
  withTenant(<QuoteDetail quoteId="1" />, "quotes.view", ...codes);
const button = (name: string) => screen.queryByRole("button", { name });
const heading = () => screen.getByRole("heading", { level: 1 });
const open = async (verb: string, form: string) => {
  fireEvent.click(await screen.findByRole("button", { name: verb }));
  return screen.getByRole("form", { name: form });
};
const confirm = (form: HTMLElement, name: string) =>
  fireEvent.click(within(form).getByRole("button", { name }));
const write = (form: HTMLElement, label: string, value: string) =>
  fireEvent.change(within(form).getByLabelText(label), { target: { value } });
const [NOON, PAST] = ["2026-11-11T15:00:00Z", "2026-11-01T05:00:00Z"];
const TONIGHT = "2026-11-12T04:59:00Z"; // 11 nov, 23:59 en Lima
// Una confirmación sin campo: el foco va al formulario, que se anuncia; nunca a confirmar.
const announced = (form: HTMLElement, text: RegExp) => {
  expect(document.activeElement).toBe(form);
  expect(form).toHaveAccessibleDescription(text);
};

describe("Cotizaciones: emitir, respuesta del cliente, revisar y cancelar (F5-09i)", () => {
  it("una vendedora emite una aprobada con validez, condiciones y garantía; se ven congeladas", async () => {
    const frozen = { terms_snapshot: "Pago contra entrega", warranty_snapshot: "12 meses" };
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("APPROVED") },
      [`POST ${BASE}1/issue/`]: { status: 200, body: at("SENT", { sent_at: NOON, ...frozen }) },
    });
    show(...SELLER);
    const form = await open("Emitir", "Emitir COT-000001");
    const day = within(form).getByLabelText("Válida hasta");
    expect(day).toHaveValue("2026-11-30"); // la que tiene, en la hora de Lima
    expect(day).toHaveAttribute("min", "2026-11-11");
    expect(day).toHaveAttribute("max", "2099-12-30");
    expect(document.activeElement).toBe(day);
    const terms = within(form).getByLabelText("Condiciones (opcional)");
    expect(terms).toHaveAttribute("maxLength", "2000"); // el tope de la API
    expect(terms).toHaveAccessibleDescription("Hasta 2000 caracteres. Las ve el cliente.");
    write(form, "Válida hasta", "2026-12-15");
    write(form, "Condiciones (opcional)", " Pago contra entrega ");
    write(form, "Garantía (opcional)", "12 meses");
    confirm(form, "Emitir la cotización");
    expect(await screen.findByText("Cotización emitida: ya no se edita.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Condiciones" })).toHaveTextContent("contra entrega");
    expect(screen.getByRole("region", { name: "Garantía" })).toHaveTextContent("12 meses");
    expect([screen.getByText("Enviada"), screen.getByText("Emitida")]).toHaveLength(2);
    expect(button("Emitir")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(heading()));
    const sent = { terms: "Pago contra entrega", warranty: "12 meses" };
    const until = { version: 3, valid_until: "2026-12-16T04:59:00.000Z" };
    expect(calls.map((c) => c.body)).toEqual([{ ...until, ...sent }]);
  });

  it("emitir con la validez ya pasada pide otra fecha desde hoy", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("DRAFT", { valid_until: PAST }) },
      [`POST ${BASE}1/issue/`]: { status: 409, body: { code: "NEEDS_APPROVAL" } },
    });
    show(...SELLER);
    const form = await open("Emitir", "Emitir COT-000001");
    const day = within(form).getByLabelText("Válida hasta");
    expect(day).toHaveValue("");
    confirm(form, "Emitir la cotización");
    expect(within(form).getByRole("alert")).toHaveTextContent("Elige hasta qué día es válida.");
    write(form, "Válida hasta", "2026-11-10");
    confirm(form, "Emitir la cotización");
    expect(within(form).getByRole("alert")).toHaveTextContent("desde hoy hasta el año 2099");
    expect(day).toHaveAttribute("aria-invalid", "true");
    expect(calls).toEqual([]);
    write(form, "Válida hasta", "2026-11-11"); // hoy, hasta las 23:59
    confirm(form, "Emitir la cotización");
    expect(await within(form).findByText(/necesita una aprobación: valídala/)).toBeVisible();
    const until = { version: 3, valid_until: "2026-11-12T04:59:00.000Z" };
    expect(calls.map((c) => c.body)).toEqual([until]);
  });

  it("a las 23:30 de Lima «hoy» sigue siendo el día de Lima, no el de UTC (F5-09j)", async () => {
    vi.setSystemTime(Date.parse("2026-11-12T04:30:00Z")); // 11 nov, 23:30 en Lima
    api({ [`GET ${BASE}1/`]: { status: 200, body: at("APPROVED", { valid_until: TONIGHT }) } });
    show(...SELLER);
    const form = await open("Emitir", "Emitir COT-000001");
    const day = within(form).getByLabelText("Válida hasta");
    expect(day).toHaveValue("2026-11-11"); // vence hoy a las 23:59: aún vale
    expect(day).toHaveAttribute("min", "2026-11-11");
  });

  it("el cliente acepta con una confirmación, o rechaza con su motivo, que se ve después", async () => {
    const said = { rejected_at: NOON, rejection_reason: "Compró en otra tienda" };
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("SENT") },
      [`POST ${BASE}1/accept/`]: { status: 200, body: at("ACCEPTED", { accepted_at: NOON }) },
    });
    show("quotes.send");
    let form = await open("El cliente aceptó", "El cliente aceptó COT-000001");
    announced(form, /pasa a «Pago pendiente»/);
    confirm(form, "Confirmar la aceptación");
    expect(await screen.findByText("Aceptación del cliente registrada.")).toBeVisible();
    expect(screen.getByText("Aceptada por el cliente")).toBeVisible();
    expect(button("El cliente rechazó")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(heading()));
    expect(calls.map((c) => [c.path, c.body])).toEqual([[`${BASE}1/accept/`, { version: 3 }]]);
    cleanup();
    const later = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("VIEWED") },
      [`POST ${BASE}1/decline/`]: { status: 200, body: at("REJECTED", said) },
    });
    show("quotes.send");
    form = await open("El cliente rechazó", "El cliente rechazó COT-000001");
    const reason = within(form).getByLabelText("Motivo que dio el cliente");
    expect(reason).toHaveAttribute("maxLength", "500");
    confirm(form, "Confirmar el rechazo del cliente");
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe el motivo del rechazo.");
    write(form, "Motivo que dio el cliente", "Compró en otra tienda");
    confirm(form, "Confirmar el rechazo del cliente");
    expect(await screen.findByText("Rechazo del cliente registrado.")).toBeVisible();
    const shown = screen.getByRole("region", { name: "Motivo del rechazo del cliente" });
    expect(shown).toHaveTextContent("Compró en otra tienda");
    expect(screen.getByText("Rechazada por el cliente")).toBeVisible();
    expect(later.map((c) => c.body)).toEqual([{ version: 3, reason: "Compró en otra tienda" }]);
  });

  it("revisar confirma y lleva a la revisión nueva; sin `quotes.send`, una emitida no", async () => {
    let reads = 0;
    const calls = api({
      [`GET ${BASE}1/`]: () => ({ status: 200, body: at(reads++ ? "CANCELLED" : "SENT") }),
      [`POST ${BASE}1/revise/`]: {
        status: 200,
        body: { ...at("DRAFT", { version: 1, revision: 2 }), id: "2" },
      },
    });
    show(...SELLER);
    const form = await open("Nueva revisión", "Nueva revisión de COT-000001");
    announced(form, /COT-000001 queda cancelada.*se abre la revisión 2 como borrador/);
    confirm(form, "Abrir la revisión 2");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/o/acme/cotizaciones/2"));
    expect(calls.map((c) => [c.path, c.body])).toEqual([[`${BASE}1/revise/`, { version: 3 }]]);
    await waitFor(() => expect(reads).toBe(2)); // la revisada se vuelve a leer: ya está cancelada
    cleanup();
    api({ [`GET ${BASE}1/`]: { status: 200, body: at("SENT") } });
    show("quotes.create", "quotes.update", "quotes.cancel");
    expect(await screen.findByRole("button", { name: "Cancelar la cotización" })).toBeVisible();
    expect(button("Nueva revisión")).toBeNull();
    cleanup(); // revisar cancela la anterior: sin `quotes.cancel`, tampoco
    api({ [`GET ${BASE}1/`]: { status: 200, body: at("APPROVED") } });
    show("quotes.create", "quotes.update", "quotes.send");
    expect(await screen.findByRole("button", { name: "Emitir" })).toBeVisible();
    expect(button("Nueva revisión")).toBeNull();
  });

  it("quien pidió la aprobación lee por qué se rechazó; después queda en las notas", async () => {
    const reason = "Margen muy bajo <b>DNI</b>";
    api({
      [`GET ${BASE}1/`]: { status: 200, body: at("DRAFT", { approval_rejection_reason: reason }) },
    });
    show("quotes.update");
    const notice = await screen.findByRole("complementary", {
      name: "Rechazaron la solicitud de aprobación",
    });
    expect(notice).toHaveTextContent(`${reason}Corrige el borrador y vuelve a validarlo.`);
    expect(
      screen.queryByRole("region", { name: "Motivo del rechazo de la aprobación" }),
    ).toBeNull();
    cleanup();
    api({
      [`GET ${BASE}1/`]: { status: 200, body: at("SENT", { approval_rejection_reason: reason }) },
    });
    show();
    const note = await screen.findByRole("region", { name: "Motivo del rechazo de la aprobación" });
    expect(note).toHaveTextContent(reason);
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("cancelar pide confirmación; Volver no escribe; el motivo es opcional", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("DRAFT") },
      [`POST ${BASE}1/cancel/`]: {
        status: 200,
        body: at("CANCELLED", { cancelled_at: NOON, cancel_reason: "Ya no lo necesita" }),
      },
    });
    show("quotes.cancel");
    const trigger = await screen.findByRole("button", { name: "Cancelar la cotización" });
    trigger.focus();
    let form = await open("Cancelar la cotización", "Cancelar COT-000001");
    expect(form).toHaveTextContent("no se puede deshacer");
    confirm(form, "Volver");
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    form = await open("Cancelar la cotización", "Cancelar COT-000001");
    write(form, "Motivo (opcional)", "Ya no lo necesita");
    confirm(form, "Confirmar la cancelación");
    expect(await screen.findByText("Cotización cancelada.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Motivo de la cancelación" })).toBeVisible();
    expect(screen.queryAllByRole("button")).toEqual([]);
    expect(calls.map((c) => c.body)).toEqual([{ version: 3, reason: "Ya no lo necesita" }]);
  });

  it("cada botón, solo con su estado y su permiso", async () => {
    const offered = async (body: unknown, ...codes: string[]) => {
      api({ [`GET ${BASE}1/`]: { status: 200, body } });
      show(...codes);
      await screen.findByRole("heading", { level: 1 });
      const names = screen.queryAllByRole("button").map((x) => x.textContent);
      cleanup();
      return names;
    };
    const all = [...SELLER, "quotes.approve"];
    expect(await offered(at("DRAFT"), ...all)).toEqual([
      ...["Editar", "Validar la cotización", "Emitir", "Cancelar la cotización"],
    ]);
    expect(await offered(at("APPROVED"), ...all)).toEqual([
      ...["Emitir", "Nueva revisión", "Cancelar la cotización"],
    ]);
    expect(await offered(at("SENT"), ...all)).toEqual([
      ...["El cliente aceptó", "El cliente rechazó", "Nueva revisión", "Cancelar la cotización"],
    ]);
    // Vencida: el cliente ya no responde; se revisa o se cancela.
    const late = at("SENT", { valid_until: PAST });
    expect(await offered(late, ...all)).toEqual(["Nueva revisión", "Cancelar la cotización"]);
    for (const status of ["ACCEPTED", "REJECTED", "EXPIRED", "CANCELLED"]) {
      expect(await offered(at(status), ...all)).toEqual([]);
    }
    expect(await offered(at("SENT"), "quotes.update", "quotes.approve")).toEqual([]);
    // Una solicitud de la IA: quien solo la envió ni la retira ni la cancela.
    const ai = at("PENDING_APPROVAL", { approval_asked_by_me: true });
    expect(await offered(ai, "quotes.update", "quotes.cancel")).toEqual([]);
    const mine = { ...ai, approval_requested_by_me: true };
    expect(await offered(mine, "quotes.update", "quotes.cancel")).toEqual([
      ...["Retirar la solicitud", "Cancelar la cotización"],
    ]);
    expect(await offered(at("PENDING_APPROVAL"), "quotes.approve", "quotes.cancel")).toEqual([
      ...["Aprobar", "Rechazar la solicitud", "Cancelar la cotización"],
    ]);
  });

  it.each([
    ["Emitir", "issue", "APPROVED", 409, "NOT_ISSUABLE", "ya no se puede emitir"],
    ["Emitir", "issue", "APPROVED", 422, "PRICE_NOT_ALLOWED", "precio puesto a mano ya no"],
    ["Emitir", "issue", "APPROVED", 429, "RATE_LIMITED", "Demasiados intentos"],
    ["El cliente aceptó", "accept", "SENT", 409, "QUOTE_EXPIRED", "La validez de la cotiz"],
    ["El cliente aceptó", "accept", "SENT", 409, "NOT_ANSWERABLE", "ya no espera la respuesta"],
    ["Nueva revisión", "revise", "APPROVED", 409, "NOT_REVISABLE", "Solo se revisa una"],
    ["Nueva revisión", "revise", "APPROVED", 409, "QUOTE_REFUSED", "no admite ese cambio"],
    ["Cancelar la cotización", "cancel", "SENT", 409, "QUOTE_TERMINAL", "ya terminó"],
    ["Cancelar la cotización", "cancel", "SENT", 409, "STALE_VERSION", "Otra persona cambió"],
  ])("«%s» (%s) en una %s: un %i %s se explica", async (verb, path, status, code, name, text) => {
    api({
      [`GET ${BASE}1/`]: { status: 200, body: at(status) },
      [`POST ${BASE}1/${path}/`]: { status: code, body: { code: name } },
    });
    show(...SELLER);
    fireEvent.click(await screen.findByRole("button", { name: verb }));
    const form = screen.getByRole("form");
    fireEvent.submit(form);
    expect(await within(form).findByText(new RegExp(text))).toHaveAttribute("role", "alert");
    expect(push).not.toHaveBeenCalled();
  });

  it("sin cambiar la validez no se envía; un 400 de campo se explica por su campo", async () => {
    const fields = { warranty: ["x"] };
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: at("APPROVED") },
      [`POST ${BASE}1/issue/`]: { status: 400, body: { code: "VALIDATION_ERROR", fields } },
    });
    show(...SELLER);
    const form = await open("Emitir", "Emitir COT-000001");
    confirm(form, "Emitir la cotización");
    expect(await within(form).findByText("La garantía admite hasta 500 caracteres.")).toBeVisible();
    expect(calls.map((c) => c.body)).toEqual([{ version: 3 }]); // la que tiene, sin textos
  });
});

describe("Cotizaciones: seguimientos de la aprobación (F5-09h)", () => {
  const waiting = (version = 3) =>
    at("PENDING_APPROVAL", { version, created_by_user_id: "u2" }) as unknown;

  it("decidir manda la versión vista al abrir aunque un 409 relea otra", async () => {
    let seen = 3;
    const calls = api({
      [`GET ${BASE}1/`]: () => ({ status: 200, body: waiting(seen) }),
      [`POST ${BASE}1/reject/`]: () => {
        seen = 4;
        return { status: 409, body: { code: "STALE_VERSION" } };
      },
    });
    show("quotes.approve");
    const form = await open("Rechazar la solicitud", "Rechazar la solicitud de COT-000001");
    const reason = within(form).getByLabelText("Motivo del rechazo");
    expect(reason).toHaveAttribute("maxLength", "200");
    expect(reason).toHaveAccessibleDescription(
      /Lo ve quien pidió .*no escribas costos ni márgenes/,
    );
    write(form, "Motivo del rechazo", "Margen");
    confirm(form, "Confirmar el rechazo");
    expect(await within(form).findByText(/Otra persona cambió/)).toBeVisible();
    await waitFor(() => expect(seen).toBe(4));
    confirm(form, "Confirmar el rechazo");
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls.map((c) => c.body)).toEqual(Array(2).fill({ version: 3, reason: "Margen" }));
  });

  it("mientras una acción espera, Aprobar no abre su formulario; al editar no hay acciones", async () => {
    const body = JSON.stringify({ ...(waiting() as object), approval_requested_by_me: true });
    const fetch = vi.fn(async (_: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(() => {}) : new Response(body),
    );
    vi.stubGlobal("fetch", fetch);
    show("quotes.approve", "quotes.update");
    fireEvent.click(await screen.findByRole("button", { name: "Retirar la solicitud" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2)); // la lectura y el retiro
    fireEvent.click(screen.getByRole("button", { name: "Aprobar" }));
    expect(screen.queryByRole("form")).toBeNull();
    cleanup();
    api({ [`GET ${BASE}1/`]: { status: 200, body: at("DRAFT") } });
    show(...SELLER);
    fireEvent.click(await screen.findByRole("button", { name: "Editar" }));
    expect(screen.getByRole("form", { name: "Editar el borrador" })).toBeVisible();
    expect(button("Emitir")).toBeNull();
    expect(button("Validar la cotización")).toBeNull();
  });
});
