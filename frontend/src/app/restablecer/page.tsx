import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AuthCard } from "@/components/auth/auth-card";
import { PasswordResetChange } from "@/components/auth/password-reset-change";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("recovery.changePageTitle")} · ${t("app.name")}` };
}

// Página pública (ADR-021 §4 y §5), en la ruta exacta que compone la tarea que envía el enlace.
// El servidor no conoce el secreto: va en el fragmento, y lo lee el navegador.
export default async function PasswordResetPage() {
  const t = await getTranslations("recovery");
  return (
    <AuthCard eyebrow={t("eyebrow")} title={t("changeTitle")}>
      <PasswordResetChange />
    </AuthCard>
  );
}
