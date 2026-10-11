import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import PasswordResetPage from "./page";

vi.mock("next-intl/server", async () => {
  const { createTranslator } = await import("next-intl");
  const { default: messages } = await import("../../../messages/es-PE.json");
  return {
    getTranslations: async (namespace?: never) =>
      createTranslator({ locale: "es-PE", messages, namespace }),
  };
});

afterEach(() => vi.unstubAllGlobals());

it("`/restablecer` abierta sin enlace no pide nada a la API y ofrece pedir uno", async () => {
  const api = mockApi({});
  renderApp(await PasswordResetPage());
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
    "Elige una contraseña nueva.",
  );
  expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Este enlace no vale.");
  expect(screen.getByRole("link", { name: "Pedir otro enlace" })).toHaveAttribute(
    "href",
    "/recuperar",
  );
  expect(api).not.toHaveBeenCalled(); // página pública: no mira la sesión
  expect(document.querySelectorAll("script, iframe, img, link")).toHaveLength(0); // nada de terceros
});
