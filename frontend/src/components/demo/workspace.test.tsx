import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import { moduleHref, MODULES } from "./data";
import { ModuleView } from "./module-view";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
    this.open = true;
  };
});

const sidebar = () => screen.getAllByRole("navigation", { name: "ESPACIO DE TRABAJO" })[0]!;

describe("Workspace de la demo", () => {
  it.each(MODULES)("$label: título, migas y módulo activo en la navegación", (module) => {
    render(<ModuleView slug={module.slug} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(module.title);
    expect(screen.getByRole("banner")).toHaveTextContent(`Workspace › ${module.label}`);
    const links = within(sidebar()).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      MODULES.map((item) => moduleHref(item.slug)),
    );
    const current = links.filter((link) => link.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveTextContent(module.label);
    expect(screen.getByRole("main")).not.toBeEmptyDOMElement();
  });

  it("identifica la demo y enlaza el resumen con el pipeline y las tareas", () => {
    render(<ModuleView slug="resumen" />);
    expect(screen.getByText(/Prototipo interactivo · Datos ficticios/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver pipeline" })).toHaveAttribute(
      "href",
      "/demo/workspace/pipeline",
    );
    expect(screen.getByRole("link", { name: "Revisar seguimientos" })).toHaveAttribute(
      "href",
      "/demo/workspace/tareas",
    );
    expect(screen.getByRole("table", { name: "Oportunidades recientes" })).toHaveTextContent(
      "Camila Torres",
    );
  });

  it("filtra las tablas sin distinguir acentos y muestra el estado vacío", () => {
    render(<ModuleView slug="contactos" />);
    const search = screen.getByRole("searchbox", { name: "Buscar en contactos" });
    const rows = () => within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows()).toHaveLength(5);

    fireEvent.change(search, { target: { value: "lucia" } });
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toHaveTextContent("Lucía Vega");

    fireEvent.change(search, { target: { value: "zzz" } });
    expect(rows()[0]).toHaveTextContent("Sin resultados para «zzz» en contactos.");
  });

  it("la acción genérica solo informa: no crea registros", () => {
    render(<ModuleView slug="catalogo" />);
    fireEvent.click(screen.getByRole("button", { name: "Nueva acción" }));
    expect(screen.getByText("Acción de demostración: no crea registros.")).toBeInTheDocument();
  });

  it("el chat del Inbox es local: cambia de conversación, usa la sugerencia y envía", () => {
    render(<ModuleView slug="inbox" />);
    fireEvent.click(screen.getByRole("button", { name: /Diego Mendoza/ }));
    const thread = () =>
      within(screen.getByRole("region", { name: "Conversación con Diego Mendoza" }));
    expect(thread().getAllByRole("listitem")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Usar sugerencia" }));
    const reply = screen.getByRole("textbox", { name: "Respuesta · Sandbox" });
    expect(reply).toHaveValue("Plan profesional anual por USD 5,299.");

    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    const messages = thread().getAllByRole("listitem");
    expect(messages).toHaveLength(3);
    expect(messages[2]).toHaveTextContent("Plan profesional anual por USD 5,299.");
    expect(reply).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: "Enviar" })); // vacío: no añade nada
    expect(thread().getAllByRole("listitem")).toHaveLength(3);
  });

  it("mueve una oportunidad de etapa con el selector y abre su detalle", async () => {
    render(<ModuleView slug="pipeline" />);
    const column = (name: RegExp) => screen.getByRole("region", { name });
    expect(column(/^Nuevo: 1 oportunidad$/)).toHaveTextContent("Camila Torres");

    fireEvent.change(screen.getByRole("combobox", { name: "ETAPA de Camila Torres" }), {
      target: { value: "Ganada" },
    });
    expect(column(/^Nuevo: 0 oportunidades$/)).toHaveTextContent(
      "Sin oportunidades en esta etapa.",
    );
    expect(column(/^Ganada: 2 oportunidades$/)).toHaveTextContent("Camila Torres");
    expect(screen.getByText("Camila Torres pasó a Ganada.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "ETAPA de Camila Torres" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Ver oportunidad de Camila Torres" }));
    const dialog = await screen.findByRole("dialog", { name: "Camila Torres" });
    expect(dialog).toHaveTextContent("Ganada");
    expect(dialog).toHaveTextContent("USD 4,899");
  });
});
