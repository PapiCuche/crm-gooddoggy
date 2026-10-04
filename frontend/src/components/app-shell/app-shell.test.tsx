import { onlineManager, type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { HealthStatus } from "@/components/health-status";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp, renderIntl } from "@/test-utils";

import { visibleItems } from "./navigation";
import { useTenant } from "./tenant-context";
import { TenantGate } from "./tenant-gate";
import { WorkspaceHome } from "./workspace-home";

const router = { replace: vi.fn(), pathname: "/o/acme" };
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => router.pathname,
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

const ME = "GET /api/v1/o/acme/me/";
const LOGOUT = "POST /api/v1/auth/logout/";
const ana: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [
    { code: "owner", name: "Owner" },
    { code: "sales", name: "Ventas" },
  ],
  permissions: [{ code: "users.view", scopes: [] }],
};
const gate = (
  <TenantGate orgSlug="acme">
    <WorkspaceHome />
  </TenantGate>
);
// Como al volver a la pestaña. TanStack avisa a React en una tarea posterior: se la deja pasar.
const refresh = (client: QueryClient) =>
  act(async () => {
    await client.refetchQueries();
    await new Promise((resolve) => setTimeout(resolve));
  });
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

beforeAll(() => {
  // jsdom no implementa la API de <dialog>.
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
});
afterEach(() => {
  router.replace.mockReset();
  router.pathname = "/o/acme";
  onlineManager.setOnline(true);
  vi.unstubAllGlobals();
});

