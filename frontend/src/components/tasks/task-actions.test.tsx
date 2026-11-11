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

import type { Task } from "@/lib/api/model";

import { BASE, NOW, page, serve, show, task, type Reply } from "./test-support";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const MEMBERS = "/api/v1/o/acme/members/";
const people = {
  status: 200,
  body: {
    results: [
      {
        id: "m2",
        status: "ACTIVE",
        user: { id: "u2", email: "l@acme.pe", first_name: "Luis", last_name: "Ramos" },
      },
    ],
    next: null,
  },
};
// El servidor de la prueba: la tarea cambia de estado cuando se hace la acción, como en la API.
function world(action: (id: string, init: RequestInit) => Reply, initial: Partial<Task> = {}) {
  const state = { task: task("1", { title: "Llamar", ...initial }) };
  const calls = serve((url, init) => {
    if (url.pathname === MEMBERS) return people;
    if (init?.method === "POST") {
      const reply = action(state.task.id, init);
      if (reply.status < 300) state.task = reply.body as Task;
      return reply;
    }
    return page(state.task);
  });
  const posts = () =>
    calls
      .filter(({ init }) => init?.method === "POST")
      .map(({ url, init }) => [url.pathname, JSON.parse(String(init!.body))]);
  return { state, posts };
}
const changed = (changes: Partial<Task>): Reply => ({
  status: 200,
  body: task("1", { title: "Llamar", ...changes }),
});
const names = () =>
  screen.queryAllByRole("button").map((button) => button.getAttribute("aria-label") ?? "");
