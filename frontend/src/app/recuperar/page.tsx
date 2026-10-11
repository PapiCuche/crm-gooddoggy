import type { Metadata } from "next";
import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { AuthCard, LINK } from "@/components/auth/auth-card";
import { PasswordResetRequest } from "@/components/auth/password-reset-request";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("recovery.pageTitle")} · ${t("app.name")}` };
}

// Página pública (ADR-021 §2): no pide sesión ni la mira.
export default async function PasswordRecoveryPage() {
  const t = await getTranslations("recovery");
  return (
    <AuthCard eyebrow={t("eyebrow")} title={t("title")}>
      <PasswordResetRequest />
      <p className="text-sm">
        <Link href="/login" className={LINK}>
          {t("back")}
        </Link>
      </p>
    </AuthCard>
  );
}
