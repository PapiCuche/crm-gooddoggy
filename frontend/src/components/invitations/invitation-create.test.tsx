import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Invitation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { InvitationsList } from "./invitations-list";

const LIST = "GET /api/v1/o/acme/invitations/";
const ROLES = "GET /api/v1/o/acme/roles/?limit=200";
const CREATE = "POST /api/v1/o/acme/invitations/";
const ALL = ["users.invite", "users.manage", "roles.view"];
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// Identificadores con prefijo propio y distintos del código: ninguno debe llegar al DOM.
const role = (id: string, name: string) => ({ id: `rol-${id}`, name, code: `c-${id}` });
const directory = (...rows: ReturnType<typeof role>[]) => ({
  status: 200,
  body: { results: rows, next: null },
});
const three = directory(role("1", "Vendedor"), role("2", "Caja"), role("3", "Owner"));
const invitation = (email: string, role_ids: string[]): Invitation =>
  ({
    id: "fila-nueva",
    email,
    role_ids,
    status: "PENDING",
    expires_at: "2026-10-14T15:30:00Z",
    invited_by: "cuenta-ana",
    created_at: "2026-10-07T15:30:00Z",
  }) as Invitation;
const list = (...rows: Invitation[]) => ({ status: 200, body: { results: rows, next: null } });
const refusal = (status: number, code: string, fields?: Record<string, unknown>) => ({
  status,
  body: { code, ...(fields ? { fields } : {}) },
});
const screenOf = (codes = ALL) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <InvitationsList />
    </TenantProvider>,
  );
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = () => screen.getByRole("button", { name: "Invitar" });
const email = (form: HTMLElement) => within(form).getByLabelText("Correo de la persona");
const box = (form: HTMLElement, name: string) => within(form).getByRole("checkbox", { name });
async function opened() {
  await screen.findByText(/en la lista$/); // la lista ya respondió, con filas o sin ellas
  fireEvent.click(await screen.findByRole("button", { name: "Invitar" }));
  const form = screen.getByRole("form", { name: "Nueva invitación" });
  await within(form).findAllByRole("checkbox"); // y el directorio de roles
  return form;
}
function fill(form: HTMLElement, address: string, ...roles: string[]) {
  fireEvent.input(email(form), { target: { value: address } });
  for (const name of roles) fireEvent.click(box(form, name));
}
// Como una pulsación real: el botón recibe el foco antes del clic.
function send(form: HTMLElement) {
  const button = within(form).getByRole("button", { name: /^Invita/ });
  button.focus();
  fireEvent.click(button);
}
const alerts = (form: HTMLElement) =>
  within(form)
    .queryAllByRole("alert")
    .map((alert) => alert.textContent);

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // el espía de `console.error`, también si el test falló
});

