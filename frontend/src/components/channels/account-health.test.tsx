import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ChannelAccount } from "@/lib/api/model";
import { renderIntl } from "@/test-utils";

import { Checklist, StatusMark } from "./account-health";

const account = (changes: Partial<ChannelAccount>) =>
  ({
    status: "PENDING",
    status_detail: null,
    quality_rating: null,
    token_last_four: "ABCD",
    ...changes,
  }) as ChannelAccount;
// Cada paso: su texto y la clase de color de su icono (el icono no es la única señal: va con texto).
const steps = (changes: Partial<ChannelAccount>) => {
  renderIntl(<Checklist account={account(changes)} />);
  const items = within(
    screen.getByRole("list", { name: "Lo que falta para usar la cuenta" }),
  ).getAllByRole("listitem");
  const found = items.map((item) => [
    item.textContent,
    item
      .querySelector("svg")
      ?.getAttribute("class")
      ?.match(/text-\w+/)?.[0],
  ]);
  cleanup();
  return found;
};

afterEach(cleanup);

describe("salud de la cuenta (F7-07d)", () => {
  it("una cuenta nueva sin webhook: token bien, mensaje de Meta y calidad pendientes", () => {
    expect(steps({ status_detail: "WEBHOOK_NOT_SEEN" })).toEqual([
      ["Token válido", "text-success"],
      ["Falta recibir el primer mensaje de Meta", "text-muted"],
      ["Falta la primera comprobación de calidad", "text-muted"],
    ]);
  });

  it("PENDING sin detalle ya vio el webhook y solo espera la primera comprobación", () => {
    expect(steps({ status: "PENDING", status_detail: null })).toEqual([
      ["Token válido", "text-success"],
      ["Mensajes de Meta recibidos", "text-success"],
      ["Falta la primera comprobación de calidad", "text-muted"],
    ]);
  });

  it("conectada con calidad buena: los tres pasos en orden", () => {
    expect(steps({ status: "CONNECTED", quality_rating: "GREEN" }).map(([text]) => text)).toEqual([
      "Token válido",
      "Mensajes de Meta recibidos",
      "Calidad del número buena",
    ]);
  });

  it("calidad media o baja avisa; sin token o con el token invalidado falta el token", () => {
    for (const quality of ["YELLOW", "RED"] as const) {
      expect(
        steps({ status: "DEGRADED", status_detail: "QUALITY_LOW", quality_rating: quality })[2],
      ).toEqual(["La calidad del número bajó", "text-danger"]);
    }
    expect(steps({ status: "ERROR", status_detail: "TOKEN_INVALID" })[0]).toEqual([
      "Falta un token válido",
      "text-danger",
    ]);
    expect(steps({ token_last_four: null })[0]).toEqual(["Falta un token válido", "text-danger"]);
  });

  it("DEGRADED por QUALITY_LOW avisa de la calidad aunque la última comprobación diga buena", () => {
    expect(
      steps({ status: "DEGRADED", status_detail: "QUALITY_LOW", quality_rating: "GREEN" })[2],
    ).toEqual(["La calidad del número bajó", "text-danger"]);
    expect(steps({ status: "DEGRADED", status_detail: "QUALITY_LOW" })[2]).toEqual([
      "La calidad del número bajó",
      "text-danger",
    ]);
  });

  it("calidad media o baja avisa también sin el motivo de la cuenta", () => {
    for (const quality of ["YELLOW", "RED"] as const) {
      expect(steps({ status: "CONNECTED", quality_rating: quality })[2]).toEqual([
        "La calidad del número bajó",
        "text-danger",
      ]);
    }
  });

  it("un estado que la pantalla no conoce no la rompe ni enseña el valor crudo", () => {
    for (const status of ["SUSPENDED", "constructor", "toString"]) {
      renderIntl(<StatusMark status={status as ChannelAccount["status"]} />);
      expect(screen.getByText("Estado desconocido")).toBeVisible();
      expect(document.body.textContent).not.toContain(status);
      cleanup();
    }
  });

  it("cada estado se dice con texto y con su icono", () => {
    const expected = {
      PENDING: ["Pendiente", "text-muted"],
      CONNECTED: ["Conectada", "text-success"],
      DEGRADED: ["Con problemas", "text-danger"],
      DISCONNECTED: ["Desconectada", "text-muted"],
      ERROR: ["Con error", "text-danger"],
    } as const;
    for (const [status, [text, tone]] of Object.entries(expected)) {
      renderIntl(<StatusMark status={status as ChannelAccount["status"]} />);
      expect(screen.getByText(text)).toBeVisible();
      expect(document.querySelector("svg")).toHaveClass(tone);
      cleanup();
    }
  });
});
