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
    // El margen de 4 módulos queda en blanco y los tres patrones de posición están en su sitio.
    const cells = drawn(container);
    for (let i = 0; i < qr.size; i++) {
      for (const edge of [0, 3, qr.size - 1, qr.size - 4]) {
        expect(cells.has(`${i},${edge}`) || cells.has(`${edge},${i}`)).toBe(false);
      }
    }
    const end = qr.size - 5;
    for (const [x, y] of [
      [4, 4],
      [end, 4],
      [4, end],
    ])
      expect(cells.has(`${x},${y}`)).toBe(true);
    expect(svg.textContent).not.toContain("ABCD"); // la clave no queda como texto en el dibujo
  });

  it("cambia con el valor", () => {
    const { container, rerender } = render(<QrCode value={URI} label="Código QR" />);
    const before = drawn(container);
    rerender(<QrCode value={URI.replace("ABCD", "QRST")} label="Código QR" />);
    expect(drawn(container)).not.toEqual(before);
  });
});
