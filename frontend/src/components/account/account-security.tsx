"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { SessionActions } from "@/components/app-shell/session-actions";
import { LINK } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { useAuthMfaStatus } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

import { MfaEnrollment } from "./mfa-enrollment";

const FEW = 3; // códigos de respaldo a partir de los que se avisa

// La seguridad de la cuenta propia (ADR-023): el estado del segundo factor y su alta. Lo que
// dice viene de la API (`GET /auth/mfa/`); la pantalla no decide nada.
export function AccountSecurity() {
  const t = useTranslations();
  const format = useFormatter();
  // Con el alta en curso no se vuelve a preguntar solo: cambiaría la pantalla bajo los pasos.
  const status = useAuthMfaStatus({
    query: { refetchOnWindowFocus: false, refetchOnReconnect: false },
  });
  const [enrolling, setEnrolling] = useState(false);
  // Sin sesión, `Providers` ya lleva al login: aquí solo se evita mostrar un error mientras.
  const expired = status.error instanceof ApiError && status.error.status === 401;

  let body;
  if (status.isPending || status.isRefetching || expired) {
    body = <p className="text-muted">{t("account.loading")}</p>;
  } else if (status.isError) {
    body = (
      <>
        <p role="alert" className="text-danger">
          {t(`errors.api.${apiErrorKey(status.error)}`)}
        </p>
        <Button variant="primary" size="lg" onClick={() => void status.refetch()}>
          {t("account.retry")}
        </Button>
      </>
    );
  } else if (enrolling) {
    body = (
      <MfaEnrollment
        onFinished={() => {
          setEnrolling(false);
          void status.refetch();
        }}
        onCancel={() => setEnrolling(false)}
      />
    );
  } else if (status.data.enabled) {
    const left = status.data.backup_codes_left;
    const since = status.data.enabled_at ? new Date(status.data.enabled_at) : null;
    body = (
      <>
        <p>
          {since
            ? t("account.mfaOn", { date: format.dateTime(since, { dateStyle: "long" }) })
            : t("account.mfaOnUndated")}
        </p>
        <p className={left <= FEW ? "text-danger" : "text-muted"}>
          {t("account.backupLeft", { count: left })}
          {left <= FEW ? ` ${t("account.backupFew")}` : null}
        </p>
      </>
    );
  } else {
    body = (
      <>
        <p className="text-muted">{t("account.mfaOff")}</p>
        <Button variant="primary" size="lg" className="w-full" onClick={() => setEnrolling(true)}>
          {t("account.enable")}
        </Button>
      </>
    );
  }

  return (
    <>
      <section
        aria-labelledby="mfa-title"
        aria-busy={status.isPending}
        className="flex flex-col gap-4"
      >
        <h2 id="mfa-title" className="text-lg font-semibold">
          {t("account.mfaTitle")}
        </h2>
        {body}
      </section>
      <p className="text-sm" hidden={enrolling}>
        <Link href="/o" className={LINK}>
          {t("account.back")}
        </Link>
      </p>
      {enrolling ? null : <SessionActions />}
    </>
  );
}
