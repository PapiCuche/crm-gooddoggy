import { useMemo } from "react";
import { encode } from "uqr";

// Un código QR de `value` (ADR-025): `uqr` solo calcula la matriz y React la dibuja; no se
// inserta HTML generado. Negro sobre blanco en los dos temas, con el margen de 4 módulos de la
// norma, que es lo que esperan los lectores.
export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, path } = useMemo(() => {
    const qr = encode(value, { ecc: "M", border: 4 });
    const dark = qr.data.flatMap((row, y) => row.map((on, x) => (on ? `M${x} ${y}h1v1h-1z` : "")));
    return { size: qr.size, path: dark.join("") };
  }, [value]);
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${size} ${size}`}
      shapeRendering="crispEdges"
      className="border-border size-48 rounded-[10px] border"
    >
      <rect width={size} height={size} fill="#ffffff" />
      <path d={path} fill="#000000" />
    </svg>
  );
}
