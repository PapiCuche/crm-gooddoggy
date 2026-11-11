import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NOW, BASE, gets, page, serve, show, task, type Reply } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Mis tareas (F5-08c)", () => {
  it("pide las de hoy con los límites del día en la zona de la pantalla", async () => {
    const calls = serve(() => page(task("1")));
    show("tasks.view");
    expect(await screen.findByText("Tarea 1")).toBeVisible();
    const [url] = gets(calls);
    expect(url!.pathname).toBe(BASE);
    expect(Object.fromEntries(url!.searchParams)).toEqual({
      mine: "true",
      due_from: "2026-11-11T05:00:00.000Z",
      due_to: "2026-11-12T05:00:00.000Z",
      limit: "100",
    });
    expect(screen.getByRole("heading", { level: 1, name: "Mis tareas" })).toBeVisible();
  });

  it("las pestañas piden Vencidas y Próximas, y se mueven con las flechas", async () => {
    const calls = serve((url) => {
      if (url.searchParams.get("overdue")) {
        return page(
          task("v1", { due_at: "2026-11-10T05:00:00Z", is_all_day: true }),
          task("hoy", { due_at: "2026-11-11T05:00:00Z", is_all_day: true }), // hoy: aún no vence
          task("v2", { due_at: "2026-11-11T14:00:00Z" }),
        );
      }
      return page(task("p1", { due_at: "2026-11-20T05:00:00Z", is_all_day: true }));
    });
    show("tasks.view");
    await screen.findByText("Tarea p1");
    const today = screen.getByRole("tab", { name: "Hoy" });
    today.focus();
    fireEvent.keyDown(today, { key: "ArrowRight" });
    expect(await screen.findByText("Tarea v1")).toBeVisible();
    expect(screen.getByRole("tab", { name: "Vencidas" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Vencidas" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Tarea v2")).toBeVisible();
    expect(screen.queryByText("Tarea hoy")).toBeNull();
    expect(screen.getAllByText(/Vencida$/).length).toBe(2);
    const overdue = gets(calls).find((url) => url.searchParams.get("overdue") === "true");
    expect(overdue?.searchParams.get("mine")).toBe("true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Vencidas" }), { key: "ArrowRight" });
    expect(await screen.findByText("Tarea p1")).toBeVisible();
    const upcoming = gets(calls).find(
      (url) => url.searchParams.get("due_from") === "2026-11-12T05:00:00.000Z",
    );
    expect(upcoming?.searchParams.get("due_to")).toBeNull();
  });

  it("una pestaña abierta de un día para otro pide el día nuevo al volver a ella", async () => {
    const calls = serve(() => page(task("1")));
    show("tasks.view");
    await screen.findByText("Tarea 1");
    expect(gets(calls)[0]!.searchParams.get("due_from")).toBe("2026-11-11T05:00:00.000Z");
    vi.setSystemTime(Date.parse("2026-11-12T15:00:00Z")); // pasó la medianoche de Lima
    fireEvent.focus(window);
    await waitFor(() =>
      expect(gets(calls).at(-1)!.searchParams.get("due_from")).toBe("2026-11-12T05:00:00.000Z"),
    );
    expect(gets(calls).at(-1)!.searchParams.get("due_to")).toBe("2026-11-13T05:00:00.000Z");
  });

  it("Inicio, Fin y la flecha izquierda dan la vuelta entre pestañas", async () => {
    serve(() => page(task("1")));
    show("tasks.view");
    await screen.findByText("Tarea 1");
    const tabs = () => screen.getByRole("tablist");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Hoy" }), { key: "End" });
    expect(screen.getByRole("tab", { name: "Próximas" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Próximas" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "Home" });
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
    // Alt+flecha es «atrás» del navegador: ni se traga ni cambia de pestaña.
    expect(fireEvent.keyDown(tabs(), { key: "ArrowRight", altKey: true })).toBe(true);
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
  });

  it("el texto de otra persona es texto: no se interpreta y va aislado", async () => {
    serve(() =>
      page(
        task("1", {
          title: "<img src=x onerror=alert(1)> ‮oculto",
          description: "línea 1\nlínea 2 <b>no</b>",
          contact: { id: "c1", display_name: "<i>Rosa</i>" },
        }),
      ),
    );
    show("tasks.view");
    const title = await screen.findByText(/<img src=x/);
    expect(title.tagName).toBe("BDI");
    expect(title).toHaveAttribute("dir", "auto");
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/línea 2 <b>no<\/b>/).tagName).toBe("BDI");
    const link = screen.getByRole("link", { name: /Cliente:/ });
    expect(link).toHaveAttribute("href", "/o/acme/clientes/c1");
    expect(within(link).getByText("<i>Rosa</i>").tagName).toBe("BDI");
  });

  it("un cliente sin nombre visible se dice sin enlace; las cerradas, atenuadas y al final", async () => {
    serve(() =>
      page(
        task("1", { contact: { id: "c9", display_name: null } }),
        task("2", { opportunity_id: "o7" }),
        task("3", { status: "DONE" }),
      ),
    );
    show("tasks.view", "tasks.update");
    expect(await screen.findByText(/sin permiso para ver su nombre/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /Cliente/ })).toBeNull();
    expect(screen.getByRole("link", { name: "Ver la oportunidad" })).toHaveAttribute(
      "href",
      "/o/acme/oportunidades/o7",
    );
    // las cerradas, atenuadas y con su estado, después de las abiertas
    expect(screen.getByText("Completada")).toBeVisible();
    expect(screen.getByText("Tarea 3").closest("li")).toHaveClass("opacity-75");
    expect(screen.getAllByRole("listitem")[2]).toHaveTextContent("Tarea 3");
    expect(screen.getAllByRole("button", { name: /Editar la tarea/ })).toHaveLength(2); // no la cerrada
  });

  it("vacío, carga, error con reintento y sin permiso", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    serve(() => reply);
    show("tasks.view");
    expect(await screen.findByText("Cargando tareas…")).toBeVisible();
    expect(await screen.findByRole("alert")).toBeVisible();
    reply = { status: 200, body: { results: [], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("No tienes tareas para hoy.")).toBeVisible();
    cleanup();
    reply = { status: 403, body: { code: "PERMISSION_DENIED" } };
    show("tasks.view");
    expect(await screen.findByText(/No tienes permiso para ver las tareas/)).toBeVisible();
  });

  it("«Cargar más» suma la página siguiente y, si falla, deja lo cargado", async () => {
    let second: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    serve((url) =>
      url.searchParams.get("cursor")
        ? second
        : { status: 200, body: { results: [task("1")], next: `${BASE}?cursor=c2` } },
    );
    show("tasks.view");
    await screen.findByText("Tarea 1");
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
    second = { status: 403, body: { code: "PERMISSION_DENIED" } }; // una página siguiente
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText(/No tienes permiso para ver las tareas/)).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
    second = page(task("2"));
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Tarea 2")).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
  });
});

