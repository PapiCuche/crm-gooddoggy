"use client";

import { createContext, type ReactNode, useContext } from "react";

import type { SelfContext } from "@/lib/api/model";

const TenantContext = createContext<SelfContext | null>(null);

export function TenantProvider({ value, children }: { value: SelfContext; children: ReactNode }) {
  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

// Quién es el usuario en la organización de la ruta y qué puede hacer, según la API
// (`GET /api/v1/o/{slug}/me/`). Solo existe por debajo de `TenantGate`.
export function useTenant(): SelfContext {
  const context = useContext(TenantContext);
  if (!context) throw new Error("useTenant se usa dentro de una ruta de organización");
  return context;
}
