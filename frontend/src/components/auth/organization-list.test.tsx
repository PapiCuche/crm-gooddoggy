import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { mockApi, renderApp } from "@/test-utils";

import { OrganizationList } from "./organization-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const MINE = "GET /api/v1/me/organizations/";
const LOGOUT = "POST /api/v1/auth/logout/";
const acme = { id: "1", slug: "acme", name: "Acme SAC" };
const norte = { id: "2", slug: "acme-norte", name: "Acme Norte" };

afterEach(() => {
  router.replace.mockReset();
  vi.unstubAllGlobals();
});

describe("OrganizationList", () => {
  it("lista las organizaciones que devuelve la API, cada una con su enlace", async () => {
    mockApi({ [MINE]: { status: 200, body: [acme, norte] } });
    renderApp(<OrganizationList choose={false} />);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando");
    expect(await screen.findByRole("link", { name: /Acme SAC/ })).toHaveAttribute(
      "href",
      "/o/acme",
    );
    const norteLink = screen.getByRole("link", { name: /Acme Norte/ });
    expect(norteLink).toHaveAttribute("href", "/o/acme-norte");
    expect(norteLink).toHaveTextContent("acme-norte"); // el slug distingue nombres parecidos
    expect(norteLink).toHaveAccessibleName(/^Acme Norte\s*acme-norte$/); // sin la flecha
    expect(screen.getByRole("status")).toHaveTextContent("2 organizaciones."); // se anuncia
    expect(document.body).toHaveFocus(); // la primera carga no mueve el foco
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("con una sola entra directamente, salvo que se pida elegir", async () => {
    mockApi({ [MINE]: { status: 200, body: [acme] } });
    const first = renderApp(<OrganizationList choose={false} />);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/o/acme"));
    expect(screen.queryByRole("link")).not.toBeInTheDocument(); // ni la lista ni el cierre de
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // sesión asoman antes de entrar
    first.unmount();
    router.replace.mockReset();
    renderApp(<OrganizationList choose />);
    expect(await screen.findByRole("link", { name: /Acme SAC/ })).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("sin organizaciones lo explica, sin inventar ninguna", async () => {
    mockApi({ [MINE]: { status: 200, body: [] } });
    renderApp(<OrganizationList choose={false} />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Aún no perteneces a ninguna"),
    );
    expect(screen.getByText(/Pide a quien administra la tuya/)).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("quien no tiene organizaciones también puede cerrar sesión", async () => {
    document.cookie = "csrftoken=" + "t".repeat(32);
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({ [MINE]: { status: 200, body: [] }, [LOGOUT]: () => reply });
    const view = renderApp(<OrganizationList choose={false} />);
    const logout = await screen.findByRole("button", { name: "Cerrar sesión" });
    expect(screen.queryByRole("link", { name: "Cambiar de organización" })).not.toBeInTheDocument();
    fireEvent.click(logout);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal"); // un fallo se dice
    expect(router.replace).not.toHaveBeenCalled();
    reply = { status: 204 };
    fireEvent.click(logout);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(view.client.getQueryCache().getAll()).toEqual([]); // nada de lo leído sigue en memoria
    const [, init] = api.mock.calls.findLast(([url]) => String(url).endsWith("/logout/")) ?? [];
    expect(new Headers(init?.headers).get("X-CSRFToken")).toBe("t".repeat(32));
  });

  it("el cierre de sesión está con la lista y con el error, no mientras carga", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({ [MINE]: () => reply });
    renderApp(<OrganizationList choose />);
    expect(screen.queryByRole("button", { name: "Cerrar sesión" })).not.toBeInTheDocument();
    await screen.findByRole("alert");
    const logout = screen.getByRole("button", { name: "Cerrar sesión" });
    logout.focus();
    const asked = api.mock.calls.length;
    for (const type of ["visibilitychange", "offline", "online"])
      fireEvent(window, new Event(type));
    await new Promise((resolve) => setTimeout(resolve));
    expect(api).toHaveBeenCalledTimes(asked); // con el error en pantalla no se pide nada solo
    expect(logout).toHaveFocus(); // y el botón sigue siendo el mismo, con su foco
    reply = { status: 200, body: [acme, norte] };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await screen.findByRole("link", { name: /Acme Norte/ });
    expect(screen.getByRole("button", { name: "Cerrar sesión" })).toBeVisible();
  });

  it("sin sesión no muestra un error: el login lo decide el proveedor", async () => {
    const api = mockApi({ [MINE]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    renderApp(<OrganizationList choose={false} />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1)); // un 401 no se reintenta
    expect(await screen.findByRole("status")).toHaveTextContent("Cargando");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // sin sesión no hay qué cerrar
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("un fallo se puede reintentar, y el foco no se pierde por el camino", async () => {
    let reply: { status: number; body: unknown } = {
      status: 503,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({ [MINE]: () => reply });
    const { container } = renderApp(<OrganizationList choose={false} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(api).toHaveBeenCalledTimes(2); // un reintento automático de un 5xx
    reply = { status: 403, body: { code: "PERMISSION_DENIED" } };
    const first = screen.getByRole("button", { name: "Reintentar" });
    first.focus();
    fireEvent.click(first);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("No tienes permiso"));
    const second = screen.getByRole("button", { name: "Reintentar" });
    await waitFor(() => expect(second).toHaveFocus()); // aunque el botón se vuelva a montar
    reply = { status: 200, body: [acme, norte] };
    fireEvent.click(second);
    expect(await screen.findByRole("link", { name: /Acme Norte/ })).toBeInTheDocument();
    await waitFor(() => expect(container.firstElementChild).toHaveFocus()); // lo que llegó
  });
});