const labelled = (name: string) => screen.findByRole("button", { name });

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("acciones de una tarea (F5-08c)", () => {
  it("cada acción sale con su permiso y según el estado de la tarea", async () => {
    world(() => ({ status: 500 }));
    show("tasks.view", "tasks.update");
    await labelled("Completar la tarea Llamar");
    expect(names().filter((name) => /Llamar$/.test(name))).toEqual([
      "Editar la tarea Llamar",
      "Completar la tarea Llamar",
      "Cancelar la tarea Llamar",
    ]);
    cleanup();
    show("tasks.view", "tasks.update", "tasks.assign");
    expect(await labelled("Reasignar la tarea Llamar")).toBeVisible();
    cleanup();
    show("tasks.view", "tasks.assign");
    await labelled("Reasignar la tarea Llamar");
    expect(names().filter((name) => /Llamar$/.test(name))).toEqual(["Reasignar la tarea Llamar"]);
    cleanup();
    world(() => ({ status: 500 }), { status: "DONE" });
    show("tasks.view", "tasks.update", "tasks.assign");
    await labelled("Reabrir la tarea Llamar");
    expect(names().filter((name) => /Llamar$/.test(name))).toEqual(["Reabrir la tarea Llamar"]);
    cleanup();
    world(() => ({ status: 500 }));
    show("tasks.view");
    await screen.findByText("Llamar");
    expect(names().filter((name) => /Llamar$/.test(name))).toEqual([]);
  });

  it("completa con la versión leída, una sola vez, y la lista enseña el nuevo estado", async () => {
    const { posts } = world(() => changed({ status: "DONE", version: 3 }));
    show("tasks.view", "tasks.update");
    const complete = await labelled("Completar la tarea Llamar");
    fireEvent.click(complete);
    fireEvent.click(complete);
    expect(await screen.findByText("Tarea completada.")).toBeVisible();
    expect(await screen.findByText("Completada")).toBeVisible();
    expect(posts()).toEqual([[`${BASE}1/complete/`, { version: 2 }]]);
    // y se puede volver a abrir
    fireEvent.click(await labelled("Reabrir la tarea Llamar"));
    expect(await screen.findByText("Tarea reabierta.")).toBeVisible();
  });

  it("reabre con la versión leída", async () => {
    const { posts } = world(() => changed({ status: "PENDING", version: 4 }), {
      status: "CANCELLED",
      version: 3,
    });
    show("tasks.view", "tasks.update");
    fireEvent.click(await labelled("Reabrir la tarea Llamar"));
    expect(await screen.findByText("Tarea reabierta.")).toBeVisible();
    expect(posts()).toEqual([[`${BASE}1/reopen/`, { version: 3 }]]);
  });

  it("cancela con motivo o sin él; Escape cierra y el foco vuelve a su botón", async () => {
    const { posts } = world(() => changed({ status: "CANCELLED" }));
    show("tasks.view", "tasks.update");
    fireEvent.click(await labelled("Cancelar la tarea Llamar"));
    const form = await screen.findByRole("form", { name: "Cancelar la tarea" });
    await waitFor(() =>
      expect(within(form).getByRole("textbox", { name: /Motivo/ })).toHaveFocus(),
    );
    fireEvent.keyDown(form, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("form", { name: "Cancelar la tarea" })).toBeNull(),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cancelar la tarea Llamar" })).toHaveFocus(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancelar la tarea Llamar" }));
    fireEvent.change(await screen.findByRole("textbox", { name: /Motivo/ }), {
      target: { value: "  Ya no la quiere  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sí, cancelar la tarea" }));
    expect(await screen.findByText("Tarea cancelada.")).toBeVisible();
    expect(posts()).toEqual([[`${BASE}1/cancel/`, { version: 2, reason: "Ya no la quiere" }]]);
    cleanup();
    const sin = world(() => changed({ status: "CANCELLED" }));
    show("tasks.view", "tasks.update");
    fireEvent.click(await labelled("Cancelar la tarea Llamar"));
    fireEvent.click(await screen.findByRole("button", { name: "Sí, cancelar la tarea" }));
    await screen.findByText("Tarea cancelada.");
    expect(sin.posts()[0]![1]).toEqual({ version: 2 });
  });

  it("reasigna a quien se elige entre yo y el directorio", async () => {
    const { posts } = world(() => changed({ assigned_user_id: "u2", version: 3 }));
    show("tasks.view", "tasks.assign", "users.view");
    fireEvent.click(await labelled("Reasignar la tarea Llamar"));
    const form = await screen.findByRole("form", { name: "Reasignar la tarea" });
    const select = within(form).getByRole("combobox", { name: "Responsable" });
    await waitFor(() =>
      expect(within(select).getByRole("option", { name: "Luis Ramos" })).toBeVisible(),
    );
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Yo", "Luis Ramos"]);
    fireEvent.change(select, { target: { value: "u2" } });
    fireEvent.click(within(form).getByRole("button", { name: "Reasignar la tarea" }));
    expect(await screen.findByText("Tarea reasignada.")).toBeVisible();
    expect(posts()).toEqual([[`${BASE}1/assign/`, { version: 2, assigned_user_id: "u2" }]]);
  });

  it("explica cada error de la API y deja volver a intentar", async () => {
    const cases: [string, Reply, RegExp][] = [
      [
        "complete",
        { status: 409, body: { code: "STALE_VERSION" } },
        /Otra persona cambió esta tarea/,
      ],
      ["complete", { status: 409, body: { code: "TASK_CLOSED" } }, /ya está cerrada/],
      ["complete", { status: 404, body: { code: "NOT_FOUND" } }, /ya no existe/],
      [
        "complete",
        { status: 403, body: { code: "PERMISSION_DENIED" } },
        /No tienes permiso para hacer esto/,
      ],
    ];
    for (const [, reply, text] of cases) {
      world(() => reply);
      show("tasks.view", "tasks.update");
      fireEvent.click(await labelled("Completar la tarea Llamar"));
      expect(await screen.findByText(text)).toBeVisible();
      cleanup();
    }
    world(() => ({ status: 409, body: { code: "TASK_OPEN" } }), { status: "DONE" });
    show("tasks.view", "tasks.update");
    fireEvent.click(await labelled("Reabrir la tarea Llamar"));
    expect(await screen.findByText(/ya está abierta/)).toBeVisible();
    cleanup();
    world(() => ({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { assigned_user_id: ["x"] } },
    }));
    show("tasks.view", "tasks.assign");
    fireEvent.click(await labelled("Reasignar la tarea Llamar"));
    fireEvent.click(await screen.findByRole("button", { name: "Reasignar la tarea" }));
    expect(await screen.findByText(/ya no es miembro activo/)).toBeVisible();
    cleanup();
    world(() => ({ status: 400, body: { code: "VALIDATION_ERROR", fields: { reason: ["x"] } } }));
    show("tasks.view", "tasks.update");
    fireEvent.click(await labelled("Cancelar la tarea Llamar"));
    fireEvent.click(await screen.findByRole("button", { name: "Sí, cancelar la tarea" }));
    expect(await screen.findByText(/El motivo no es válido/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Sí, cancelar la tarea" })).toHaveAttribute(
      "aria-disabled",
      "false",
    );
  });

  it("sin directorio, reasignar ofrece solo a uno mismo y a quien la tiene, sin nombrar a nadie más", async () => {
    world(() => changed({}), { assigned_user_id: "u3" });
    show("tasks.view", "tasks.assign"); // sin `users.view`: no se lee el directorio
    fireEvent.click(await labelled("Reasignar la tarea Llamar"));
    const form = await screen.findByRole("form", { name: "Reasignar la tarea" });
    const select = within(form).getByRole("combobox", { name: "Responsable" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((one) => one.textContent),
    ).toEqual(["Yo", "Otro miembro"]);
    expect(select).toHaveValue("u3"); // por omisión, quien la tiene
  });

  it("un 409 o un 404 de una acción de un clic vuelve a pedir la lista y lo dice", async () => {
    for (const [reply, text] of [
      [{ status: 409, body: { code: "STALE_VERSION" } }, /Ya actualizamos la lista/],
      [{ status: 409, body: { code: "TASK_CLOSED" } }, /Ya actualizamos la lista/],
      [{ status: 404, body: { code: "NOT_FOUND" } }, /ya no existe/],
    ] as const) {
      const calls = serve((url, init) =>
        init?.method === "POST"
          ? reply
          : url.pathname === MEMBERS
            ? people
            : page(task("1", { title: "Llamar" })),
      );
      const reads = () =>
        calls.filter(
          ({ url, init }) => url.pathname === BASE && (!init?.method || init.method === "GET"),
        ).length;
      show("tasks.view", "tasks.update");
      fireEvent.click(await labelled("Completar la tarea Llamar"));
      expect(await screen.findByText(text)).toBeVisible();
      await waitFor(() => expect(reads()).toBeGreaterThan(1)); // la lista se volvió a pedir
      cleanup();
    }
  });

  it("cancelar y reasignar mandan la versión leída al abrir el formulario, no la que traiga la lista después", async () => {
    const { state, posts } = world(() => ({ status: 409, body: { code: "STALE_VERSION" } }));
    const { client } = show("tasks.view", "tasks.update", "tasks.assign");
    for (const [open, confirm, route] of [
      ["Cancelar la tarea Llamar", "Sí, cancelar la tarea", "cancel"],
      ["Reasignar la tarea Llamar", "Reasignar la tarea", "assign"],
    ] as const) {
      fireEvent.click(await labelled(open));
      const form = await screen.findByRole("form", { name: /la tarea$/ });
      const next = state.task.version + 1; // otra persona la cambió y la lista se vuelve a pedir
      state.task = { ...state.task, version: next, description: `v${next}` };
      await act(() => client.invalidateQueries());
      await screen.findByText(`v${next}`); // la lista ya trae la versión nueva
      fireEvent.click(within(form).getByRole("button", { name: confirm }));
      await screen.findByText(/Otra persona cambió esta tarea/);
      expect(posts().at(-1)).toEqual([
        `${BASE}1/${route}/`,
        expect.objectContaining({ version: state.task.version - 1 }),
      ]);
      fireEvent.click(within(form).getByRole("button", { name: "Volver" }));
    }
  });

  it("si la fila sale de la lista al completarla, el foco pasa al aviso", async () => {
    let done = false;
    const calls = serve((url, init) => {
      if (init?.method === "POST") {
        done = true;
        return changed({ status: "DONE" });
      }
      return page(
        ...(done
          ? []
          : [task("1", { title: "Llamar", due_at: "2026-11-10T05:00:00Z", is_all_day: true })]),
      );
    });
    show("tasks.view", "tasks.update");
    fireEvent.click(await screen.findByRole("tab", { name: "Vencidas" }));
    fireEvent.click(await labelled("Completar la tarea Llamar"));
    await waitFor(() => expect(screen.getByText("Tarea completada.")).toHaveFocus());
    expect(screen.queryByText("Llamar")).toBeNull();
    expect(calls.length).toBeGreaterThan(2);
  });
});
