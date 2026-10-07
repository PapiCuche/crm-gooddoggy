import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AuditEntry, Member, SelfContext } from "@/lib/api/model";
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
const screenOf = (context = tenant) =>
  renderApp(
    <TenantProvider value={context}>
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
    expect(document.body).toHaveFocus(); // nadie estaba en los filtros: el foco no se mueve
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
      for (const [code] of options("Acción").slice(1)) expect(code).toMatch(/^[a-z]+\.[a-z_]+$/);
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
      expect(select("Entidad")).toHaveValue("role"); // cada selector enseña lo suyo
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
        [`${LIST}?action=team.created&entity_type=team&actor_type=USER`]: page(),
      });
      screenOf();
      await screen.findByRole("list", { name: "Auditoría" });
      choose("Acción", "team.created");
      choose("Entidad", "team");
      choose("Quién", "USER");
      const clear = await screen.findByRole("button", { name: "Quitar filtros" });
      expect(clear).toHaveClass("min-h-11");
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
      select("Entidad").focus();
      choose("Entidad", "branch");
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "No tienes permiso para ver la auditoría de esta organización.",
      );
      // Con la negativa los filtros siguen montados: el foco no se queda sin sitio.
      expect(screen.getByRole("group", { name: "Filtros" })).toBeVisible();
      expect(select("Entidad")).toHaveFocus();
    });
  });

  describe("quién fue", () => {
    const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
    const seesPeople: SelfContext = {
      ...tenant,
      permissions: [...tenant.permissions, { code: "users.view", scopes: [] }],
    };
    // La membresía (`m-…`) y la cuenta (`u-…`) con ids distintos: la auditoría guarda la cuenta.
    const member = (id: string, first_name: string, last_name: string): Member => ({
      id: `m-${id}`,
      status: "ACTIVE",
      joined_at: "2026-10-04T15:49:34Z",
      default_branch: null,
      user: { id: `u-${id}`, email: `${id}@acme.pe`, first_name, last_name },
      roles: [],
    });
    const [ana, luis, zoe] = [
      member("ana", "Ana", "López"),
      member("luis", "", ""),
      member("zoe", "Érika", "Zoe"),
    ];
    const directory = {
      [MEMBERS]: { status: 200, body: { results: [zoe], next: "p2" } },
      [`${MEMBERS}&cursor=p2`]: { status: 200, body: { results: [ana, luis], next: null } },
    };
    const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
    const page = (...results: AuditEntry[]) => ({ status: 200, body: { results, next: null } });
    const who = () => rows().map((row) => lines(row)[3]);
    const grid = () => screen.getByRole("group", { name: "Filtros" }).firstElementChild;

    it("con `users.view` nombra a cada persona con el directorio, todas sus páginas", async () => {
      const api = mockApi({
        ...directory,
        [LIST]: page(
          entry("a1", { actor_id: "u-ana" }),
          entry("a2", { actor_id: "u-luis" }), // sin nombre: su correo
          entry("a3", { actor_id: "u-nadie" }), // ya no está en el directorio
          entry("a4", { actor_id: "m-ana" }), // el id de la membresía no es el de la cuenta
          entry("a5", { actor_id: "u-ana", actor_type: "AI_AGENT" }), // solo las personas
          entry("a6", { actor_id: "u-ana", actor_label: "Ana de entonces" }), // lo anotado gana
          entry("a7", { actor_id: null }),
        ),
      });
      const view = screenOf(seesPeople);
      await waitFor(() =>
        expect(who()).toEqual([
          "Por: una persona (Ana López)",
          "Por: una persona (luis@acme.pe)",
          "Por: una persona",
          "Por: una persona",
          "Por: un agente de IA",
          "Por: una persona (Ana de entonces)",
          "Por: una persona",
        ]),
      );
      expect(urls(api).sort()).toEqual([
        "/api/v1/o/acme/audit/",
        "/api/v1/o/acme/members/?limit=200",
        "/api/v1/o/acme/members/?limit=200&cursor=p2",
      ]);
      // Ningún identificador en el DOM: ni el de la cuenta ni el de la membresía.
      const dom = new XMLSerializer().serializeToString(document.body);
      for (const hidden of ["u-ana", "u-luis", "u-zoe", "m-ana", "u-nadie"])
        expect(dom).not.toContain(hidden);
      expect(grid()).toHaveClass("sm:grid-cols-2"); // cuatro selectores: de dos en dos
      view.unmount(); // al salir, el directorio tampoco queda en memoria
      await waitFor(() => expect(view.client.getQueryCache().getAll()).toEqual([]));
    });

    it("sin `users.view` ni pide el directorio ni ofrece «Persona»", async () => {
      const api = mockApi({ ...directory, [LIST]: page(entry("a1", { actor_id: "u-ana" })) });
      screenOf();
      await screen.findByRole("list", { name: "Auditoría" });
      await new Promise((resolve) => setTimeout(resolve, 5));
      expect(who()).toEqual(["Por: una persona"]);
      expect(urls(api)).toEqual(["/api/v1/o/acme/audit/"]);
      expect(screen.queryByRole("combobox", { name: "Persona" })).not.toBeInTheDocument();
      expect(screen.getAllByRole("combobox")).toHaveLength(3);
      expect(grid()).toHaveClass("sm:grid-cols-3");
    });

    it("si `users.view` desaparece con la pantalla abierta, se van «Persona» y los nombres", async () => {
      mockApi({ ...directory, [LIST]: page(entry("a1", { actor_id: "u-ana" })) });
      // El contexto cambia en su sitio: `TenantGate` lo vuelve a pedir al volver a la ventana.
      function Session() {
        const [context, setContext] = useState(seesPeople);
        return (
          <TenantProvider value={context}>
            <AuditList />
            <button onClick={() => setContext(tenant)}>pierde el permiso</button>
          </TenantProvider>
        );
      }
      renderApp(<Session />);
      await screen.findByRole("combobox", { name: "Persona" });
      await screen.findByText("Por: una persona (Ana López)");
      fireEvent.click(screen.getByRole("button", { name: "pierde el permiso" }));
      expect(screen.queryByRole("combobox", { name: "Persona" })).not.toBeInTheDocument();
      expect(who()).toEqual(["Por: una persona"]); // lo ya leído tampoco se enseña
      expect(grid()).toHaveClass("sm:grid-cols-3");
    });

    it("«Persona» filtra por la cuenta de quien se elige, y se quita con los demás", async () => {
      const api = mockApi({
        ...directory,
        [LIST]: page(entry("a1")),
        [`${LIST}?actor_id=u-zoe`]: page(entry("a2", { actor_id: "u-zoe" })),
        [`${LIST}?actor_type=USER&actor_id=u-zoe`]: page(),
      });
      screenOf(seesPeople);
      const select = (await screen.findByRole("combobox", {
        name: "Persona",
      })) as HTMLSelectElement;
      await waitFor(() => expect(select.options).toHaveLength(4));
      expect([...select.options].map((option) => [option.value, option.text])).toEqual([
        ["", "Cualquiera"],
        ["ana@acme.pe", "Ana López (ana@acme.pe)"], // por su texto; el valor, el correo
        ["zoe@acme.pe", "Érika Zoe (zoe@acme.pe)"], // ni por correo ni por código: É tras la A
        ["luis@acme.pe", "luis@acme.pe"],
      ]);
      select.focus();
      fireEvent.change(select, { target: { value: "zoe@acme.pe" } });
      await waitFor(() => expect(who()).toEqual(["Por: una persona (Érika Zoe)"]));
      expect(select).toHaveFocus();
      expect(select).toHaveValue("zoe@acme.pe");
      const dom = new XMLSerializer().serializeToString(document.body); // ni con alguien elegido
      for (const hidden of ["u-zoe", "m-zoe"]) expect(dom).not.toContain(hidden);
      fireEvent.change(screen.getByRole("combobox", { name: "Quién" }), {
        target: { value: "USER" },
      });
      await screen.findByText("Ningún registro coincide con estos filtros.");
      fireEvent.click(screen.getByRole("button", { name: "Quitar filtros" }));
      await waitFor(() => expect(rows()).toHaveLength(1));
      expect(select).toHaveValue("");
      expect(urls(api).filter((url) => url.includes("/audit/"))).toEqual([
        "/api/v1/o/acme/audit/",
        "/api/v1/o/acme/audit/?actor_id=u-zoe", // la cuenta, no la membresía ni el correo
        "/api/v1/o/acme/audit/?actor_type=USER&actor_id=u-zoe",
        "/api/v1/o/acme/audit/",
      ]);
    });

    it("si el directorio falla, la auditoría sigue: sin nombres, sin aviso y sin «Persona»", async () => {
      const api = mockApi({
        [MEMBERS]: { status: 500, body: { code: "INTERNAL_ERROR" } },
        [LIST]: page(entry("a1", { actor_id: "u-ana" })),
      });
      screenOf(seesPeople);
      await screen.findByRole("list", { name: "Auditoría" });
      await waitFor(() =>
        expect(urls(api).filter((url) => url.includes("/members/"))).toHaveLength(2),
      ); // la aplicación reintenta una lectura una vez
      await waitFor(() =>
        expect(screen.queryByRole("combobox", { name: "Persona" })).not.toBeInTheDocument(),
      );
      expect(who()).toEqual(["Por: una persona"]);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getAllByRole("combobox")).toHaveLength(3);
    });

    it("«Persona» llega con el directorio, y un fallo al volver a pedirlo no la quita", async () => {
      let down = false;
      const api = mockApi({
        [MEMBERS]: () =>
          down
            ? { status: 500, body: { code: "INTERNAL_ERROR" } }
            : { status: 200, body: { results: [ana], next: null } },
        [LIST]: page(entry("a1", { actor_id: "u-ana" })),
      });
      const view = screenOf(seesPeople);
      expect(screen.queryByRole("combobox", { name: "Persona" })).not.toBeInTheDocument(); // aún no
      const select = await screen.findByRole("combobox", { name: "Persona" });
      await screen.findByText("Por: una persona (Ana López)");
      select.focus();
      down = true;
      fireEvent(window, new Event("visibilitychange")); // al volver a la ventana se pide otra vez
      const read = () => view.client.getQueryState(["/api/v1/o/acme/members/", "all"]);
      await waitFor(() => expect(read()?.status).toBe("error"));
      await new Promise((resolve) => setTimeout(resolve, 5));
      expect(urls(api).filter((url) => url.includes("/members/"))).toHaveLength(3);
      expect(select).toBeInTheDocument(); // el mismo selector, con su foco y con lo ya leído
      expect(select).toHaveFocus();
      expect(who()).toEqual(["Por: una persona (Ana López)"]);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });
});
