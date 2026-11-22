import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AuthCard } from "@/components/auth/auth-card";
import { InvitationAccept } from "@/components/auth/invitation-accept";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("invitationAccept.pageTitle")} · ${t("app.name")}` };
}

// Página pública (ADR-020 §4 y §5), en la ruta que compondrá el correo de la invitación.
// El servidor no conoce el secreto: va en el fragmento, y lo lee el navegador.
export default async function InvitationPage() {
  const t = await getTranslations("invitationAccept");
  return (
    <AuthCard eyebrow={t("eyebrow")} title={t("title")}>
      <InvitationAccept />
    </AuthCard>
  );
}
