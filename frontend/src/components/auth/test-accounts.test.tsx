import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { LoginForm } from "./login-form";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const SESSION = "GET /api/v1/auth/session/";
const LIST = "GET /api/v1/auth/test-accounts/";
const LOGIN = "POST /api/v1/auth/login/";
const anonymous = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
const accounts = [
  { email: "owner@pruebas.gooddoggy.test", role: "Owner" },
  { email: "vendedor@pruebas.gooddoggy.test", role: "Vendedor" },
];

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  router.replace.mockReset();
  vi.unstubAllGlobals();
});

describe("Accesos de prueba en el login", () => {
  it("apagados (como en producción, la ruta da 404) no pintan nada", async () => {
    const api = mockApi({ [SESSION]: anonymous }); // cualquier otra ruta: 404
    renderApp(<LoginForm next="/o" />);
    await waitFor(() =>
      expect(api.mock.calls.some(([url]) => String(url).endsWith("/test-accounts/"))).toBe(true),
    );
    await waitFor(() => expect(screen.getByLabelText("Correo")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Accesos de prueba" })).toBeNull();
    expect(screen.queryByText(/pruebas\.gooddoggy\.test/)).toBeNull();
  });

  it("sin cuentas provisionadas tampoco", async () => {
    mockApi({ [SESSION]: anonymous, [LIST]: { status: 200, body: { accounts: [] } } });
    renderApp(<LoginForm next="/o" />);
    await waitFor(() => expect(screen.getByLabelText("Correo")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Accesos de prueba" })).toBeNull();
  });

  it("con cuentas, elegir una completa el correo y deja la contraseña al usuario", async () => {
    const api = mockApi({
      [SESSION]: anonymous,
      [LIST]: { status: 200, body: { accounts } },
      [LOGIN]: anonymous,
    });
    renderApp(<LoginForm next="/o" />);
    expect(await screen.findByRole("heading", { name: "Accesos de prueba" })).toBeInTheDocument();
    expect(screen.getByText(/Este es un entorno de pruebas/)).toBeInTheDocument();
    const seller = screen.getByRole("button", { name: /Vendedor/ });
    fireEvent.click(seller);
    expect(screen.getByLabelText("Correo")).toHaveValue("vendedor@pruebas.gooddoggy.test");
    expect(screen.getByLabelText("Contraseña")).toHaveValue(""); // nunca la completa
    expect(screen.getByLabelText("Contraseña")).toHaveFocus();
    expect(api.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false); // no entra solo
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