describe("TenantGate", () => {
  it("no pinta nada del workspace hasta que la API responde, y después muestra sus datos", async () => {
    mockApi({ [ME]: { status: 200, body: ana } });
    renderApp(gate);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando tu espacio");
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Hola, Ana.");
    const nav = screen.getByRole("navigation", { name: "Navegación principal" });
    const home = within(nav).getByRole("link", { name: "Inicio" });
    expect(home).toHaveAttribute("href", "/o/acme");
    expect(home).toHaveAttribute("aria-current", "page");
    const aside = screen.getByRole("complementary");
    expect(aside).toHaveTextContent("Acme SAC"); // el nombre que devuelve la API, no la URL
    expect(aside).toHaveTextContent("Ana López");
    expect(aside).toHaveTextContent("Owner · Ventas");
    const main = screen.getByRole("main");
    expect(main).toHaveTextContent("Estás en Acme SAC.");
    const roles = within(main).getAllByRole("listitem");
    expect(roles.map((role) => role.textContent)).toEqual(["Owner", "Ventas"]); // nombres, no códigos
    expect(screen.getByRole("banner")).toHaveTextContent("Workspace › Inicio");
    expect(document.title).toBe("Acme SAC · Good Doggy CRM");
    const skip = screen.getByRole("link", { name: "Saltar al contenido" });
    const target = document.querySelector(skip.getAttribute("href") ?? "");
    expect(target).toBe(screen.getByRole("main"));
    expect(target).toHaveAttribute("tabindex", "-1");
  });

  it("una organización ajena o inexistente es la página 404 de siempre", async () => {
    mockApi({ [ME]: { status: 404, body: { code: "NOT_FOUND" } } });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const failure = new Promise<unknown>((resolve) => {
      window.addEventListener("error", (event) => resolve(event.error), { once: true });
    });
    renderApp(gate);
    expect(await failure).toMatchObject({ message: "NEXT_NOT_FOUND" });
    errors.mockRestore();
  });

  it("sin sesión no enseña un error: el login lo decide el proveedor", async () => {
    const api = mockApi({ [ME]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    renderApp(gate);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
  });

  it("una organización suspendida lo dice y deja cambiar a otra", async () => {
    let reply: { status: number; body: unknown } = { status: 403, body: { code: "ORG_SUSPENDED" } };
    const api = mockApi({ [ME]: () => reply });
    renderApp(gate);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Esta organización está suspendida.",
    );
    expect(screen.getByRole("link", { name: "Cambiar de organización" })).toHaveAttribute(
      "href",
      "/o?elegir",
    );
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    const first = screen.getByRole("button", { name: "Reintentar" });
    const release = hold(api);
    fireEvent.click(first);
    expect(await screen.findByRole("status")).toHaveTextContent("Cargando tu espacio");
    release();
    const second = await screen.findByRole("button", { name: "Reintentar" });
    expect(second).not.toBe(first);
    await waitFor(() => expect(second).toHaveFocus()); // el foco no se queda en ninguna parte
    const unnamed = { ...ana.user, first_name: "", last_name: "" };
    reply = { status: 200, body: { ...ana, user: unnamed, roles: [], permissions: [] } };
    fireEvent.click(second);
    expect(await screen.findByText(/Aún no tienes un rol/)).toBeVisible(); // miembro sin roles
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
    expect(screen.getByRole("complementary")).toHaveTextContent("Sin rol asignado");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Hola\.$/); // sin nombre
    expect(screen.getByRole("complementary")).toHaveTextContent("ana@acme.pe"); // el correo, sí
  });

  it("con el workspace abierto, un fallo pasajero al refrescar no lo cierra; una negativa sí", async () => {
    let reply: { status: number; body: unknown } = { status: 200, body: ana };
    const api = mockApi({ [ME]: () => reply });
    const view = renderApp(gate);
    await screen.findByRole("navigation", { name: "Navegación principal" });
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    await refresh(view.client);
    expect(view.client.getQueryState(["/api/v1/o/acme/me/"])?.status).toBe("error");
    expect(screen.getByRole("main")).toHaveTextContent("Hola, Ana."); // sigue lo último que dijo la API
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    reply = { status: 403, body: { code: "ORG_SUSPENDED" } };
    await refresh(view.client);
    expect(screen.getByRole("alert")).toHaveTextContent("Esta organización está suspendida.");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cerrar sesión" })).toBeInTheDocument(); // sin shell también
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    await refresh(view.client); // tras la negativa, un fallo pasajero no reabre con lo de antes
    expect(screen.getByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    const asked = api.mock.calls.length;
    await act(async () => void fireEvent(window, new Event("visibilitychange"))); // ni pregunta sola
    expect(api).toHaveBeenCalledTimes(asked);
    reply = { status: 200, body: ana };
    const release = hold(api);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Cargando tu espacio"); // se nota
    release();
    expect(await screen.findByRole("main")).toHaveTextContent("Hola, Ana.");
  });

  it("volver a la pestaña vuelve a preguntar con el workspace abierto, no con la tarjeta de error", async () => {
    let reply: { status: number; body: unknown } = { status: 502, body: null };
    const api = mockApi({ [ME]: () => reply });
    const back = () =>
      act(async () => {
        fireEvent(window, new Event("visibilitychange")); // lo que escucha TanStack
        await new Promise((resolve) => setTimeout(resolve));
      });
    renderApp(gate);
    await screen.findByRole("alert"); // tarjeta al entrar: dos intentos y ningún dato
    await back();
    expect(api).toHaveBeenCalledTimes(2);
    reply = { status: 200, body: ana };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await screen.findByRole("main");
    reply = { status: 403, body: { code: "ORG_SUSPENDED" } };
    await back(); // con el workspace abierto sí pregunta, y esta respuesta lo cierra
    await screen.findByRole("alert");
    await back(); // la tarjeta no se desmonta: conserva el foco y los avisos que tenga
    expect(api).toHaveBeenCalledTimes(4);
  });

  it("`useTenant` solo existe por debajo de la guardia", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const Outside = () => <p>{useTenant().organization.name}</p>;
    expect(() => renderIntl(<Outside />)).toThrow("useTenant se usa dentro de");
    errors.mockRestore();
  });

  it("perder la membresía con el workspace abierto es un 404, y volver a entrar pregunta de nuevo", async () => {
    let reply: { status: number; body: unknown } = { status: 200, body: ana };
    const api = mockApi({ [ME]: () => reply });
    const view = renderApp(gate);
    await screen.findByRole("main");
    reply = { status: 404, body: { code: "NOT_FOUND" } };
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const failure = new Promise<unknown>((resolve) => {
      window.addEventListener("error", (event) => resolve(event.error), { once: true });
    });
    void view.client.refetchQueries();
    expect(await failure).toMatchObject({ message: "NEXT_NOT_FOUND" });
    errors.mockRestore();
    // Al salir no queda la respuesta en memoria: la siguiente entrada no decide con ella.
    await waitFor(() => expect(view.client.getQueryCache().getAll()).toEqual([]));
    reply = { status: 200, body: ana };
    renderIntl(<QueryClientProvider client={view.client}>{gate}</QueryClientProvider>);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando tu espacio");
    expect(await screen.findByRole("main")).toHaveTextContent("Hola, Ana.");
    expect(api).toHaveBeenCalledTimes(3);
  });

  it("la dirección con barra final marca la misma entrada", async () => {
    router.pathname = "/o/acme/";
    mockApi({ [ME]: { status: 200, body: ana } });
    renderApp(gate);
    const nav = await screen.findByRole("navigation", { name: "Navegación principal" });
    expect(within(nav).getByRole("link", { name: "Inicio" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("banner")).toHaveTextContent("Workspace › Inicio");
  });
});

describe("shell de la organización", () => {
  it("el menú móvil se abre con su nombre y se cierra al elegir o al pulsar fuera", async () => {
    mockApi({ [ME]: { status: 200, body: ana } });
    renderApp(gate);
    const open = await screen.findByRole("button", { name: "Abrir menú" });
    fireEvent.click(open);
    const menu = screen.getByRole("dialog", { name: "Menú" });
    expect(within(menu).getByRole("navigation", { name: "Navegación del menú" })).toBeVisible();
    const close = within(menu).getByRole("button", { name: "Cerrar menú" });
    expect(close.closest("form")).toHaveAttribute("method", "dialog"); // cierra sin JavaScript
    fireEvent.click(within(menu).getByText("Acme SAC")); // dentro del panel: sigue abierto
    expect(menu).toHaveAttribute("open");
    const destination = within(menu).getByRole("link", { name: "Inicio" });
    destination.addEventListener("click", (event) => event.preventDefault()); // jsdom no navega
    fireEvent.click(destination);
    expect(menu).not.toHaveAttribute("open");
    fireEvent.click(open);
    fireEvent.click(menu); // el fondo
    expect(menu).not.toHaveAttribute("open");
  });

  it("cerrar sesión llama a la API, olvida lo leído y vuelve al login", async () => {
    document.cookie = "csrftoken=" + "t".repeat(32);
    const api = mockApi({ [ME]: { status: 200, body: ana }, [LOGOUT]: { status: 204 } });
    const view = renderApp(gate);
    const aside = await screen.findByRole("complementary");
    expect(within(aside).getByRole("link", { name: "Cambiar de organización" })).toHaveAttribute(
      "href",
      "/o?elegir",
    );
    fireEvent.click(within(aside).getByRole("button", { name: "Cerrar sesión" }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(view.client.getQueryCache().getAll()).toEqual([]); // nada de lo leído sigue en memoria
    const [, init] = api.mock.calls.find(([url]) => String(url).endsWith("/logout/")) ?? [];
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("t".repeat(32));
  });

  it("si la sesión ya no existía, cerrar sesión acaba igual y sin avisos", async () => {
    document.cookie = "csrftoken=" + "t".repeat(32);
    const api = mockApi({
      [ME]: { status: 200, body: ana },
      [LOGOUT]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    renderApp(gate);
    const aside = await screen.findByRole("complementary");
    const logout = within(aside).getByRole("button", { name: "Cerrar sesión" });
    fireEvent.click(logout);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(within(aside).queryByRole("alert")).not.toBeInTheDocument();
    // Hasta que la página cambia sigue ocupado: otra pulsación no cierra dos veces.
    await waitFor(() => expect(logout).toHaveTextContent("Cerrando sesión…"));
    expect(logout).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(logout);
    await new Promise((resolve) => setTimeout(resolve));
    expect(api.mock.calls.filter(([url]) => String(url).endsWith("/logout/"))).toHaveLength(1);
  });

  it("un cierre que falla se dice, deja el foco en el botón y permite reintentar", async () => {
    document.cookie = "csrftoken=" + "t".repeat(32);
    let reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const api = mockApi({ [ME]: { status: 200, body: ana }, [LOGOUT]: () => reply });
    renderApp(gate);
    const aside = await screen.findByRole("complementary");
    const logout = within(aside).getByRole("button", { name: "Cerrar sesión" });
    logout.focus();
    fireEvent.click(logout);
    const alert = await within(aside).findByRole("alert");
    expect(alert).toHaveTextContent("Algo salió mal");
    expect(alert.compareDocumentPosition(logout)).toBe(Node.DOCUMENT_POSITION_FOLLOWING); // encima
    expect(logout).toHaveFocus();
    expect(logout).toHaveAttribute("aria-disabled", "false");
    reply = { status: 403, body: { code: "CSRF_FAILED" } }; // un 403 no es «ya estaba cerrada»
    fireEvent.click(logout);
    await waitFor(() =>
      expect(within(aside).getByRole("alert")).toHaveTextContent("La página caducó"),
    );
    onlineManager.setOnline(false); // el navegador avisó de que no hay red: falla, no espera
    api.mockRejectedValue(new TypeError("Failed to fetch"));
    fireEvent.click(logout);
    await waitFor(() =>
      expect(within(aside).getByRole("alert")).toHaveTextContent("No hay conexión"),
    );
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("el cierre termina aunque el shell se desmonte con la petición en vuelo", async () => {
    document.cookie = "csrftoken=" + "t".repeat(32);
    const api = mockApi({ [ME]: { status: 200, body: ana }, [LOGOUT]: { status: 204 } });
    const view = renderApp(gate);
    const aside = await screen.findByRole("complementary");
    const release = hold(api);
    fireEvent.click(within(aside).getByRole("button", { name: "Cerrar sesión" }));
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
    view.unmount();
    release();
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
  });
});

describe("visibleItems", () => {
  const items = [
    { key: "home" },
    { key: "members", permission: "users.view" },
    { key: "audit", permission: "audit.view" },
  ];

  it("solo deja las entradas sin permiso o con uno que el usuario tiene", () => {
    const keys = (codes: string[]) =>
      visibleItems(
        items,
        codes.map((code) => ({ code, scopes: [] })),
      ).map((item) => item.key);
    expect(keys([])).toEqual(["home"]);
    expect(keys(["users.view"])).toEqual(["home", "members"]);
    expect(keys(["audit.view", "users.view", "otro.permiso"])).toEqual([
      "home",
      "members",
      "audit",
    ]);
  });
});

describe("MobileMenu", () => {
  it("se cierra al pasar a escritorio y deja de escuchar al desmontarse", async () => {
    const listeners = new Set<() => void>();
    const desktop = {
      matches: false,
      addEventListener: (_: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    };
    vi.stubGlobal("matchMedia", () => desktop);
    mockApi({ [ME]: { status: 200, body: ana } });
    const shell = renderApp(gate);
    fireEvent.click(await screen.findByRole("button", { name: "Abrir menú" }));
    const menu = screen.getByRole("dialog", { name: "Menú" });
    listeners.forEach((listener) => listener()); // cambia, pero sigue siendo pantalla pequeña
    expect(menu).toHaveAttribute("open");
    desktop.matches = true;
    listeners.forEach((listener) => listener());
    expect(menu).not.toHaveAttribute("open");
    shell.unmount();
    expect(listeners.size).toBe(0);
    vi.unstubAllGlobals();
  });
});

describe("HealthStatus", () => {
  it.each([
    ["ok", "Operativo"],
    ["fail", "No disponible"],
  ] as const)("%s → %s", (status, text) => {
    renderIntl(<HealthStatus label="Backend" status={status} />);
    expect(screen.getByRole("status")).toHaveTextContent(text);
  });
});
