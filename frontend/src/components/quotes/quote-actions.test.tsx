import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { QuoteDetail } from "./quote-detail";
import { api, BASE, detail, withTenant } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeEach(() => {
  document.cookie = "csrftoken=t";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const draft = detail({ status: "DRAFT", version: 2, created_by_user_id: "u1" });
const waiting = (mine: boolean, version = 3) =>
  detail({
    ...{ status: "PENDING_APPROVAL", version },
    ...{ approval_asked_by_me: mine, approval_requested_by_me: mine },
  });
const show = (...codes: string[]) =>
  withTenant(<QuoteDetail quoteId="1" />, "quotes.view", ...codes);
const button = (name: string) => screen.queryByRole("button", { name });
const heading = () => screen.getByRole("heading", { level: 1 });

describe("Cotizaciones: aprobación en el detalle (F5-09h)", () => {
  it("una vendedora envía su borrador; queda por aprobar con el aviso, y no lo aprueba ella", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: draft },
      [`POST ${BASE}1/request-approval/`]: { status: 200, body: waiting(true) },
    });
    show("quotes.update", "quotes.approve");
    const send = await screen.findByRole("button", { name: "Validar la cotización" });
    expect(screen.queryByText("Espera aprobación")).toBeNull();
    send.focus();
    fireEvent.click(send);
    expect(await screen.findByText(/Enviada a aprobación: espera/)).toBeVisible();
    const notice = screen.getByRole("complementary", { name: "Espera aprobación" });
    expect(notice).toHaveTextContent("La pediste tú: la decide otra persona");
    expect(button("Aprobar")).toBeNull(); // la pidió ella: lo dice la API
    expect(button("Rechazar la solicitud")).toBeNull();
    expect(button("Retirar la solicitud")).toBeVisible();
    expect(button("Editar")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(heading())); // su botón ya no está
    expect(calls).toEqual([
      { method: "POST", path: `${BASE}1/request-approval/`, body: { version: 2 } },
    ]);
  });

  it("dentro del límite queda aprobada; retirar devuelve el borrador", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: waiting(true) },
      [`POST ${BASE}1/withdraw-request/`]: { status: 200, body: draft },
      [`POST ${BASE}1/request-approval/`]: { status: 200, body: detail({ status: "APPROVED" }) },
    });
    show("quotes.update");
    fireEvent.click(await screen.findByRole("button", { name: "Retirar la solicitud" }));
    expect(await screen.findByText(/Solicitud retirada/)).toBeVisible();
    expect(screen.queryByText("Espera aprobación")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Validar la cotización" }));
    expect(await screen.findByText("Aprobada: el descuento cabe en tu límite.")).toBeVisible();
    expect(calls.map((c) => [c.path, c.body])).toEqual([
      [`${BASE}1/withdraw-request/`, { version: 3 }],
      [`${BASE}1/request-approval/`, { version: 2 }],
    ]);
  });

  it("un supervisor aprueba con comentario opcional; sin `quotes.update` no retira", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: waiting(false) },
      [`POST ${BASE}1/approve/`]: { status: 200, body: detail({ status: "APPROVED", version: 4 }) },
    });
    show("quotes.approve");
    expect(await screen.findByText(/que no la haya pedido/)).toBeVisible();
    expect(button("Retirar la solicitud")).toBeNull();
    expect(button("Validar la cotización")).toBeNull();
    const open = screen.getByRole("button", { name: "Aprobar" });
    open.focus();
    fireEvent.click(open);
    expect(open).toHaveAttribute("aria-expanded", "true");
    const form = screen.getByRole("form", { name: "Aprobar COT-000001" });
    expect(document.activeElement).toBe(within(form).getByLabelText("Comentario (opcional)"));
    fireEvent.click(within(form).getByRole("button", { name: "Confirmar la aprobación" }));
    expect(await screen.findByText("Cotización aprobada.")).toBeVisible();
    expect(screen.getByText("Aprobada")).toBeVisible();
    expect(screen.queryByRole("form")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(heading()));
    expect(calls.map((c) => c.body)).toEqual([{ version: 3 }]); // sin comentario, no viaja
  });

  it("rechazar pide su motivo; con él vuelve a borrador; Volver devuelve el foco", async () => {
    const calls = api({
      [`GET ${BASE}1/`]: { status: 200, body: waiting(false) },
      [`POST ${BASE}1/reject/`]: { status: 200, body: { ...draft, version: 4 } },
    });
    show("quotes.approve", "quotes.update");
    const open = await screen.findByRole("button", { name: "Rechazar la solicitud" });
    expect(button("Retirar la solicitud")).toBeVisible(); // quien aprueba también retira
    open.focus();
    fireEvent.click(open);
    let form = screen.getByRole("form", { name: "Rechazar la solicitud de COT-000001" });
    fireEvent.click(within(form).getByRole("button", { name: "Volver" }));
    await waitFor(() => expect(document.activeElement).toBe(open));
    fireEvent.click(open);
    form = screen.getByRole("form", { name: "Rechazar la solicitud de COT-000001" });
    const reason = within(form).getByLabelText("Motivo del rechazo");
    fireEvent.change(reason, { target: { value: "   " } });
    fireEvent.click(within(form).getByRole("button", { name: "Confirmar el rechazo" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe el motivo del rechazo.");
    expect(reason).toHaveAttribute("aria-invalid", "true");
    expect(calls).toEqual([]);
    fireEvent.change(reason, { target: { value: " Margen bajo " } });
    fireEvent.click(within(form).getByRole("button", { name: "Confirmar el rechazo" }));
    expect(await screen.findByText(/Solicitud rechazada/)).toBeVisible();
    expect(screen.getByText("Borrador")).toBeVisible();
    expect(calls.map((c) => c.body)).toEqual([{ version: 3, reason: "Margen bajo" }]);
  });

  it.each([
    [409, "SELF_APPROVAL_FORBIDDEN", "la decide otra persona"],
    [409, "BEYOND_LIMIT", "pasa de tu límite"],
    [409, "STALE_VERSION", "Otra persona cambió la cotización"],
    [422, "PRICE_NOT_ALLOWED", "precio puesto a mano ya no se admite"],
    [429, "RATE_LIMITED", "Demasiados intentos"],
    [403, "PERMISSION_DENIED", "No tienes permiso"],
  ])("un %i %s al aprobar se explica en el formulario", async (status, code, text) => {
    api({
      [`GET ${BASE}1/`]: { status: 200, body: waiting(false) },
      [`POST ${BASE}1/approve/`]: { status, body: { code } },
    });
    show("quotes.approve");
    fireEvent.click(await screen.findByRole("button", { name: "Aprobar" }));
    const form = screen.getByRole("form", { name: "Aprobar COT-000001" });
    fireEvent.change(within(form).getByLabelText("Comentario (opcional)"), {
      target: { value: "Ok" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Confirmar la aprobación" }));
    expect(await within(form).findByText(new RegExp(text))).toHaveAttribute("role", "alert");
  });

  it("un NOT_PENDING relee la cotización: el botón se va, el formulario y su error siguen", async () => {
    let decided = false;
    api({
      [`GET ${BASE}1/`]: () => ({
        status: 200,
        body: decided ? detail({ status: "APPROVED", version: 4 }) : waiting(false),
      }),
      [`POST ${BASE}1/reject/`]: () => {
        decided = true;
        return { status: 409, body: { code: "NOT_PENDING" } };
      },
    });
    show("quotes.approve");
    const open = await screen.findByRole("button", { name: "Rechazar la solicitud" });
    open.focus();
    fireEvent.click(open);
    const form = screen.getByRole("form", { name: "Rechazar la solicitud de COT-000001" });
    fireEvent.change(within(form).getByLabelText("Motivo del rechazo"), {
      target: { value: "No" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Confirmar el rechazo" }));
    expect(await within(form).findByText(/ya no espera una decisión/)).toBeVisible();
    await waitFor(() => expect(button("Rechazar la solicitud")).toBeNull());
    expect(screen.getByText("Aprobada")).toBeVisible();
    fireEvent.click(within(form).getByRole("button", { name: "Volver" }));
    expect(screen.queryByRole("form")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(heading()));
  });

  it("cada botón, solo con su estado y su permiso; `OWN` solo sobre lo propio", async () => {
    api({ [`GET ${BASE}1/`]: { status: 200, body: waiting(false) } });
    show("quotes.update", "quotes.discount"); // ni aprueba ni la pidió: no retira
    expect(await screen.findByText("Espera aprobación")).toBeVisible();
    expect(screen.queryAllByRole("button")).toEqual([]);
    cleanup();
    api({ [`GET ${BASE}1/`]: { status: 200, body: { ...draft, created_by_user_id: "u2" } } });
    show("quotes.update:OWN", "quotes.discount"); // el borrador es de otra persona
    expect(await screen.findByText("Borrador")).toBeVisible();
    expect(screen.queryAllByRole("button")).toEqual([]);
    cleanup();
    api({ [`GET ${BASE}1/`]: { status: 200, body: draft } });
    show("quotes.update:OWN", "quotes.approve"); // suyo; aprobar no aplica a un borrador
    expect(await screen.findByRole("button", { name: "Validar la cotización" })).toBeVisible();
    expect(button("Editar")).toBeVisible();
    expect(button("Aprobar")).toBeNull();
    cleanup();
    for (const status of ["APPROVED", "SENT", "CANCELLED"]) {
      api({ [`GET ${BASE}1/`]: { status: 200, body: detail({ status }) } });
      show("quotes.update", "quotes.approve", "quotes.discount");
      expect(await screen.findByRole("heading", { level: 1 })).toBeVisible();
      expect(screen.queryAllByRole("button")).toEqual([]);
      cleanup();
    }
  });
});
