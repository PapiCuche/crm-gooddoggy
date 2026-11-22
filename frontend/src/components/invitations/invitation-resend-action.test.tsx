import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Invitation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { InvitationsList } from "./invitations-list";

const LIST = "GET /api/v1/o/acme/invitations/";
const RESEND = "POST /api/v1/o/acme/invitations/fila-lima/resend/";
const NOW = Date.parse("2026-11-20T15:00:00Z"); // las 10:00 a. m. en Lima
const SOON = "Se envió hace menos de 15 minutos.";
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// Identificadores con prefijo propio: ninguno debe llegar al DOM.
const invitation = (name: string, extra: Partial<Invitation> = {}): Invitation =>
  ({
    id: `fila-${name}`,
    email: `${name}@cliente.pe`,
    role_ids: ["rol-ventas"],
    status: "PENDING",
    expires_at: "2026-11-25T15:30:00Z",
    invited_by: "cuenta-ana",
    created_at: "2026-11-18T15:30:00Z",
    sent_at: ago(20),
    send_count: 1,
    delivery: "SENT",
    ...extra,
  }) as Invitation;
const others = [
  invitation("tarde", { status: "EXPIRED", sent_at: ago(3000), delivery: "NOT_DELIVERED" }),
  invitation("fuera", { status: "REVOKED" }),
  invitation("dentro", { status: "ACCEPTED" }),
];
const list = (lima: Partial<Invitation> = {}) => ({
  status: 200,
  body: { results: [invitation("lima", lima), ...others], next: null },
});
const refusal = (status: number, code: string, wait?: string) => ({
  status,
  body: { code, message: "texto del servidor" },
  ...(wait ? { headers: { "Retry-After": wait } } : {}),
});
const screenOf = (codes = ["users.invite", "users.manage"]) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <InvitationsList />
    </TenantProvider>,
  );