describe("crear y cambiar una tarea", () => {
  const created = task("9", { title: "Llamar a Rosa" });
  const fill = async (title: string) => {
    fireEvent.click(await screen.findByRole("button", { name: "Nueva tarea" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Título" }), {
      target: { value: title },
    });
  };
  const bodies = (calls: { url: URL; init?: RequestInit }[], method: string) =>
    calls
      .filter(({ init }) => init?.method === method)
      .map(({ init }) => JSON.parse(String(init!.body)));
  const post = (calls: { url: URL; init?: RequestInit }[]) => bodies(calls, "POST");

  it("sin tasks.update no hay «Editar» y sin tasks.create no hay «Nueva tarea»", async () => {
    serve(() => page(task("1", { title: "Antigua" })));
    show("tasks.view", "tasks.create");
    await screen.findByText("Antigua");
    expect(screen.queryByRole("button", { name: /Editar la tarea/ })).toBeNull();
    cleanup();
    show("tasks.view", "tasks.update");
    expect(await screen.findByRole("button", { name: /Editar la tarea/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Nueva tarea" })).toBeNull();
  });

  it("crea con la fecha de hoy y «todo el día»: envía las 00:00 de Lima, una vez", async () => {
    const calls = serve((url, init) =>
      init?.method === "POST" ? { status: 201, body: created } : page(),
    );
    show("tasks.view", "tasks.create");
    await fill("  Llamar a Rosa  ");
    expect(screen.getByLabelText(/^Fecha/)).toHaveValue("2026-11-11");
    const send = screen.getByRole("button", { name: "Guardar tarea" });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(await screen.findByText("Tarea creada.")).toBeVisible();
    expect(post(calls)).toEqual([
      expect.objectContaining({ title: "Llamar a Rosa", due_at: "2026-11-11T05:00:00.000Z" }),
    ]);
    await waitFor(() => expect(gets(calls).length).toBeGreaterThan(1)); // la lista se vuelve a pedir
  });

  it("con hora envía ese instante; sin hora no envía; sin fecha avisa que no saldrá", async () => {
    const calls = serve((url, init) =>
      init?.method === "POST"
        ? { status: 201, body: { ...created, ...JSON.parse(String(init.body)) } }
        : page(),
    );
    show("tasks.view", "tasks.create");
    await fill("Visita");
    fireEvent.click(screen.getByRole("checkbox", { name: "Todo el día" }));
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText("Elige la hora o marca «Todo el día».")).toBeVisible();
    expect(post(calls)).toEqual([]);
    fireEvent.change(screen.getByLabelText("Hora"), { target: { value: "14:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText("Tarea creada.")).toBeVisible();
    expect(post(calls)[0]).toMatchObject({ due_at: "2026-11-11T19:30:00.000Z", is_all_day: false });
    await fill("Sin fecha");
    fireEvent.change(screen.getByLabelText(/^Fecha/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText(/sin fecha: no sale en Hoy/)).toBeVisible();
    expect(post(calls)[1]).toMatchObject({ due_at: null, is_all_day: false });
  });

  it("un título vacío o un año absurdo no se envían (se explican); Escape cierra sin enviar", async () => {
    const calls = serve(() => page());
    show("tasks.view", "tasks.create");
    await fill("   ");
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText("Escribe un título.")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Título" }), {
      target: { value: "Vieja" },
    });
    fireEvent.change(screen.getByLabelText(/^Fecha/), { target: { value: "0001-01-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText("La fecha no es válida.")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Título" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(post(calls)).toEqual([]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Nueva tarea" })).toHaveFocus());
  });

  it("una descripción con tabulador se explica junto al campo y lo escrito se conserva", async () => {
    serve((url, init) =>
      init?.method === "POST"
        ? { status: 400, body: { code: "VALIDATION_ERROR", fields: { description: ["x"] } } }
        : page(),
    );
    show("tasks.view", "tasks.create");
    await fill("Con tab");
    fireEvent.change(screen.getByRole("textbox", { name: "Descripción" }), {
      target: { value: "a\tb" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText(/caracteres que no se aceptan/)).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Título" })).toHaveValue("Con tab"); // se conserva
  });

  it("cambia con la versión leída, solo lo cambiado: título, hora de la zona y fecha quitada", async () => {
    const open = task("1", { title: "Antigua", due_at: "2026-11-11T19:30:00Z", version: 5 });
    const calls = serve((url, init) =>
      init?.method === "PATCH" ? { status: 200, body: open } : page(open),
    );
    show("tasks.view", "tasks.update");
    const edit = () => screen.findByRole("button", { name: "Editar la tarea Antigua" });
    const save = async (count: number) => {
      fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
      await waitFor(() => expect(bodies(calls, "PATCH")).toHaveLength(count));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /Editar la tarea/ })).toHaveFocus(),
      );
    };
    fireEvent.click(await edit());
    expect(await screen.findByLabelText(/^Fecha/)).toHaveValue("2026-11-11");
    expect(screen.getByLabelText("Hora")).toHaveValue("14:30");
    fireEvent.change(screen.getByRole("textbox", { name: "Título" }), {
      target: { value: "Nueva" },
    });
    await save(1);
    fireEvent.click(await edit());
    fireEvent.change(await screen.findByLabelText("Hora"), { target: { value: "15:00" } });
    await save(2);
    fireEvent.click(await edit());
    fireEvent.change(await screen.findByLabelText(/^Fecha/), { target: { value: "" } });
    await save(3);
    expect(calls.find(({ init }) => init?.method === "PATCH")!.url.pathname).toBe(`${BASE}1/`);
    expect(bodies(calls, "PATCH")).toEqual([
      { version: 5, title: "Nueva" },
      { version: 5, due_at: "2026-11-11T20:00:00.000Z", is_all_day: false },
      { version: 5, due_at: null, is_all_day: false },
    ]);
  });

  it("envía la versión que leyó al abrir, no la que traiga la lista después", async () => {
    let version = 5;
    const calls = serve((url, init) =>
      init?.method === "PATCH"
        ? { status: 409, body: { code: "STALE_VERSION" } }
        : page(task("1", { title: version === 5 ? "Antigua" : "De otra persona", version })),
    );
    const { client } = show("tasks.view", "tasks.update");
    fireEvent.click(await screen.findByRole("button", { name: "Editar la tarea Antigua" }));
    await screen.findByRole("form", { name: "Editar la tarea" });
    version = 6; // otra persona la cambió y la lista se vuelve a pedir mientras se escribe
    await act(() => client.invalidateQueries());
    await waitFor(() => expect(gets(calls).length).toBeGreaterThan(1));
    fireEvent.change(screen.getByRole("textbox", { name: "Título" }), {
      target: { value: "Mía" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    await screen.findByText(/Otra persona cambió esta tarea/);
    expect(bodies(calls, "PATCH")).toEqual([{ version: 5, title: "Mía" }]);
  });

  it("si al guardar la fila sale de la lista (ya no vencida), el foco pasa al aviso", async () => {
    let saved = false;
    const old = task("1", { title: "Antigua", due_at: "2026-11-10T05:00:00Z", is_all_day: true });
    serve((url, init) => {
      if (init?.method === "PATCH") {
        saved = true;
        return { status: 200, body: old };
      }
      return page(...(saved ? [] : [old]));
    });
    show("tasks.view", "tasks.update");
    fireEvent.click(await screen.findByRole("tab", { name: "Vencidas" }));
    fireEvent.click(await screen.findByRole("button", { name: "Editar la tarea Antigua" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Título" }), {
      target: { value: "Nueva" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    await waitFor(() => expect(screen.getByText("Tarea guardada.")).toHaveFocus());
  });

  it("explica un 409 de versión o de tarea cerrada y un 404", async () => {
    const open = task("1", { title: "Antigua" });
    for (const [status, code, text] of [
      [409, "STALE_VERSION", /Otra persona cambió esta tarea/],
      [409, "TASK_CLOSED", /ya está cerrada/],
      [404, "NOT_FOUND", /ya no existe/],
    ] as const) {
      serve((url, init) => (init?.method === "PATCH" ? { status, body: { code } } : page(open)));
      show("tasks.view", "tasks.update");
      fireEvent.click(await screen.findByRole("button", { name: "Editar la tarea Antigua" }));
      fireEvent.click(await screen.findByRole("button", { name: "Guardar tarea" }));
      expect(await screen.findByText(text)).toBeVisible();
      cleanup();
    }
  });
});
