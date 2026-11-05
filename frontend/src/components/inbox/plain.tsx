import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

// Todo texto que escribió otra persona (nombre, mensaje, vista previa) se pinta así: como texto
// plano, nunca como HTML, y aislado (`<bdi dir="auto">`) para que los caracteres de dirección o de
// ancho cero no reordenen lo que lo rodea. Los saltos de línea se conservan y una palabra larga
// se parte en lugar de ensanchar la pantalla. El servidor aún no filtra esos caracteres (F6-02d).
export function Plain({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <bdi dir="auto" className={cn("wrap-anywhere whitespace-pre-wrap", className)}>
      {children}
    </bdi>
  );
}
