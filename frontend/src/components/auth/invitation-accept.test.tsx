import { act, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { InvitationAccept } from "./invitation-accept";

const PREVIEW = "POST /api/v1/invitations/preview/";
const ACCEPT = "POST /api/v1/invitations/accept/";
const LINK = "a".repeat(20) + "-_Z" + "9".repeat(20); // la forma de un secreto: 43 caracteres
const PASSWORD = "  una frase larga 2026 ";
const found = (needs: boolean) => ({
  status: 200,
  body: { organization_name: "Acme", email: "ana@acme.pe", needs_account: needs },
});
const refused = { status: 400, body: { code: "INVALID_INVITATION_LINK" } };
const exists = { status: 409, body: { code: "ACCOUNT_EXISTS" } };
const joined = { status: 200, body: { organization_name: "Acme", account_created: true } };
const INVALID = "Esta invitación no vale.";
const dom = () => new XMLSerializer().serializeToString(document.documentElement);
const bodies = (api: ReturnType<typeof mockApi>, path: string) =>
  api.mock.calls
    .filter(([url]) => String(url).endsWith(path))
    .map(([, init]) => JSON.parse(String(init?.body)) as unknown);
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

function open(fragment: string) {
  window.history.replaceState({ deNext: true }, "", `/invitacion?x=1${fragment}`);
  return renderApp(<InvitationAccept />);
}
async function register(first = "Ana", confirm = PASSWORD) {
  await screen.findByText(/Ese correo todavía no tiene cuenta/);
  type("Nombre", first);
  type("Apellidos (opcional)", " Paz ");
  type("Contraseña", PASSWORD);
  type("Repite la contraseña", confirm);
  fireEvent.click(screen.getByRole("button", { name: "Crear cuenta y aceptar" }));
}

// La consola nunca ve el secreto; el navegador no guarda nada.
let logged: ReturnType<typeof vi.spyOn>[] = [];
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  logged = (["log", "info", "warn", "error", "debug"] as const).map((name) =>
    vi.spyOn(console, name),
  );
});
afterEach(() => {
  for (const spy of logged) expect(JSON.stringify(spy.mock.calls)).not.toContain(LINK);
  expect([localStorage.length, sessionStorage.length]).toEqual([0, 0]);
  // Ninguna petición lleva el secreto en la URL, en ningún recorrido.
  const calls = vi.isMockFunction(fetch) ? vi.mocked(fetch).mock.calls : [];
  expect(calls.filter(([url]) => String(url).includes(LINK))).toEqual([]);
  vi.restoreAllMocks();
  configure({ reactStrictMode: false });
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("InvitationAccept", () => {
  it("saca el secreto de la barra al abrir y pide la vista previa una vez, en el cuerpo", async () => {
    configure({ reactStrictMode: true }); // los efectos dobles no gastan otro intento
    const api = mockApi({ [PREVIEW]: found(true) });
    const entries = window.history.length;
    const strip = vi.spyOn(window.history, "replaceState");
    open(`#${LINK}`);
    expect(window.location.href).toBe(`${window.location.origin}/invitacion?x=1`);
    expect(window.location.hash).toBe("");
    expect(window.history.length).toBe(entries);
    await Promise.resolve();
    expect(strip.mock.calls.slice(1)).toEqual([
      [{ deNext: true }, "", "/invitacion?x=1"],
      [null, "", "/invitacion?x=1"],
    ]);
    expect(await screen.findByText(/Acme te invitó a su espacio/)).toHaveTextContent("ana@acme.pe");
    expect(bodies(api, "/preview/")).toEqual([{ token: LINK }]);
    const [url, init] = api.mock.calls[0] ?? [];
    expect(String(url)).not.toContain(LINK);
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("token-de-prueba");
    expect(screen.getByLabelText("Nombre")).toHaveFocus();
    expect(dom()).not.toContain(LINK);
    expect(bodies(api, "/accept/")).toEqual([]); // abrir no acepta
  });

  it("hasta sacar el secreto de la barra solo dice que abre la invitación", () => {
    mockApi({});
    window.history.replaceState(null, "", `/invitacion#${LINK}`);
    const strip = vi.spyOn(window.history, "replaceState");
    strip.mockImplementationOnce(() =>
      expect(document.body.textContent).toBe("Abriendo la invitación…"),
    );
    renderApp(<InvitationAccept />);
    expect(strip).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["", "Abre el enlace del correo."],
    ["#", "Abre el enlace del correo."],
    ["#corto", INVALID],
    [`#${LINK}x`, INVALID],
  ])("con el fragmento %j no pregunta a la API", (hash, title) => {
    const api = mockApi({});
    open(hash);
    expect(screen.getByRole("heading", { level: 2, name: title })).toHaveFocus();
    expect(api).not.toHaveBeenCalled();
  });

  it("cualquier enlace que no vale, en la vista previa o al aceptar, ve la misma pantalla", async () => {
    mockApi({ [PREVIEW]: refused });
    const early = open(`#${LINK}`);
    await screen.findByRole("heading", { name: INVALID });
    const said = early.container.textContent;
    early.unmount();
    mockApi({ [PREVIEW]: found(true), [ACCEPT]: refused });
    const late = open(`#${LINK}`);
    await register();
    await screen.findByRole("heading", { name: INVALID });
    expect(late.container.textContent).toBe(said);
    expect(said).toContain("Pide a quien te invitó que te envíe otra.");
  });

  it("sin cuenta: la crea con lo escrito, acepta y manda a entrar, sin iniciar sesión", async () => {
    const api = mockApi({ [PREVIEW]: found(true), [ACCEPT]: joined });
    const view = open(`#${LINK}`);
    await register();
    const title = await screen.findByRole("heading", { name: "Ya eres parte de Acme." });
    await waitFor(() => expect(title).toHaveFocus());
    expect(screen.getByText(/Entra con ana@acme.pe y la contraseña que elegiste/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Entrar" })).toHaveAttribute("href", "/login");
    expect(bodies(api, "/accept/")).toEqual([
      { token: LINK, first_name: "Ana", last_name: "Paz", password: PASSWORD },
    ]);
    expect(api.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/v1/invitations/preview/",
      "/api/v1/invitations/accept/",
    ]);
    expect(dom()).not.toContain(PASSWORD.trim());
    await waitFor(() => expect(view.client.getMutationCache().getAll()).toHaveLength(0));
  });

  it("sin cuenta: nombre, contraseña y confirmación se revisan antes de enviar", async () => {
    const api = mockApi({ [PREVIEW]: found(true) });
    open(`#${LINK}`);
    await register(" ", "otra");
    expect(screen.getByLabelText("Nombre")).toHaveFocus();
    expect(screen.getByText("Escribe tu nombre.")).toBeVisible();
    expect(screen.getByText("Las dos contraseñas no coinciden.")).toBeVisible();
    expect(bodies(api, "/accept/")).toEqual([]);
  });

  it("una contraseña que la API no acepta se dice con las reglas, nunca con su texto", async () => {
    const weak = { code: "invalid", message: "texto de la API" };
    mockApi({
      [PREVIEW]: found(true),
      [ACCEPT]: { status: 400, body: { code: "VALIDATION_ERROR", fields: { password: [weak] } } },
    });
    open(`#${LINK}`);
    expect(await screen.findByText(/Al menos 12 caracteres/)).toBeVisible(); // las reglas, antes
    await register();
    const field = screen.getByLabelText("Contraseña");
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveAccessibleDescription(
      "Esa contraseña no sirve. Revisa las reglas y elige otra.",
    );
    expect(dom()).not.toContain("texto de la API");
  });

  it.each([
    ["con cuenta al abrir", { [PREVIEW]: found(false) }],
    ["con cuenta al aceptar (409)", { [PREVIEW]: found(true), [ACCEPT]: exists }],
  ])("%s: lo dice sin ofrecer aceptar, que llega con #978", async (_, routes) => {
    const api = mockApi(routes);
    open(`#${LINK}`);
    if (ACCEPT in routes) await register();
    const title = await screen.findByRole("heading", { name: "Ese correo ya tiene una cuenta." });
    await waitFor(() => expect(title).toHaveFocus());
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(bodies(api, "/accept/")).toHaveLength(ACCEPT in routes ? 1 : 0);
  });

  it("demasiados intentos: dice cuánto esperar y deja volver a preguntar", async () => {
    let reply: { status: number; body?: unknown; headers?: Record<string, string> } = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "600" },
    };
    const api = mockApi({ [PREVIEW]: () => reply });
    open(`#${LINK}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Podrás volver a probar en 10 minutos.",
    );
    reply = found(true);
    fireEvent.click(screen.getByRole("button", { name: "Volver a intentar" }));
    expect(await screen.findByLabelText("Nombre")).toBeVisible();
    expect(bodies(api, "/preview/")).toEqual([{ token: LINK }, { token: LINK }]);
  });

  it("tras un intento sin respuesta, un enlace que ya no vale avisa de que pudo aceptarse", async () => {
    const api = mockApi({ [ACCEPT]: refused });
    api.mockImplementationOnce(async () => new Response(JSON.stringify(found(true).body)));
    api.mockImplementationOnce(async () => Promise.reject(new TypeError("sin red")));
    open(`#${LINK}`);
    await register();
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay conexión con el servidor.");
    fireEvent.click(screen.getByRole("button", { name: "Crear cuenta y aceptar" }));
    expect(await screen.findByText(/puede que ya estés dentro/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Entrar" })).toHaveAttribute("href", "/login");
  });

  it("otro enlace abierto en la misma pestaña se lee, se quita y se pregunta", async () => {
    const api = mockApi({ [PREVIEW]: found(true) });
    open(`#${LINK}`);
    await screen.findByLabelText("Nombre");
    const other = "b".repeat(43);
    await act(async () => {
      window.location.hash = other; // el navegador avisa con `hashchange`
    });
    await waitFor(() => expect(bodies(api, "/preview/")).toHaveLength(2));
    expect(bodies(api, "/preview/")[1]).toEqual({ token: other });
    expect(window.location.hash).toBe("");
  });
});
