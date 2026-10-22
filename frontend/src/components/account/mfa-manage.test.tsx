import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { AccountSecurity } from "./account-security";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const STATUS = "GET /api/v1/auth/mfa/";
const REGENERATE = "POST /api/v1/auth/mfa/backup-codes/";
const DISABLE = "POST /api/v1/auth/mfa/disable/";
const off = { enabled: false, enabled_at: null, enrollment_expires_in: null, backup_codes_left: 0 };
const on = { ...off, enabled: true, enabled_at: "2026-10-21T15:00:00Z", backup_codes_left: 2 };
const CODES = Array.from({ length: 10 }, (_, n) => `AAAA-BBBB-CCCC-${String(n).padStart(4, "0")}`);
const invalid = (field: string) => ({
  status: 400,
  body: { code: "VALIDATION_ERROR", fields: { [field]: [{ code: "invalid", message: "x" }] } },
});
type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

const posts = (api: ReturnType<typeof mockApi>, path: string) =>
  api.mock.calls.filter(([url, init]) => String(url).endsWith(path) && init?.method === "POST");
const sent = (api: ReturnType<typeof mockApi>, path: string, n: number) =>
  JSON.parse(String(posts(api, path)[n]?.[1]?.body)) as unknown;

function fill(password: string, code: string) {
  fireEvent.change(screen.getByLabelText("Contraseña actual"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Código"), { target: { value: code } });
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  router.replace.mockReset();
  vi.useRealTimers();
});

describe("AccountSecurity con el segundo factor activado", () => {
  it("sin segundo factor no ofrece regenerar ni quitar", async () => {
    mockApi({ [STATUS]: { status: 200, body: off } });
    renderApp(<AccountSecurity />);
    await screen.findByRole("button", { name: "Activar la verificación en dos pasos" });
    expect(screen.queryByRole("button", { name: /códigos de respaldo nuevos/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Quitar/ })).toBeNull();
  });

  it("regenera: contraseña y código de la aplicación, diez códigos una vez", async () => {
    let status: Reply = { status: 200, body: on };
    let regenerate: Reply = invalid("password");
    const api = mockApi({ [STATUS]: () => status, [REGENERATE]: () => regenerate });
    const { client } = renderApp(<AccountSecurity />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Generar códigos de respaldo nuevos" }),
    );
    expect(screen.getByLabelText("Contraseña actual")).toHaveFocus();
    expect(screen.queryByRole("link")).toBeNull(); // no se sale de la página a medias
    fill("clave", "");
    fireEvent.click(screen.getByRole("button", { name: "Generar códigos nuevos" }));
    expect(screen.getByLabelText("Código")).toHaveAccessibleDescription("Escribe el código.");
    expect(posts(api, "/mfa/backup-codes/")).toHaveLength(0);
    fill(" mala ", "123456");
    fireEvent.click(screen.getByRole("button", { name: "Generar códigos nuevos" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Contraseña actual")).toHaveAccessibleDescription(
        "La contraseña no es correcta.",
      ),
    );
    expect(sent(api, "/mfa/backup-codes/", 0)).toEqual({ password: " mala ", code: "123456" });
    regenerate = invalid("code"); // un código de respaldo no vale aquí
    fill("clave", "AAAA-BBBB-CCCC-DDDD");
    fireEvent.click(screen.getByRole("button", { name: "Generar códigos nuevos" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Código")).toHaveAccessibleDescription(
        /uno de respaldo no sirve aquí/,
      ),
    );
    expect(screen.getByLabelText("Código")).toHaveFocus();
    regenerate = { status: 200, body: { backup_codes: CODES } };
    fill("clave", "123 456");
    const button = screen.getByRole("button", { name: "Generar códigos nuevos" });
    fireEvent.click(button);
    fireEvent.click(button); // dos pulsaciones, una petición
    expect(
      await screen.findByRole("heading", { name: "Guarda tus códigos de respaldo" }),
    ).toHaveFocus();
    expect(posts(api, "/mfa/backup-codes/")).toHaveLength(3);
    expect(sent(api, "/mfa/backup-codes/", 2)).toEqual({ password: "clave", code: "123456" });
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(CODES);
    expect(screen.queryByText(/cerramos tus otras sesiones/)).toBeNull(); // regenerar no las cierra
    status = { status: 200, body: { ...on, backup_codes_left: 10 } };
    fireEvent.click(screen.getByRole("checkbox", { name: /He guardado estos códigos/ }));
    fireEvent.click(screen.getByRole("button", { name: "Terminar" }));
    expect(
      await screen.findByText("Te quedan 10 códigos de respaldo sin usar."),
    ).toBeInTheDocument();
    expect(screen.queryByText(CODES[0]!)).toBeNull();
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0));
    expect(location.search).toBe("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("quita: advierte, pide contraseña y código, y el estado dice que ya no está", async () => {
    let status: Reply = { status: 200, body: on };
    let disable: Reply = invalid("code");
    const api = mockApi({ [STATUS]: () => status, [DISABLE]: () => disable });
    renderApp(<AccountSecurity />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Quitar la verificación en dos pasos" }),
    );
    expect(screen.getByRole("note")).toHaveTextContent(/le bastará para entrar/);
    expect(screen.queryByRole("button", { name: "Cerrar sesión" })).toBeNull();
    fill("clave", "000000");
    fireEvent.click(screen.getByRole("button", { name: "Quitar" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Código")).toHaveAccessibleDescription(
        /uno de respaldo sin usar/,
      ),
    );
    disable = { status: 204 };
    status = { status: 200, body: off };
    fill("clave", "aaaa-bbbb-cccc-dddd");
    fireEvent.click(screen.getByRole("button", { name: "Quitar" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Quitaste la verificación en dos pasos. Por seguridad, cerramos tus otras sesiones abiertas.",
    );
    expect(sent(api, "/mfa/disable/", 1)).toEqual({ password: "clave", code: "aaaabbbbccccdddd" });
    expect(
      screen.getByRole("button", { name: "Activar la verificación en dos pasos" }),
    ).toBeInTheDocument();
  });

  it("si otra sesión ya lo quitó, enseña el estado; los límites dicen su espera", async () => {
    let status: Reply = { status: 200, body: on };
    let disable: Reply = {
      status: 429,
      body: { code: "RATE_LIMITED" },
      headers: { "Retry-After": "120" },
    };
    mockApi({ [STATUS]: () => status, [DISABLE]: () => disable });
    renderApp(<AccountSecurity />);
    fireEvent.click(await screen.findByRole("button", { name: /Quitar la verificación/ }));
    fill("clave", "123456");
    fireEvent.click(screen.getByRole("button", { name: "Quitar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("en 2 minutos");
    status = { status: 200, body: off };
    disable = { status: 409, body: { code: "MFA_NOT_ENABLED" } };
    fireEvent.click(screen.getByRole("button", { name: "Quitar" }));
    expect(await screen.findByText(/te pediremos un código/)).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull(); // no lo quitó esta pantalla
  });

  it("se puede cancelar, y deja de esperar a un servidor que no contesta", async () => {
    const api = mockApi({ [STATUS]: { status: 200, body: on } });
    renderApp(<AccountSecurity />);
    fireEvent.click(await screen.findByRole("button", { name: /Generar códigos de respaldo/ }));
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    fireEvent.click(screen.getByRole("button", { name: /Quitar la verificación/ }));
    const usual = api.getMockImplementation()!;
    api.mockImplementation((input, init) =>
      String(input).endsWith("/mfa/disable/")
        ? new Promise<Response>(() => {})
        : usual(input, init),
    );
    vi.useFakeTimers();
    fill("clave", "123456");
    fireEvent.click(screen.getByRole("button", { name: "Quitar" }));
    const cancel = screen.getByRole("button", { name: "Cancelar" });
    expect(cancel).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(cancel); // mientras se envía no se sale
    expect(screen.getByRole("button", { name: "Quitando…" })).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(30_500));
    expect(screen.getByRole("alert")).toHaveTextContent("El servidor tarda demasiado");
    expect(screen.getByRole("button", { name: "Quitar" })).toBeInTheDocument();
  });
});
