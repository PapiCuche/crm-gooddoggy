import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { TenantGate } from "@/components/app-shell/tenant-gate";

const ORG_SLUG = /^[-a-zA-Z0-9_]{1,63}$/;

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("shell.workspace")} · ${t("app.name")}` }; // el shell pone la organización
}

// Rutas de tenant (D3): el slug solo selecciona; la API autoriza (`TenantGate`).
export default async function TenantLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ orgSlug: string }>;
}) {
  const { orgSlug } = await params;
  if (!ORG_SLUG.test(orgSlug)) notFound(); // mismo patrón que el backend (TenantResolutionMiddleware)
  return <TenantGate orgSlug={orgSlug}>{children}</TenantGate>;
}