describe("InvitationCreate", () => {
  it("solo se ofrece con los tres permisos, y abrir o cancelar no envía nada", async () => {
    for (const missing of ALL) {
      mockApi({ [LIST]: list(), [ROLES]: three });
      const view = screenOf(ALL.filter((code) => code !== missing));
      await screen.findByText(/en la lista$/);
      expect(screen.queryByRole("button", { name: "Invitar" })).toBeNull();
      view.unmount();
    }
    const api = mockApi({ [LIST]: list(), [ROLES]: three });
    screenOf();
    const form = await opened(); // también sin ninguna invitación
    expect(email(form)).toHaveFocus();
    expect(within(form).getByRole("heading", { name: "Nueva invitación" })).toBeVisible();
    expect(within(form).getByRole("group", { name: "Roles que tendrá al aceptar" })).toBeVisible();
    const boxes = within(form).getAllByRole("checkbox");
    expect(boxes.map((one) => one.closest("label")?.textContent)).toEqual([
      "Vendedor", // en el orden del directorio
      "Caja",
      "Owner",
    ]);
    for (const one of boxes) expect(one).not.toBeChecked();
    expect(within(form).getByText(/el correo con el enlace todavía no se envía/)).toBeVisible();
    expect(new XMLSerializer().serializeToString(document.body)).not.toMatch(/rol-|c-\d/);
    fireEvent.click(box(form, "Caja"));
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).toBeNull();
    await waitFor(() => expect(trigger()).toHaveFocus());
    fireEvent.click(trigger()); // otra vez: sin lo elegido antes
    expect(box(screen.getByRole("form"), "Caja")).not.toBeChecked();
    expect(calls(api, "POST")).toHaveLength(0);
  });

  it("invita con un solo envío, lo anuncia con el correo que guardó la API y vuelve a pedir la lista", async () => {
    let rows: Invitation[] = [];
    const made = invitation("nueva@cliente.pe", ["rol-1", "rol-3"]);
    const api = mockApi({
      [LIST]: () => list(...rows),
      [ROLES]: three,
      [CREATE]: () => ({ status: 201, body: made }),
    });
    screenOf();
    const form = await opened();
    fill(form, "  Nueva@Cliente.pe ", "Owner", "Caja", "Vendedor");
    fireEvent.click(box(form, "Caja")); // se arrepiente de uno
    expect(box(form, "Owner")).toBeChecked();
    rows = [made];
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: una sola invitación
    send(form);
    fireEvent.click(box(form, "Caja")); // y lo elegido ya no cambia
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(calls(api, "POST")).toHaveLength(1);
    // Sin espacios exteriores; la forma del correo la decide la API. Los roles, por
    // identificador y en el orden en que se eligieron.
    expect(body(calls(api, "POST")[0])).toEqual({
      email: "Nueva@Cliente.pe",
      role_ids: ["rol-3", "rol-1"],
    });
    const done = screen.getByText(
      "Invitación a nueva@cliente.pe registrada. Todavía no se envía ningún correo.",
    );
    expect(done).toHaveAttribute("role", "status");
    expect(done).not.toHaveClass("sr-only");
    await waitFor(() => expect(trigger()).toHaveFocus());
    expect(await screen.findByText("nueva@cliente.pe", { selector: ".font-medium" })).toBeVisible();
    expect(calls(api, "GET").filter(([url]) => !String(url).includes("/roles/"))).toHaveLength(2);
    fireEvent.click(trigger()); // otra: el anuncio anterior se retira y el formulario, vacío
    expect(done).toHaveTextContent("");
    expect(email(screen.getByRole("form"))).toHaveValue("");
    expect(box(screen.getByRole("form"), "Owner")).not.toBeChecked();
  });

  it("sin correo, sin rol o con más de veinte no envía nada, y lo dice donde toca", async () => {
    const many = Array.from({ length: 21 }, (_, n) => role(`m${n}`, `Rol ${n}`));
    const api = mockApi({ [LIST]: list(), [ROLES]: directory(...many) });
    screenOf();
    const form = await opened();
    send(form);
    expect(alerts(form)).toEqual(["Escribe el correo de la persona."]); // primero, el campo
    expect(email(form)).toHaveFocus();
    fill(form, "x@cliente.pe");
    send(form);
    expect(alerts(form)).toEqual(["Elige al menos un rol."]);
    expect(box(form, "Rol 0")).toHaveFocus(); // en la primera casilla
    for (const one of within(form).getAllByRole("checkbox")) fireEvent.click(one);
    expect(alerts(form)).toEqual([]); // elegir retira el aviso
    send(form);
    expect(alerts(form)).toEqual(["Elige como mucho 20 roles."]);
    expect(calls(api, "POST")).toHaveLength(0);
    fireEvent.click(box(form, "Rol 20"));
    send(form);
    await waitFor(() => expect(calls(api, "POST")).toHaveLength(1)); // con veinte, sí
    expect(body(calls(api, "POST")[0]).role_ids).toHaveLength(20);
  });

  it.each([
    [
      refusal(400, "VALIDATION_ERROR", { email: [{ code: "invalid" }] }),
      "email",
      "Ese correo no sirve. Escribe una sola dirección, sin tildes ni espacios.",
    ],
    [refusal(409, "ALREADY_MEMBER"), "email", "Ese correo ya es miembro de la organización."],
    [refusal(409, "INVITATION_PENDING"), "email", "Ese correo ya tiene una invitación pendiente."],
    [
      refusal(400, "VALIDATION_ERROR", { role_ids: [{ code: "invalid" }] }),
      "form",
      "Alguno de los roles elegidos ya no existe en la organización. Cierra el formulario y vuelve a abrirlo.",
    ],
    [
      refusal(409, "INVITATION_LIMIT"),
      "form",
      "La organización ya tiene 50 invitaciones pendientes. No se pueden crear más hasta que alguna se acepte o se revoque.",
    ],
    [
      refusal(429, "RATE_LIMITED"),
      "form",
      "La organización ya creó 100 invitaciones en las últimas 24 horas. Inténtalo más tarde.",
    ],
    [refusal(403, "PERMISSION_DENIED"), "form", "No tienes permiso para invitar con esos roles."],
    [
      refusal(409, "LAST_OWNER"),
      "form",
      "Debe quedar al menos un Owner activo en la organización.",
    ],
    [
      refusal(500, "INTERNAL_ERROR"),
      "form",
      "Algo salió mal de nuestro lado. Inténtalo de nuevo en unos minutos.",
    ],
    [
      refusal(400, "VALIDATION_ERROR", { otro: [{ code: "invalid" }] }),
      "form",
      "Algo salió mal de nuestro lado. Inténtalo de nuevo en unos minutos.",
    ],
  ])("explica %j junto al %s", async (answer, where, text) => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const api = mockApi({ [LIST]: list(), [ROLES]: three, [CREATE]: answer });
    screenOf();
    const form = await opened();
    fill(form, "x@cliente.pe", "Caja");
    send(form);
    await waitFor(() => expect(alerts(form)).toEqual([text]));
    expect(screen.getByRole("form")).toBe(form); // sigue abierto, con lo escrito
    expect(email(form)).toHaveValue("x@cliente.pe");
    expect(box(form, "Caja")).toBeChecked();
    if (where === "email") {
      expect(email(form)).toHaveAttribute("aria-invalid", "true");
      await waitFor(() => expect(email(form)).toHaveFocus());
    } else expect(email(form)).not.toHaveAttribute("aria-invalid");
    await tick();
    fireEvent.click(box(form, "Vendedor")); // cambiar lo elegido retira el error anterior
    await waitFor(() => expect(alerts(form)).toEqual([]));
    send(form);
    await waitFor(() => expect(calls(api, "POST")).toHaveLength(2)); // y se puede reintentar
    expect(body(calls(api, "POST")[1]).role_ids).toEqual(["rol-2", "rol-1"]);
  });

  it("si el directorio de roles no llega, lo dice y no deja invitar a ciegas", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const api = mockApi({ [LIST]: list(), [ROLES]: { status: 500 } });
    const answer = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementation(async (...request) => {
      if (String(request[0]).includes("/roles/")) await new Promise<void>((go) => (release = go));
      return answer(...request);
    });
    screenOf();
    await screen.findByText(/en la lista$/);
    fireEvent.click(trigger()); // antes de que el directorio responda
    const form = screen.getByRole("form");
    expect(within(form).getByRole("status")).toHaveTextContent("Cargando los roles…");
    expect(alerts(form)).toEqual([]);
    release();
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // se reintenta una vez
    release();
    await waitFor(() =>
      expect(alerts(form)).toEqual([
        "No pudimos cargar los roles. Cierra el formulario e inténtalo de nuevo.",
      ]),
    );
    expect(within(form).queryAllByRole("checkbox")).toEqual([]);
    fill(form, "x@cliente.pe");
    send(form);
    expect(alerts(form)).toContain("Elige al menos un rol.");
    expect(calls(api, "POST")).toHaveLength(0);
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre el formulario ni reenvía", async () => {
    const api = mockApi({
      [LIST]: list(),
      [ROLES]: three,
      [CREATE]: { status: 201, body: invitation("x@cliente.pe", ["rol-2"]) },
    });
    screenOf();
    const form = await opened();
    fill(form, "x@cliente.pe", "Caja");
    send(form);
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    const held = fireEvent.keyDown(trigger(), { key: "Enter", repeat: true });
    expect(held).toBe(false); // la pulsación repetida se descarta
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.getByText(/^Invitación a x@cliente.pe registrada/)).toBeVisible();
    expect(calls(api, "POST")).toHaveLength(1);
  });
});
