"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  automationsPause,
  automationsResume,
  automationsRetrieve,
  getAutomationsRetrieveQueryKey,
} from "@/lib/api/client";
import type { Automations } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { AutomationRuleCard, Result, type Said, WHEN } from "./automation-rule";

const [VIEW, MANAGE, KILL] = ["automations.view", "automations.manage", "automations.kill_switch"];
const OUTLINE = "border-border min-h-11 border sm:min-h-9";

// «Automatizaciones» (F11-08a, ADR-049): el interruptor de la organización y cada regla; el
// historial y el resumen de SIMULATE llegan con F11-08a2 (#1225). Solo lee y cambia la
// configuración: nada de aquí envía mensajes ni toca conversaciones, cotizaciones o ventas. Lo que cada rol ve es comodidad; la API decide:
// `automations.view` lee, `automations.kill_switch` o `.manage` pausan, `.manage` edita y reanuda.
export function AutomationsSettings() {
  const t = useTranslations("automations");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = new Set(permissions.map((grant) => grant.code));
  const [view, manage] = [held.has(VIEW), held.has(MANAGE)];
  const queryClient = useQueryClient();
  const key = getAutomationsRetrieveQueryKey(slug);
  const query = useQuery<Automations, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => automationsRetrieve(slug, { signal }),
    enabled: view,
  });
  const [said, setSaid] = useState<Said>(null);
  const sending = useRef(false);
  const [busy, setBusy] = useState(false);
  const title = useId();
  const heading = useRef<HTMLHeadingElement>(null);

  async function toggle(pause: boolean) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setSaid(null);
    try {
      const value = await (pause ? automationsPause : automationsResume)(slug);
      await queryClient.cancelQueries({ queryKey: key });
      queryClient.setQueryData<Automations>(key, (old) => old && { ...old, ...value });
      setSaid({ text: t(pause ? "pause.done" : "resume.done") });
      setTimeout(() => heading.current?.focus()); // el botón pulsado ya no está
    } catch (error) {
      setSaid({ text: errors(`api.${apiErrorKey(error)}`), alert: true });
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  const status = query.error?.status;
  let body;
  if (!view || status === 403) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (query.data) {
    const data = query.data;
    const switchButton = (pause: boolean) => (
      <Button
        variant={pause ? "primary" : "ghost"}
        className={pause ? "min-h-11 self-start sm:min-h-9" : `${OUTLINE} self-start`}
        aria-disabled={busy}
        onClick={() => void toggle(pause)}
      >
        {t(`${pause ? "pause" : "resume"}.${busy ? "busy" : "open"}`)}
      </Button>
    );
    body = (
      <>
        <section
          aria-labelledby={title}
          className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4"
        >
          <h2
            id={title}
            ref={heading}
            tabIndex={-1}
            className="font-medium focus-visible:outline-none"
          >
            {data.paused
              ? t("paused", {
                  at: data.paused_changed_at
                    ? format.dateTime(new Date(data.paused_changed_at), WHEN)
                    : "—",
                })
              : t("running")}
          </h2>
          {data.global_enabled ? null : <p className="text-sm">{t("globalOff")}</p>}
          {!data.paused ? (
            manage || held.has(KILL) ? (
              switchButton(true)
            ) : null
          ) : manage ? (
            switchButton(false)
          ) : (
            <p className="text-muted text-sm">{t("resume.onlyManage")}</p>
          )}
          <Result said={said} />
        </section>
        {data.rules.map((rule) => (
          <AutomationRuleCard key={rule.key} rule={rule} manage={manage} />
        ))}
      </>
    );
  } else if (query.isError && status !== 401) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {errors(`api.${apiErrorKey(query.error)}`)}
        </p>
        <Button variant="ghost" className={OUTLINE} onClick={() => void query.refetch()}>
          {errors("retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {body}
    </section>
  );
}
