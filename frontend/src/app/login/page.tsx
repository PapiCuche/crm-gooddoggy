import type { Metadata } from "next";
import Link from "next/link";
import { getTranslations } from "next-intl/server";

import { AuthCard, LINK } from "@/components/auth/auth-card";
import { LoginForm } from "@/components/auth/login-form";
import { safeNext } from "@/lib/next-path";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations();
  return { title: `${t("auth.pageTitle")} · ${t("app.name")}` };
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string | string[] }>;
}) {
  const t = await getTranslations("auth");
  const { next } = await searchParams;
  return (
    <AuthCard eyebrow={t("eyebrow")} title={t("title")}>
      <p className="text-muted">{t("intro")}</p>
      <LoginForm next={safeNext(next)} />
      <p className="text-sm">
        <Link href="/recuperar" className={LINK}>
          {t("forgot")}
        </Link>
      </p>
      <p className="text-muted text-sm">{t("help")}</p>
    </AuthCard>
  );
}
