import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import InvitationPage, { generateMetadata } from "./page";

vi.mock("next-intl/server", async () => {
  const { createTranslator } = await import("next-intl");
  const { default: messages } = await import("../../../messages/es-PE.json");
  return {
    getTranslations: async (namespace?: never) =>
      createTranslator({ locale: "es-PE", messages, namespace }),
  };
});

afterEach(() => vi.unstubAllGlobals());

it("`/invitacion` abierta sin enlace no pide nada a la API ni carga nada de terceros", async () => {
  const api = mockApi({});
  renderApp(await InvitationPage());
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Te invitaron a un equipo.");
  expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Abre el enlace del correo.");
  expect(api).not.toHaveBeenCalled();
  expect(document.querySelectorAll("script, iframe, img, link")).toHaveLength(0);
  expect((await generateMetadata()).title).toBe("Invitación · Good Doggy CRM");
});

// Como `/restablecer`: el router de Next devolvería a la barra el fragmento con el secreto.
it("nada en el código enlaza ni navega a `/invitacion`", () => {
  const sources = (readdirSync("src", { recursive: true }) as string[])
    .filter((file) => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file))
    .map((file) => join("src", file));
  expect(sources.length).toBeGreaterThan(50);
  const naming = sources.filter((file) =>
    /["'`]\/invitacion(?!es)/.test(readFileSync(file, "utf8")),
  );
  expect(naming).toEqual([]);
});
