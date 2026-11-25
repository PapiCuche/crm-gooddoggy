"use client";

import { createContext, type ReactNode, useContext } from "react";

import { DraftsProvider } from "@/components/inbox/drafts";
import type { SelfContext } from "@/lib/api/model";

const TenantContext = createContext<SelfContext | null>(null);

// Los borradores del Inbox (UX-INBOX-04): un almacén por organización y persona; salir lo vacía.
export function TenantProvider({ value, children }: { value: SelfContext; children: ReactNode }) {
  const { id } = value.organization;
  return (
    <TenantContext.Provider value={value}>
      <DraftsProvider key={`${id}/${value.user.id}`} organization={id}>
        {children}
      </DraftsProvider>
    </TenantContext.Provider>
  );
}

// Quién es el usuario en la organización de la ruta y qué puede hacer, según la API
// (`GET /api/v1/o/{slug}/me/`). Solo existe por debajo de `TenantGate`.
export function useTenant(): SelfContext {
  const context = useContext(TenantContext);
  if (!context) throw new Error("useTenant se usa dentro de una ruta de organización");
  return context;
}
