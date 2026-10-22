import { focusManager, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { AccountSecurity } from "./account-security";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const STATUS = "GET /api/v1/auth/mfa/";
const START = "POST /api/v1/auth/mfa/totp/";
const CONFIRM = "POST /api/v1/auth/mfa/totp/confirm/";
const off = { enabled: false, enabled_at: null, enrollment_expires_in: null, backup_codes_left: 0 };
const on = {
  enabled: true,
  enabled_at: "2026-10-21T15:00:00Z",
  enrollment_expires_in: null,
  backup_codes_left: 10,
};
const SECRET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const URI = `otpauth://totp/Good%20Doggy%20CRM:ana%40acme.pe?secret=${SECRET}&issuer=Good%20Doggy%20CRM`;
const CODES = Array.from({ length: 10 }, (_, n) => `AAAA-BBBB-CCCC-${String(n).padStart(4, "0")}`);
const started = { status: 201, body: { secret: SECRET, uri: URI, expires_in: 600 } };
const confirmed = { status: 200, body: { backup_codes: CODES } };
const invalid = (field: string) => ({
  status: 400,
  body: { code: "VALIDATION_ERROR", fields: { [field]: [{ code: "invalid", message: "x" }] } },
});
type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

const calls = (api: ReturnType<typeof mockApi>, path: string) =>
  api.mock.calls.filter(([url, init]) => String(url).endsWith(path) && init?.method === "POST");

function send(label: string, value: string, button: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: button }));
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  router.replace.mockReset();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("AccountSecurity", () => {
  it("sin segundo factor lo ofrece, y el alta lleva de la contraseña a los códigos de respaldo", async () => {
    let status: Reply = { status: 200, body: off };
    let start: Reply = invalid("password");
    let confirm: Reply = invalid("code");
    const api = mockApi({ [STATUS]: () => status, [START]: () => start, [CONFIRM]: () => confirm });
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const { client } = renderApp(<AccountSecurity />);
    expect(await screen.findByText(/te pediremos un código/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Activar la verificación en dos pasos" }));
    expect(screen.getByLabelText("Contraseña actual")).toHaveFocus();
    expect(screen.queryByRole("link")).toBeNull(); // durante el alta no se sale de la página
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    expect(screen.getByLabelText("Contraseña actual")).toHaveAccessibleDescription(
      "Escribe tu contraseña.",
    );
    expect(calls(api, "/mfa/totp/")).toHaveLength(0); // vacío: no se envía
    send("Contraseña actual", " mala ", "Continuar");
    await waitFor(() =>
      expect(screen.getByLabelText("Contraseña actual")).toHaveAccessibleDescription(
        "La contraseña no es correcta.",
      ),
    );
    expect(JSON.parse(String(calls(api, "/mfa/totp/")[0]?.[1]?.body))).toEqual({
      password: " mala ",
    });
    start = started;
    send("Contraseña actual", "clave", "Continuar");
    fireEvent.click(screen.getByRole("button", { name: "Comprobando…" })); // una sola petición
    expect(await screen.findByText("ABCD EFGH IJKL MNOP QRST UVWX YZ23 4567")).toBeInTheDocument();
    expect(calls(api, "/mfa/totp/")).toHaveLength(2); // la que no valía y esta
    expect(screen.getByText(/Abre tu aplicación de autenticación/)).toHaveFocus();
    expect(
      screen.getByRole("link", { name: "Abrir en la aplicación de autenticación" }),
    ).toHaveAttribute("href", URI);
    fireEvent.click(screen.getByRole("button", { name: "Copiar la clave" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SECRET));
    expect(await screen.findByRole("button", { name: "Copiado" })).toBeInTheDocument();
    send("Código", "000000", "Activar");
    await waitFor(() =>
      expect(screen.getByLabelText("Código")).toHaveAccessibleDescription(
        "Ese código no vale. Prueba con el que muestra ahora la aplicación.",
      ),
    );
    expect(screen.getByLabelText("Código")).toHaveFocus();
    confirm = confirmed;
    fireEvent.change(screen.getByLabelText("Código"), { target: { value: " 123 456 " } });
    const activate = screen.getByRole("button", { name: "Activar" });
    fireEvent.click(activate);
    fireEvent.click(activate); // dos pulsaciones, una petición
    expect(
      await screen.findByRole("heading", { name: "Guarda tus códigos de respaldo" }),
    ).toHaveFocus();
    expect(calls(api, "/mfa/totp/confirm/")).toHaveLength(2); // el que no valía y este
    expect(JSON.parse(String(calls(api, "/mfa/totp/confirm/")[1]?.[1]?.body))).toEqual({
      code: "123456",
    });
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(CODES);
    const finish = screen.getByRole("button", { name: "Terminar" });
    expect(finish).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(finish); // sin marcar que los guardó, no termina
    expect(
      screen.getByRole("heading", { name: "Guarda tus códigos de respaldo" }),
    ).toBeInTheDocument();
    status = { status: 200, body: on };
    fireEvent.click(screen.getByRole("checkbox", { name: /He guardado estos códigos/ }));
    fireEvent.click(finish);
    expect(screen.getByText("Cargando…")).toBeInTheDocument(); // no ofrece activarlo mientras
    expect(await screen.findByText(/Activada desde el 21 de octubre de 2026/)).toBeInTheDocument();
    expect(screen.getByText("Te quedan 10 códigos de respaldo sin usar.")).toBeInTheDocument();
    expect(screen.queryByText(CODES[0]!)).toBeNull(); // los códigos ya no están en la página
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0)); // ni en memoria
    expect(location.search).toBe("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("con segundo factor dice desde cuándo y avisa si quedan pocos códigos", async () => {
    mockApi({ [STATUS]: { status: 200, body: { ...on, backup_codes_left: 2 } } });
    renderApp(<AccountSecurity />);
    const left = await screen.findByText(/Te quedan 2 códigos de respaldo sin usar./);
    expect(left).toHaveTextContent("Si los gastas y pierdes el teléfono, no podrás entrar.");
    expect(left).toHaveClass("text-danger");
    expect(screen.queryByRole("button", { name: /Activar/ })).toBeNull();
    expect(screen.getByRole("link", { name: "Volver a tus organizaciones" })).toHaveAttribute(
      "href",
      "/o",
    );
  });

  it("un alta caducada vuelve a la contraseña con su aviso", async () => {
    mockApi({
      [STATUS]: { status: 200, body: off },
      [START]: started,
      [CONFIRM]: { status: 409, body: { code: "MFA_ENROLLMENT_NOT_STARTED" } },
    });
    renderApp(<AccountSecurity />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Activar la verificación en dos pasos" }),
    );
    send("Contraseña actual", "clave", "Continuar");
    await screen.findByLabelText("Código");
    send("Código", "123456", "Activar");
    expect(await screen.findByRole("alert")).toHaveTextContent("El alta caducó o dejó de valer.");
    expect(screen.getByLabelText("Contraseña actual")).toHaveValue("");
  });

  it("si ya estaba activado, enseña el estado; los límites dicen su espera", async () => {
    let status: Reply = { status: 200, body: off };
    let start: Reply = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "86400" },
    };
    mockApi({ [STATUS]: () => status, [START]: () => start });
    renderApp(<AccountSecurity />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Activar la verificación en dos pasos" }),
    );
    send("Contraseña actual", "clave", "Continuar");
    expect(await screen.findByRole("alert")).toHaveTextContent("en 24 horas");
    status = { status: 200, body: on };
    start = { status: 409, body: { code: "MFA_ALREADY_ENABLED" } };
    send("Contraseña actual", "clave", "Continuar");
    expect(await screen.findByText(/Activada desde el/)).toBeInTheDocument();
  });

  it("si al confirmar ya estaba activado, enseña el estado", async () => {
    let status: Reply = { status: 200, body: off };
    const already = { status: 409, body: { code: "MFA_ALREADY_ENABLED" } };
    mockApi({ [STATUS]: () => status, [START]: started, [CONFIRM]: already });
    renderApp(<AccountSecurity />);
    fireEvent.click(await screen.findByRole("button", { name: /Activar la verificación/ }));
    send("Contraseña actual", "clave", "Continuar");
    await screen.findByLabelText("Código");
    status = { status: 200, body: on };
    send("Código", "123456", "Activar");
    expect(await screen.findByText(/Activada desde el/)).toBeInTheDocument();
  });

  it("con el alta en curso no vuelve a pedir el estado al volver la ventana o la red", async () => {
    const api = mockApi({ [STATUS]: { status: 200, body: off } });
    renderApp(<AccountSecurity />);
    fireEvent.click(await screen.findByRole("button", { name: /Activar la verificación/ }));
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(Date.now() + 60_000); // el estado ya está viejo (`staleTime`)
    for (const on of [false, true])
      act(() => (focusManager.setFocused(on), onlineManager.setOnline(on)));
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(api.mock.calls.filter(([url]) => String(url).endsWith("/auth/mfa/"))).toHaveLength(1);
  });

  it("se puede cancelar, y deja de esperar a un servidor que no contesta", async () => {
    const api = mockApi({ [STATUS]: { status: 200, body: off } });
    renderApp(<AccountSecurity />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Activar la verificación en dos pasos" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(
      screen.getByRole("button", { name: "Activar la verificación en dos pasos" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Activar la verificación en dos pasos" }));
    const usual = api.getMockImplementation()!;
    api.mockImplementation((input, init) =>
      String(input).endsWith("/mfa/totp/") ? new Promise<Response>(() => {}) : usual(input, init),
    );
    vi.useFakeTimers();
    send("Contraseña actual", "clave", "Continuar");
    await act(() => vi.advanceTimersByTimeAsync(30_500));
    expect(screen.getByRole("alert")).toHaveTextContent("El servidor tarda demasiado");
    expect(screen.getByRole("button", { name: "Continuar" })).toBeInTheDocument();
  });

  it("si no puede leer el estado, lo dice y deja reintentar", async () => {
    let status: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    mockApi({ [STATUS]: () => status });
    renderApp(<AccountSecurity />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado.");
    status = { status: 200, body: off };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText(/te pediremos un código/)).toBeInTheDocument();
  });
});
