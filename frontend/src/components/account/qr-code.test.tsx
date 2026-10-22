import { render, screen } from "@testing-library/react";
import { encode } from "uqr";
import { describe, expect, it } from "vitest";

import { QrCode } from "./qr-code";

const URI =
  "otpauth://totp/Good%20Doggy%20CRM:ana%40acme.pe?secret=ABCDEFGHIJKLMNOPQRSTUVWXYZ234567&issuer=Good%20Doggy%20CRM";

// Las casillas negras que dibuja el `<path>`, como «x,y».
function drawn(container: HTMLElement) {
  const d = container.querySelector("path")?.getAttribute("d") ?? "";
  return new Set([...d.matchAll(/M(\d+) (\d+)h1v1h-1z/g)].map(([, x, y]) => `${x},${y}`));
}

describe("QrCode", () => {
  it("dibuja exactamente la matriz del código, con margen y nombre accesible", () => {
    const { container } = render(<QrCode value={URI} label="Código QR" />);
    const svg = screen.getByRole("img", { name: "Código QR" });
    const qr = encode(URI, { ecc: "M", border: 4 });
    expect(svg).toHaveAttribute("viewBox", `0 0 ${qr.size} ${qr.size}`);
    const dark = qr.data.flatMap((row, y) => row.flatMap((on, x) => (on ? [`${x},${y}`] : [])));
    expect(drawn(container)).toEqual(new Set(dark));
    // Los tres patrones de posición, tras el margen de 4 módulos.
    const cells = drawn(container);
    const end = qr.size - 5;
    for (const [x, y] of [
      [4, 4],
      [end, 4],
      [4, end],
    ])
      expect(cells.has(`${x},${y}`)).toBe(true);
    // Negro sobre blanco en cualquier tema, y 4 px por módulo.
    expect(container.querySelector("rect")).toHaveAttribute("fill", "#ffffff");
    expect(container.querySelector("path")).toHaveAttribute("fill", "#000000");
    expect(svg).toHaveAttribute("width", String(qr.size * 4));
  });

  it("cambia con el valor", () => {
    const { container, rerender } = render(<QrCode value={URI} label="Código QR" />);
    const before = drawn(container);
    rerender(<QrCode value={URI.replace("ABCD", "QRST")} label="Código QR" />);
    expect(drawn(container)).not.toEqual(before);
  });
});