const card = (name: string) => screen.getByText(`${name}@cliente.pe`).closest("li") as HTMLElement;
const resend = (name = "lima") =>
  screen.findByRole("button", { name: `Reenviar la invitación de ${name}@cliente.pe` });
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const dom = () => new XMLSerializer().serializeToString(document.body);
function hold(api: ReturnType<typeof mockApi>) {
  const answer = api.getMockImplementation()!;
  let release = () => {};
  api.mockImplementationOnce(async (...request) => {
    await new Promise<void>((resolve) => (release = resolve));
    return answer(...request);
  });
  return () => release();
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no lo pide antes
  vi.useFakeTimers({ toFake: ["Date"] }); // solo el reloj: los plazos se leen a una hora fija
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("InvitationResendAction", () => {
  it("solo se ofrece con los dos permisos, y en las filas pendientes o caducadas", async () => {
    for (const codes of [["users.invite"], ["users.manage"], ["roles.view"]]) {
      mockApi({ [LIST]: list() });
      const view = screenOf(codes);
      await screen.findByText("4 invitaciones en la lista");
      expect(screen.queryByRole("button", { name: /Reenviar/ })).toBeNull();
      view.unmount();
    }
    mockApi({ [LIST]: list() });
    screenOf();
    await screen.findByText("4 invitaciones en la lista");
    const offered = screen.getAllByRole("button", { name: /^Reenviar la invitación de/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Reenviar la invitación de lima@cliente.pe",
      "Reenviar la invitación de tarde@cliente.pe", // caducada: reenviarla la renueva
    ]);
    for (const button of offered) {
      expect(button).toHaveTextContent(/^Reenviar$/);
      expect(button).toHaveAttribute("aria-disabled", "false");
      expect(button).not.toHaveAttribute("aria-describedby");
    }
  });

  it("reenvía una sola vez, enseña la fila que respondió la API y lo anuncia", async () => {
    const saved = {
      ...invitation("lima", { delivery: "NOT_SENT", expires_at: "2026-11-27T15:00:00Z" }),
      link: "https://crm.example/invitacion#secreto-del-enlace", // nunca llega: aun así no se ve
    };
    const api = mockApi({ [LIST]: list(), [RESEND]: { status: 200, body: saved } });
    screenOf();
    const button = await resend();
    const release = hold(api);
    button.focus();
    fireEvent.click(button);
    fireEvent.click(button); // dos pulsaciones más mientras se envía: una sola petición
    fireEvent.keyDown(button, { key: "Enter", repeat: true });
    fireEvent.click(button);
    await tick();
    expect(button).toHaveTextContent("Reenviando…");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveFocus();
    release();
    const done = await screen.findByText(
      "Reenvío pedido para lima@cliente.pe. El enlace anterior ya no vale y en unos minutos le llegará un correo nuevo.",
    );
    expect(done).toHaveAttribute("role", "status");
    expect(calls(api, "POST")).toHaveLength(1);
    expect(calls(api, "GET")).toHaveLength(1); // la fila es la respuesta: no se vuelve a pedir
    expect(within(card("lima")).getByText("Correo: sin enviar")).toBeVisible();
    expect(within(card("lima")).getByText("Caduca el 27 nov. 2026")).toBeVisible();
    // Hasta que la tarea cuente el envío, la fila aún tiene el anterior: no se ofrece otra vez.
    expect(button).toHaveTextContent("Reenviar");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription(
      "Acabas de pedir el reenvío. Desde las 10:15 a. m. podrás volver a reenviarla.",
    );
    expect(button).toHaveFocus();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(button);
    await tick();
    expect(calls(api, "POST")).toHaveLength(1);
    for (const hidden of ["secreto", "crm.example", "fila-", "rol-", "cuenta-"])
      expect(dom()).not.toContain(hidden);
    fireEvent.click(await resend("tarde")); // otra fila: el anuncio anterior se retira
    expect(done).toHaveTextContent("");
  });

  it("con los 5 envíos usados lo dice antes de pulsar y no envía nada", async () => {
    const api = mockApi({ [LIST]: list({ send_count: 5 }) });
    screenOf();
    const button = await resend();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription(
      "Ya se envió 5 veces, el máximo. Para enviarla otra vez, revócala e invita de nuevo.",
    );
    expect(within(card("lima")).getByText("Envíos: 5 de 5")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull(); // lo dice la fila, no es un error
    fireEvent.click(button);
    await tick();
    expect(calls(api, "POST")).toHaveLength(0);
    expect(await resend("tarde")).toHaveAttribute("aria-disabled", "false"); // las demás, no
  });

  it.each([
    [0, 5, "true", "Desde las 10:10 a. m."], // dentro de los 15 minutos: dice desde qué hora
    // Atrasado una hora: el envío de hace 20 minutos le cae en el futuro. Para 15, no 55.
    [-60, 20, "true", "Desde las 9:15 a. m."],
    [60, 5, "false", null], // adelantado: se ofrece, y si es pronto lo dice el 429 de la API
  ])("con el reloj desviado %i minutos para como mucho 15", async (skew, sent, stops, when) => {
    vi.setSystemTime(NOW + skew * 60_000);
    const api = mockApi({ [LIST]: list({ sent_at: ago(sent) }) });
    screenOf();
    const button = await resend();
    expect(button).toHaveAttribute("aria-disabled", stops);
    if (when) expect(button).toHaveAccessibleDescription(`${SOON} ${when} podrás reenviarla.`);
    else expect(button).not.toHaveAttribute("aria-describedby");
    if (when) fireEvent.click(button);
    await tick();
    expect(calls(api, "POST")).toHaveLength(0);
  });

  it("con el reloj atrasado, la hora prometida no se mueve y se cumple", async () => {
    vi.useRealTimers(); // también los temporizadores, que avanzan con el tiempo real
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"], shouldAdvanceTime: true });
    vi.setSystemTime(NOW - 60 * 60_000); // las 9:00 a. m.; el envío, a las 9:40 de su reloj
    mockApi({ [LIST]: list({ sent_at: ago(20) }) });
    screenOf();
    const button = await resend();
    const promise = `${SOON} Desde las 9:15 a. m. podrás reenviarla.`;
    expect(button).toHaveAccessibleDescription(promise);
    await act(() => vi.advanceTimersByTimeAsync(10 * 60_000));
    expect(button).toHaveAccessibleDescription(promise); // no se mueve
    await act(() => vi.advanceTimersByTimeAsync(5 * 60_000 + 100));
    expect(button).toHaveAttribute("aria-disabled", "false"); // a las 9:15, no a las 9:55
    expect(button).not.toHaveAttribute("aria-describedby");
  });

  it("un envío nuevo que llega con el reloj atrasado también para 15 minutos, no más", async () => {
    vi.setSystemTime(NOW - 60 * 60_000);
    let rows = list({ sent_at: null, send_count: 0, delivery: "NOT_SENT" });
    mockApi({ [LIST]: () => rows });
    const view = screenOf();
    const button = await resend();
    rows = list({ sent_at: ago(20) }); // la tarea lo envió: la lista se vuelve a pedir
    await act(() => view.client.refetchQueries());
    const promise = `${SOON} Desde las 9:15 a. m. podrás reenviarla.`;
    await waitFor(() => expect(button).toHaveAccessibleDescription(promise));
  });

  it("una lectura en vuelo no pisa la fila reenviada", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [invitation("lima"), others[0]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [invitation("tacna")], next: null } },
      [RESEND]: { status: 200, body: invitation("lima", { delivery: "NOT_SENT" }) },
    });
    screenOf();
    const button = await resend();
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con la lista de antes del reenvío
    fireEvent.click(button);
    await waitFor(() => expect(within(card("lima")).getByText("Correo: sin enviar")).toBeVisible());
    release();
    await tick();
    expect(within(card("lima")).getByText("Correo: sin enviar")).toBeVisible();
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false")); // se canceló
  });

  it.each([
    [
      "INVITATION_SEND_TOO_SOON",
      "600",
      "Se envió hace menos de 15 minutos. Desde las 10:10 a. m. podrás reenviarla.",
    ],
    [
      "INVITATION_ADDRESS_LIMIT",
      "7200",
      "Ese correo ya recibió 3 invitaciones en 24 horas. Desde el 20 nov. 2026, 12:00 p. m. podrás reenviarla.",
    ],
  ])(
    "un 429 %s con Retry-After %s deja el botón parado hasta esa hora",
    async (code, wait, text) => {
      const api = mockApi({ [LIST]: list(), [RESEND]: refusal(429, code, wait) });
      screenOf();
      const button = await resend();
      button.focus();
      fireEvent.click(button);
      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveAccessibleDescription(text);
      expect(button).toHaveFocus();
      fireEvent.click(button);
      await tick();
      expect(calls(api, "POST")).toHaveLength(1);
      expect(dom()).not.toContain("texto del servidor");
    },
  );

  it.each([
    [
      409,
      "INVITATION_SEND_LIMIT",
      "Ya se envió 5 veces, el máximo. Para enviarla otra vez, revócala e invita de nuevo.",
      true,
    ],
    [
      429,
      "INVITATION_SEND_TOO_SOON",
      "Ese correo recibió una invitación hace menos de 15 minutos. Inténtalo más tarde.",
      false,
    ],
    [
      429,
      "INVITATION_ADDRESS_LIMIT",
      "Ese correo ya recibió 3 invitaciones en 24 horas. Inténtalo más tarde.",
      false,
    ],
    [
      409,
      "INVITATION_ROLE_GONE",
      "Un rol de esta invitación ya no existe. Revócala e invita otra vez con otros roles.",
      false,
    ],
    [409, "ALREADY_MEMBER", "Ese correo ya es miembro de la organización.", false],
    [403, "PERMISSION_DENIED", "No tienes permiso para reenviar esta invitación.", false],
    [409, "LAST_OWNER", "Debe quedar al menos un Owner activo en la organización.", false],
    [
      400,
      "VALIDATION_ERROR",
      "Algo salió mal de nuestro lado. Inténtalo de nuevo en unos minutos.",
      false,
    ],
    [
      500,
      "INTERNAL_ERROR",
      "Algo salió mal de nuestro lado. Inténtalo de nuevo en unos minutos.",
      false,
    ],
  ])("un %i %s se explica por su código", async (status, code, text, stops) => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const api = mockApi({ [LIST]: list(), [RESEND]: refusal(status, code) });
    screenOf();
    const button = await resend();
    button.focus();
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(button).toHaveAccessibleDescription(text);
    expect(button).toHaveAttribute("aria-disabled", String(stops));
    expect(button).toHaveFocus();
    expect(dom()).not.toContain("texto del servidor");
    await tick();
    fireEvent.click(button);
    await tick();
    expect(calls(api, "POST")).toHaveLength(stops ? 1 : 2); // si se puede, se reintenta
    if (!stops) await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(text));
  });

  it.each([
    [404, "NOT_FOUND", "La invitación de lima@cliente.pe ya no está disponible. Revisa la lista."],
    [
      409,
      "INVALID_TRANSITION",
      "La invitación de lima@cliente.pe ya no estaba pendiente. Revisa la lista.",
    ],
  ])("un %i %s lo explica la lista, que se vuelve a pedir", async (status, code, text) => {
    let rows = list();
    const api = mockApi({ [LIST]: () => rows, [RESEND]: refusal(status, code) });
    screenOf();
    const button = await resend();
    button.focus();
    rows = list({ status: "REVOKED" }); // otro la revocó entretanto
    fireEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(within(card("lima")).queryByRole("button")).toBeNull();
  });

  it("sin sesión (401) no enseña un error: sigue enviando hasta que lleve al login", async () => {
    const api = mockApi({ [LIST]: list(), [RESEND]: refusal(401, "NOT_AUTHENTICATED") });
    screenOf();
    const button = await resend();
    fireEvent.click(button);
    await waitFor(() => expect(calls(api, "POST")).toHaveLength(1));
    await tick();
    expect(button).toHaveTextContent("Reenviando…");
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(button);
    await tick();
    expect(calls(api, "POST")).toHaveLength(1);
  });
});
