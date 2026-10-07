import { useMutation } from "@tanstack/react-query";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/http";
import { renderApp } from "@/test-utils";

import { FieldsForm } from "./fields-form";

type Values = { code: string; name: string };
const FIELDS = [
  { name: "code", label: "Código", max: 20, invalid: "Código no válido", required: "Falta" },
  { name: "name", label: "Nombre", max: 100, invalid: "Nombre no válido" },
] as const;
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));

// Una pantalla mínima con lo que «Crear sucursal» no ejercita: el formulario sigue abierto
// tras guardar, y el error propio es de un campo que no es el primero (o no hay ninguno).
function Screen({
  write,
  own,
  check,
}: {
  write: (values: Values) => Promise<unknown>;
  own?: boolean;
  check?: () => boolean;
}) {
  const save = useMutation<unknown, ApiError, Values>({ mutationFn: write, networkMode: "always" });
  return (
    <FieldsForm
      title="Datos"
      heading={<h2>Datos</h2>}
      className="clase-propia"
      fields={FIELDS}
      write={save}
      send={(values) => save.mutate(values)}
      taken={
        own
          ? (error) =>
              error.code === "NAME_TAKEN"
                ? { field: "name", text: "Repetido" }
                : error.code === "FULL"
                  ? { text: "No cabe" } // sin campo: es del formulario
                  : null
          : undefined
      }
      check={check}
      note={<input type="checkbox" aria-label="Extra" />}
      denied="Sin permiso"
      labels={{ submit: "Guardar", busy: "Guardando…", cancel: "Cancelar" }}
      onCancel={() => {}}
    />
  );
}
function opened(write: (values: Values) => Promise<unknown>, own = false, check?: () => boolean) {
  const view = renderApp(<Screen write={write} own={own} check={check} />);
  const form = screen.getByRole("form", { name: "Datos" });
  fireEvent.input(within(form).getByLabelText("Código"), { target: { value: " a " } });
  return { form, view };
}
// Como una pulsación real: el botón recibe el foco antes del clic.
function send(form: HTMLElement) {
  const button = within(form).getByRole("button", { name: "Guardar" });
  button.focus();
  fireEvent.click(button);
}

describe("FieldsForm", () => {
  it("tras guardar sin cerrarse, la marca se suelta: una escritura por pulsación", async () => {
    const write = vi.fn(async (values: Values) => values);
    const { form } = opened(write);
    expect(form).toHaveClass("clase-propia");
    expect(within(form).getByRole("heading", { name: "Datos" })).toBeVisible();
    for (const times of [1, 2, 3]) {
      send(form);
      fireEvent.submit(form); // con la escritura en curso: no es otra
      await tick(); // la respuesta llega antes de que la pantalla diga «Guardando…»
      expect(write).toHaveBeenCalledTimes(times);
    }
    expect(write.mock.calls[0]![0]).toEqual({ code: "a", name: "" });
  });

  it("el error propio va solo a su campo, con el foco; sin `taken`, al formulario", async () => {
    const write = vi.fn(() => Promise.reject(new ApiError(409, "NAME_TAKEN")));
    const { form, view } = opened(write, true);
    send(form);
    await tick();
    expect(within(form).getByLabelText("Nombre")).toHaveAccessibleDescription("Repetido");
    expect(within(form).getByLabelText("Nombre")).toHaveFocus();
    expect(within(form).getByLabelText("Código")).not.toHaveAttribute("aria-invalid");
    expect(within(form).getAllByRole("alert")).toHaveLength(1);
    view.unmount();
    const plain = opened(write).form;
    send(plain);
    await tick();
    expect(within(plain).getByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(within(plain).getByLabelText("Nombre")).not.toHaveAttribute("aria-invalid");
    expect(within(plain).getByRole("button", { name: "Guardar" })).toHaveFocus(); // no se mueve
  });

  it("un aviso de «falta» retira a la vez los errores de la respuesta anterior", async () => {
    const write = vi.fn(() => Promise.reject(new ApiError(400, "VALIDATION_ERROR", { name: [] })));
    const { form } = opened(write);
    send(form);
    await tick();
    expect(within(form).getByLabelText("Nombre")).toHaveAccessibleDescription("Nombre no válido");
    fireEvent.change(within(form).getByLabelText("Código"), { target: { value: "" } }); // sin `input`
    send(form);
    expect(within(form).getByRole("alert")).toHaveTextContent("Falta"); // el único, ya en esta tarea
    await tick();
    expect(within(form).getByRole("alert")).toHaveTextContent("Falta");
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("un error propio sin campo va al formulario, y un control de `note` lo retira", async () => {
    const write = vi.fn(() => Promise.reject(new ApiError(409, "FULL")));
    const { form } = opened(write, true);
    send(form);
    await tick();
    expect(within(form).getByRole("alert")).toHaveTextContent("No cabe"); // no el genérico
    expect(within(form).getByLabelText("Código")).not.toHaveAttribute("aria-invalid");
    fireEvent.click(within(form).getByRole("checkbox", { name: "Extra" }));
    await tick();
    expect(within(form).queryByRole("alert")).toBeNull();
  });

  it("`check` decide si se envía, y si no, retira el error de la respuesta anterior", async () => {
    let ready = true;
    const write = vi.fn(() => Promise.reject(new ApiError(409, "FULL")));
    const { form } = opened(write, true, () => ready);
    send(form);
    await tick();
    expect(within(form).getByRole("alert")).toHaveTextContent("No cabe");
    ready = false;
    send(form);
    expect(write).toHaveBeenCalledTimes(1); // no se envió
    expect(within(form).queryByRole("alert")).toBeNull(); // y el error de antes no se queda
    ready = true;
    send(form); // en la misma tarea: ni el error ni el reintento esperan a la pantalla
    await tick();
    expect(write).toHaveBeenCalledTimes(2); // la marca no quedó puesta
    fireEvent.input(within(form).getByLabelText("Código"), { target: { value: "" } });
    send(form);
    expect(within(form).getByRole("alert")).toHaveTextContent("Falta"); // los campos, antes
    expect(write).toHaveBeenCalledTimes(2);
  });
});
