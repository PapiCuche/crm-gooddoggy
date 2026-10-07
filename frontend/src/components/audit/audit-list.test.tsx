import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AuditEntry, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { AuditList } from "./audit-list";

const LIST = "GET /api/v1/o/acme/audit/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "audit.view", scopes: [] }],
};
const entry = (id: string, extra: Partial<AuditEntry> = {}): AuditEntry => ({
  id: `fila-${id}`,
  occurred_at: "2026-10-07T03:15:04.123456Z",
  actor_type: "USER",
  actor_id: `actor-${id}`,
  actor_label: null,
  action: "branch.updated",
  entity_type: "branch",
  entity_id: `entity-${id}`,
  entity_label: null,
  changes: {},
  metadata: {},
  result: "SUCCESS",
  correlation_id: `corr-${id}`,
  ...extra,
});
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <AuditList />
    </TenantProvider>,
  );
const rows = () => within(screen.getByRole("list", { name: "Auditoría" })).getAllByRole("listitem");
const lines = (row: HTMLElement) =>
  [...row.querySelectorAll("p > span, div > p, time")].map((node) => node.textContent);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // el espía de `console.error`, también si el test falló
});

describe("AuditList", () => {
  it("muestra lo que devuelve la API: acción, entidad, actor, fecha y resultado", async () => {
    const renamed = entry("a1", {
      entity_label: "LIM-01",
      actor_label: "Ana López",
      changes: { legal_name: ["Centro", "Centro de Lima"] },
      metadata: { operator: "ops@plataforma.pe", reason: "motivo-privado" },
    });
    const refused = entry("a2", {
      occurred_at: "2026-01-02T03:04:05Z",
      actor_type: "SYSTEM",
      actor_id: null,
      action: "role.permission_granted",
      entity_type: "role",
      result: "DENIED",
    });
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [renamed, refused], next: null } },
    });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando la auditoría…");
    expect(await screen.findByRole("list", { name: "Auditoría" })).toBeVisible();
    const [first, second] = rows() as [HTMLElement, HTMLElement];
    expect(lines(first)).toEqual([
      "Sucursal editada",
      "branch.updated", // el código, debajo del nombre
      "Sucursal: LIM-01",
      "Por: una persona (Ana López)",
      expect.stringMatching(/^6 oct\.? 2026, 10:15:04\sp\.\sm\.$/), // en la zona de la aplicación
      "Correcto",
    ]);
    expect(lines(second)).toEqual([
      "Permiso concedido a un rol",
      "role.permission_granted",
      "Rol", // sin etiqueta: solo el tipo
      "Por: el sistema",
      expect.stringMatching(/^1 ene\.? 2026, 10:04:05\sp\.\sm\.$/),
      "Denegado",
    ]);
    expect(within(first).getByText("Correcto")).toHaveClass("text-muted");
    expect(within(second).getByText("Denegado")).toHaveClass("text-danger");
    const time = first.querySelector("time");
    expect(time).toHaveAttribute("datetime", "2026-10-07T03:15:04.123456Z");
    const [name, code, entity, actor, result] = first.querySelectorAll("p > span, div > p");
    for (const line of [name, code, entity, actor, result])
      expect(line).toHaveClass("wrap-anywhere");
    // Ni los cambios, ni el contexto, ni identificadores (OBS-F2-73-1), tampoco en atributos.
    for (const hidden of [
      ...Object.keys({ ...renamed.changes, ...renamed.metadata }), // ni los nombres de los campos
      "Centro",
      "ops@plataforma.pe",
      "motivo-privado",
      "fila-",
      "actor-",
      "entity-",
      "corr-",
    ])
      expect(new XMLSerializer().serializeToString(document.body)).not.toContain(hidden);
    expect(screen.getByRole("status")).toHaveTextContent("2 registros en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Auditoría");
    expect(screen.getByText(/Lo que se hizo en Acme SAC, de lo más reciente/)).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/o/acme/audit/"]);
  });

  it("una acción o un tipo sin nombre propio se enseñan con su código, una sola vez", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const results = [
      entry("a1", { action: "quote.sent", entity_type: "quote", entity_label: "COT-000123" }),
      entry("a2", { action: "constructor", entity_type: "toString" }), // claves heredadas
      entry("a3", { action: "actions.length", entity_type: "__proto__" }),
      entry("a3b", { action: "role.created.0", entity_type: "role.created" }), // ni en un texto
      entry("a4", { action: "branch", entity_type: "entities.role" }), // ni rutas de mensajes
      entry("a5", { actor_type: "ROBOT" as never, result: "__proto__" as never }), // fuera del enum
      entry("a6", { actor_type: "constructor" as never, result: null as never }), // ni un texto
      entry("a7", { actor_type: 7 as never, result: "toString" as never }),
    ];
    mockApi({ [LIST]: { status: 200, body: { results, next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Auditoría" });
    expect(rows().map((row) => lines(row).slice(0, 2))).toEqual([
      ["quote.sent", "quote: COT-000123"],
      ["constructor", "toString"],
      ["actions.length", "__proto__"],
      ["role.created.0", "role.created"],
      ["branch", "entities.role"],
      ...Array(3).fill(["Sucursal editada", "branch.updated"]),
    ]);
    const unknown = rows().slice(-3);
    expect(unknown.map((row) => lines(row).slice(3))).toEqual([
      ["Por: ROBOT", expect.any(String), "__proto__"],
      ["Por: constructor", expect.any(String), "null"],
      ["Por: 7", expect.any(String), "toString"],
    ]);
    expect(screen.getByText("null")).toHaveClass("text-danger"); // no es «Correcto»: resaltado
    expect(error).not.toHaveBeenCalled(); // ningún mensaje que falte
  });

  it.each([
    ["USER", "Por: una persona"],
    ["AI_AGENT", "Por: un agente de IA"],
    ["SYSTEM", "Por: el sistema"],
    ["INTEGRATION", "Por: una integración"],
    ["PLATFORM_STAFF", "Por: personal de la plataforma"],
  ] as const)("el actor %s se nombra por su tipo", async (actor_type, text) => {
    const results = [entry("a1", { actor_type, result: "FAILED" })];
    mockApi({ [LIST]: { status: 200, body: { results, next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Auditoría" });
    expect(lines(rows()[0] as HTMLElement).slice(3)).toEqual([text, expect.any(String), "Falló"]);
    expect(screen.getByText("Falló")).toHaveClass("text-danger"); // resaltado, como «Denegado»
    expect(screen.getByRole("status")).toHaveTextContent("1 registro en la lista");
  });

  it("sin filas lo dice, y sin permiso explica el 403", async () => {
    mockApi({ [LIST]: { status: 200, body: { results: [], next: null } } });
    const view = screenOf();
    expect(
      await screen.findByText("Todavía no hay nada en la auditoría de esta organización."),
    ).toBeVisible();
    expect(screen.queryByRole("list", { name: "Auditoría" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Sin registros en la lista");
    view.unmount();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver la auditoría de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).not.toBeInTheDocument();
  });

  it("carga la página siguiente con el cursor", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [entry("a1")], next: "abc+/=" } },
      [`${LIST}?cursor=abc%2B%2F%3D`]: {
        status: 200,
        body: {
          results: [entry("a2", { action: "team.created", entity_type: "team" })],
          next: null,
        },
      },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 registros"));
    expect(rows().map((row) => lines(row)[0])).toEqual(["Sucursal editada", "Equipo creado"]);
    expect(api).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  describe("filtros", () => {
    const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
    const page = (...results: AuditEntry[]) => ({ status: 200, body: { results, next: null } });
    const select = (name: string) => screen.getByRole("combobox", { name }) as HTMLSelectElement;
    const choose = (name: string, value: string) =>
      fireEvent.change(select(name), { target: { value } });
    // Deja en espera la siguiente respuesta de la API hasta que se llama a lo que devuelve.
    function hold(api: ReturnType<typeof mockApi>) {
      const answer = api.getMockImplementation()!;
      let release = () => {};
      api.mockImplementationOnce(async (...request) => {
        await new Promise<void>((resolve) => (release = resolve));
        return answer(...request);
      });
      return () => release();
    }

    it("ofrece lo que la pantalla sabe nombrar y no pide nada hasta que se elige", async () => {
      const api = mockApi({ [LIST]: page(entry("a1")) });
      screenOf();
      const group = screen.getByRole("group", { name: "Filtros" }); // ya mientras carga
      await screen.findByRole("list", { name: "Auditoría" });
      const options = (name: string) =>
        [...select(name).options].map((option) => [option.value, option.text]);
      expect(options("Acción")).toHaveLength(20);
      expect(options("Acción").slice(0, 3)).toEqual([
        ["", "Todas"],
        ["organization.created", "Organización creada"],
        ["membership.role_assigned", "Rol asignado a un miembro"],
      ]);
      expect(options("Entidad")).toEqual([
        ["", "Todas"],
        ["organization", "Organización"],
        ["membership", "Membresía"],
        ["role", "Rol"],
        ["branch", "Sucursal"],
        ["team", "Equipo"],
      ]);
      expect(options("Quién")).toEqual([
        ["", "Cualquiera"],
        ["USER", "Una persona"],
        ["AI_AGENT", "Un agente de IA"],
        ["SYSTEM", "El sistema"],
        ["INTEGRATION", "Una integración"],
        ["PLATFORM_STAFF", "Personal de la plataforma"],
      ]);
      for (const name of ["Acción", "Entidad", "Quién"]) {
        expect(select(name)).toHaveValue("");
        expect(select(name)).toHaveClass("text-[16px]", "h-11");
      }
      expect(within(group).queryByRole("button")).not.toBeInTheDocument(); // nada que quitar
      expect(urls(api)).toEqual(["/api/v1/o/acme/audit/"]);
    });

    it("al elegir pide la lista con ese filtro, y el selector conserva el foco mientras llega", async () => {
      const created = entry("b1", { action: "role.created", entity_type: "role" });
      const api = mockApi({
        [LIST]: page(entry("a1")),
        [`${LIST}?action=role.created`]: page(created),
        [`${LIST}?action=role.created&entity_type=role&actor_type=SYSTEM`]: page(),
        [`${LIST}?entity_type=role&actor_type=SYSTEM`]: page(created, entry("a1")),
      });
      screenOf();
      await screen.findByRole("list", { name: "Auditoría" });
      const release = hold(api);
      const action = select("Acción");
      action.focus();
      choose("Acción", "role.created");
      expect(screen.getByRole("status")).toHaveTextContent("Cargando la auditoría…");
      expect(screen.queryByRole("list", { name: "Auditoría" })).not.toBeInTheDocument();
      expect(action).toBeInTheDocument(); // el mismo selector, sin desmontarse
      expect(action).toHaveFocus();
      expect(action).toHaveValue("role.created");
      release();
      await waitFor(() => expect(rows().map((row) => lines(row)[0])).toEqual(["Rol creado"]));
      expect(action).toHaveFocus();
      choose("Entidad", "role");
      choose("Quién", "SYSTEM"); // combinados: se envían todos
      expect(await screen.findByText("Ningún registro coincide con estos filtros.")).toBeVisible(); // distinto de una auditoría vacía
      expect(screen.queryByText(/Todavía no hay nada/)).not.toBeInTheDocument();
      expect(screen.getByRole("group", { name: "Filtros" })).toBeVisible();
      choose("Acción", ""); // «Todas»: ese filtro deja de enviarse
      await waitFor(() => expect(rows()).toHaveLength(2));
      expect(urls(api)).toEqual([
        "/api/v1/o/acme/audit/",
        "/api/v1/o/acme/audit/?action=role.created",
        "/api/v1/o/acme/audit/?action=role.created&entity_type=role",
        "/api/v1/o/acme/audit/?action=role.created&entity_type=role&actor_type=SYSTEM",
        "/api/v1/o/acme/audit/?entity_type=role&actor_type=SYSTEM",
      ]);
      for (const url of urls(api)) expect(url).not.toMatch(/=(&|$)/); // ningún filtro vacío
    });

    it("«Quitar filtros» los quita todos y deja el foco en el primer selector", async () => {
      const api = mockApi({
        [LIST]: page(entry("a1")),
        [`${LIST}?entity_type=team`]: page(),
        [`${LIST}?entity_type=team&actor_type=USER`]: page(),
      });
      screenOf();
      await screen.findByRole("list", { name: "Auditoría" });
      choose("Entidad", "team");
      choose("Quién", "USER");
      const clear = await screen.findByRole("button", { name: "Quitar filtros" });
      await screen.findByText("Ningún registro coincide con estos filtros.");
      clear.focus();
      fireEvent.click(clear);
      expect(clear).not.toBeInTheDocument();
      expect(select("Acción")).toHaveFocus();
      for (const name of ["Acción", "Entidad", "Quién"]) expect(select(name)).toHaveValue("");
      await waitFor(() => expect(rows()).toHaveLength(1));
      expect(urls(api).at(-1)).toBe("/api/v1/o/acme/audit/"); // se vuelve a pedir, sin filtros
    });

    it("«Cargar más» lleva los mismos filtros, y un fallo o una negativa no esconden de más", async () => {
      let reply: { status: number; body: unknown } = {
        status: 200,
        body: { results: [entry("a1")], next: "abc" },
      };
      const api = mockApi({
        [LIST]: page(entry("a0")),
        [`${LIST}?actor_type=USER`]: () => reply,
        [`${LIST}?actor_type=USER&cursor=abc`]: page(entry("a2", { action: "team.created" })),
        [`${LIST}?entity_type=branch&actor_type=USER`]: {
          status: 403,
          body: { code: "PERMISSION_DENIED" },
        },
      });
      screenOf();
      await screen.findByRole("list", { name: "Auditoría" });
      choose("Quién", "USER");
      fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
      await waitFor(() => expect(rows()).toHaveLength(2));
      expect(urls(api).at(-1)).toBe("/api/v1/o/acme/audit/?actor_type=USER&cursor=abc");
      reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
      choose("Quién", ""); // otra lista, y vuelta a esta: ahora falla
      await waitFor(() => expect(rows().map((row) => lines(row)[1])).toEqual(["branch.updated"]));
      choose("Quién", "USER");
      expect(await screen.findByRole("button", { name: "Reintentar" })).toBeVisible();
      expect(select("Quién")).toHaveValue("USER"); // los filtros siguen ahí para cambiarlos
      choose("Entidad", "branch");
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "No tienes permiso para ver la auditoría de esta organización.",
      );
      expect(screen.queryByRole("group", { name: "Filtros" })).not.toBeInTheDocument();
    });
  });
});
