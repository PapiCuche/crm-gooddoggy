import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import { Landing } from "./landing";

beforeAll(() => {
  // jsdom no implementa <dialog>.showModal(); basta con reflejar el atributo `open`.
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
    this.open = true;
  };
});

describe("Landing de la demo", () => {
  it("enlaza las acciones del prototipo con los módulos del workspace", () => {
    render(<Landing />);
    expect(screen.getByRole("heading", { level: 1, name: "GOOD DOGGY" })).toBeInTheDocument();
    const href = (name: string) => screen.getByRole("link", { name }).getAttribute("href");
    expect(href("Explorar el CRM")).toBe("/demo/workspace");
    expect(href("Abrir Inbox")).toBe("/demo/workspace/inbox");
    expect(href("Entrar a la demostración")).toBe("/demo/workspace");
    expect(href("Inbox")).toBe("/demo/workspace/inbox");
    expect(href("Propuestas")).toBe("/demo/workspace/cotizaciones");
    expect(href("Copiloto IA")).toBe("/demo/workspace/agentes-ia");
    expect(href("Explorar módulo: OPORTUNIDADES")).toBe("/demo/workspace/pipeline");
  });

  it("navega por anclas a las secciones y usa la fotografía del Figma con texto alternativo", () => {
    render(<Landing />);
    const nav = screen.getByRole("navigation", { name: "Secciones" });
    expect(within(nav).getByRole("link", { name: "Funcionalidades" })).toHaveAttribute(
      "href",
      "#funcionalidades",
    );
    expect(document.getElementById("funcionalidades")).not.toBeNull();
    expect(document.getElementById("equipo")).not.toBeNull();
    expect(screen.getByRole("img")).toHaveAccessibleName(/equipo/);
  });

  it("cambia la vista previa con clic y con las flechas del teclado", () => {
    render(<Landing />);
    const tab = (name: string) => screen.getByRole("tab", { name });
    expect(tab("Inbox")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Camila Torres · hace 2 min");

    fireEvent.click(tab("Pipeline"));
    expect(tab("Pipeline")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Seguimiento");

    fireEvent.keyDown(tab("Pipeline"), { key: "ArrowRight" });
    expect(tab("Copiloto IA")).toHaveAttribute("aria-selected", "true");
    expect(tab("Copiloto IA")).toHaveFocus();
    expect(screen.getByRole("link", { name: "Revisar sugerencia" })).toHaveAttribute(
      "href",
      "/demo/workspace/agentes-ia",
    );
    fireEvent.keyDown(tab("Copiloto IA"), { key: "ArrowRight" });
    expect(tab("Inbox")).toHaveAttribute("aria-selected", "true");
  });

  it("abre el acceso simulado, sin campos de credenciales", async () => {
    render(<Landing />);
    fireEvent.click(screen.getByRole("button", { name: "Ingresar demo" }));
    const dialog = await screen.findByRole("dialog", { name: "Tu espacio te espera." });
    expect(dialog).toHaveAttribute("open");
    expect(within(dialog).getByRole("link", { name: "Continuar a la demo" })).toHaveAttribute(
      "href",
      "/demo/workspace",
    );
    expect(dialog.querySelector("input")).toBeNull();
  });
});
