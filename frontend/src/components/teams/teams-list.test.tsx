import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Team } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { TeamsList } from "./teams-list";

const LIST = "GET /api/v1/o/acme/teams/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "teams.view", scopes: [] }],
};
const team = (slug: string, extra: Partial<Team> = {}): Team => ({
  id: `id-${slug}`, // distinto del `slug`: lo que se enseña es el `slug`, no el identificador
  slug,
  name: slug,
  description: "",
  assignment_strategy: "MANUAL",
  is_active: true,
  ...extra,
});
const sales = team("ventas", {
  name: "Ventas Lima",
  description: "Clientes nuevos y cotizaciones",
  assignment_strategy: "ROUND_ROBIN",
});
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <TeamsList />
    </TenantProvider>,
  );
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
const lines = (name: string) => [...card(name).querySelectorAll("p")].map((p) => p.textContent);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // el espía de `console.error`, también si el test falló
});

describe("TeamsList", () => {
  it("muestra lo que devuelve la API: nombre, slug, descripción, asignación y estado", async () => {
    const closed = team("soporte", { name: "Soporte", description: "  ", is_active: false });
    mockApi({ [LIST]: { status: 200, body: { results: [sales, closed], next: null } } });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando equipos");
    expect(await screen.findByRole("list", { name: "Equipos" })).toBeVisible();
    expect(lines("Ventas Lima")).toEqual([
      "Ventas Limaventas",
      "Clientes nuevos y cotizaciones",
      "Asignación: Por turnos",
      "Activo",
    ]);
    expect(lines("Soporte")).toEqual([
      "Soportesoporte",
      "Sin descripción", // solo espacios: no hay nada que enseñar
      "Asignación: Manual",
      "Inactivo", // también los inactivos
    ]);
    for (const text of ["Ventas Lima", "ventas", "Clientes nuevos y cotizaciones"])
      expect(screen.getByText(text)).toHaveClass("wrap-anywhere"); // largo: se parte, no se recorta
    expect(screen.getByText("Asignación: Por turnos")).toHaveClass("wrap-anywhere");
    expect(screen.getByText("Activo")).toHaveClass("text-success");
    expect(screen.getByText("Inactivo")).toHaveClass("text-muted");
    expect(screen.getByText("Good Doggy / Equipos")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("2 equipos en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Equipos");
    expect(screen.getByText("Los equipos de trabajo de Acme SAC.")).toBeVisible();
    expect(screen.queryByText("Esta organización todavía no tiene equipos.")).toBeNull();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // solo lectura, una página
  });

  it("nombra cada forma de asignar, y enseña con su código la que no conoce", async () => {
    const strategies = [
      "MANUAL",
      "ROUND_ROBIN",
      "LOAD_BALANCED",
      "SKILL_BASED",
      "AI_RULES",
      "DICE",
      "constructor", // ni lo que hereda cualquier objeto
      "MANUAL.length", // ni una ruta dentro de un texto
      "A.B",
    ];
    const rows = strategies.map((strategy) =>
      team(strategy.toLowerCase(), {
        assignment_strategy: strategy as Team["assignment_strategy"],
      }),
    );
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({ [LIST]: { status: 200, body: { results: rows, next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Equipos" });
    expect(strategies.map((strategy) => lines(strategy.toLowerCase())[2])).toEqual([
      "Asignación: Manual",
      "Asignación: Por turnos",
      "Asignación: Por carga de trabajo",
      "Asignación: Por habilidades",
      "Asignación: Por reglas de IA",
      "Asignación: DICE", // la API se adelantó a esta versión: su código, no un error
      "Asignación: constructor",
      "Asignación: MANUAL.length",
      "Asignación: A.B",
    ]);
    expect(errors).not.toHaveBeenCalled(); // ningún texto sin resolver
  });

  it("un texto de la API con llaves o etiquetas se enseña tal cual", async () => {
    const odd = team("x", { name: "<b>{count}</b>", description: "{strategy} # {x, plural}" });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({ [LIST]: { status: 200, body: { results: [odd], next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Equipos" });
    expect(lines("<b>{count}</b>")).toEqual([
      "<b>{count}</b>x",
      "{strategy} # {x, plural}",
      "Asignación: Manual",
      "Activo",
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("1 equipo en la lista");
    expect(errors).not.toHaveBeenCalled();
  });

  it("una organización sin equipos lo dice, sin error", async () => {
    mockApi({ [LIST]: { status: 200, body: { results: [], next: null } } });
    screenOf();
    expect(await screen.findByText("Esta organización todavía no tiene equipos.")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Sin equipos en la lista");
    expect(screen.queryByRole("list")).not.toBeInTheDocument(); // ni una lista vacía
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pide sus propios datos, con la organización del contexto", async () => {
    const api = mockApi({ [LIST]: { status: 200, body: { results: [sales], next: null } } });
    const view = screenOf();
    await screen.findByRole("list", { name: "Equipos" });
    const cached = view.client
      .getQueryCache()
      .getAll()
      .map((query) => query.queryKey);
    expect(cached).toEqual([["/api/v1/o/acme/teams/", "pages"]]);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("carga la página siguiente con el cursor y deja el foco en lo que llegó", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [sales], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [team("soporte")], next: null } },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 equipos"));
    expect(api).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(card("soporte")).toHaveFocus());
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  it("sin el permiso lo explica; sin sesión no enseña un error; un fallo se puede reintentar", async () => {
    let reply: { status: number; body: unknown } = {
      status: 403,
      body: { code: "PERMISSION_DENIED" },
    };
    mockApi({ [LIST]: () => reply });
    const denied = screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los equipos de esta organización.",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // decide la API
    denied.unmount();
    reply = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
    const ended = screenOf();
    await waitFor(() => expect(ended.client.isFetching()).toBe(0));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el login lo decide el proveedor
    expect(screen.getByRole("status")).toHaveTextContent("Cargando equipos");
    ended.unmount();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = { status: 200, body: { results: [sales], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("list", { name: "Equipos" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });
});
