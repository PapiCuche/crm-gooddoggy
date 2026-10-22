import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AccountSecurity } from "@/components/account/account-security";
import { AuthCard } from "@/components/auth/auth-card";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("account.pageTitle")} · ${t("app.name")}` };
}

// La cuenta es de la persona, no de una organización (como `/o`): fuera de `/o/{slug}`.
export default async function AccountSecurityPage() {
  const t = await getTranslations("account");
  return (
    <AuthCard eyebrow={t("eyebrow")} title={t("title")}>
      <AccountSecurity />
    </AuthCard>
  );
}
