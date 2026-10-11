import { screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import LoginPage from "../login/page";

import PasswordRecoveryPage from "./page";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("next-intl/server", async () => {
  const { createTranslator } = await import("next-intl");
  const { default: messages } = await import("../../../messages/es-PE.json");
  return {
    getTranslations: async (namespace?: never) =>
      createTranslator({ locale: "es-PE", messages, namespace }),
  };
});

afterEach(() => vi.unstubAllGlobals());

it("`/recuperar` pide el enlace sin mirar la sesión y enlaza de vuelta al acceso", async () => {
  const api = mockApi({});
  renderApp(await PasswordRecoveryPage());
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Recupera tu acceso.");
  expect(screen.getByLabelText("Correo")).toBeVisible();
  expect(screen.getByRole("link", { name: "Volver a entrar" })).toHaveAttribute("href", "/login");
  expect(api).not.toHaveBeenCalled(); // página pública: no pregunta nada al abrir
  expect(document.querySelectorAll("script, iframe, img, link")).toHaveLength(0); // nada de terceros
});

it("`/login` enlaza a la página que pide el enlace", async () => {
  mockApi({ "GET /api/v1/auth/session/": { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
  renderApp(await LoginPage({ searchParams: Promise.resolve({}) }));
  const link = screen.getByRole("link", { name: "¿Olvidaste tu contraseña?" });
  expect(link).toHaveAttribute("href", "/recuperar");
  expect(within(document.forms[0] as HTMLFormElement).queryByRole("link")).toBeNull(); // fuera del formulario
});
