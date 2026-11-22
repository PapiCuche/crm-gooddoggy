"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { channelsList, getChannelsListQueryKey } from "@/lib/api/client";
import type { ChannelAccount } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { Plain } from "../inbox/plain";
import { Checklist, SocialChecklist, Standby, StatusMark, useCodeLabel } from "./account-health";
import { type Route, routeOf } from "./channel-routes";
import { ConnectAccount } from "./connect-account";
import { DisconnectAccount } from "./disconnect-account";
import { RotateToken } from "./rotate-token";

// Los canales (F7-07b): las cuentas que devuelve `GET /api/v1/o/{slug}/channels/` (F7-07a), la
// Sandbox, WhatsApp, Messenger e Instagram (F7b-06), con su estado y su salud. Quién las ve lo
// decide la API (`channels.manage`); un 403 solo se explica. Ninguna pantalla enseña un secreto: del token solo
// los últimos cuatro caracteres. Lo que la API dice de una cuenta es texto que no controlamos:
// los nombres van por `Plain`, y los códigos de estado se traducen, nunca se pintan tal cual.
export function ChannelsList() {
  const { organization } = useTenant();
  const slug = organization.slug;
  const list = useRef<CursorListHandle>(null);
  const refresh = () => list.current?.refetch();
  const t = useTranslations("channels");
  // La cuenta desconectada desaparece de la lista (solo vienen las vivas), y también la que otra
  // persona quitó (un 404 al rotar o al desconectar, F7b-06f): se dice aquí, fuera de su tarjeta,
  // para que el aviso sobreviva a la lista nueva. El nombre va aislado (`Plain`), como en la pregunta.
  const [notice, setNotice] = useState<Notice>(null);
  // Un 404: el aviso, la lista nueva (que ya no la trae) y el foco al título, para que no caiga a
  // `<body>` con el formulario o la tarjeta que se van.
  const missing = () => {
    setNotice("missing");
    refresh();
    list.current?.focusHeading();
  };
  const connect = (route: Route) => (
    <ConnectAccount key={route} slug={slug} route={route} onConnected={refresh} />
  );

  return (
    <CursorList<ChannelAccount>
      ref={list}
      section="channels"
      organization={organization.name}
      listKey={[...getChannelsListQueryKey(slug), "pages"]}
      fetchPage={(cursor, signal) =>
        channelsList(slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-start gap-3">
            {(["whatsapp", "messenger", "instagram"] as const).map(connect)}
          </div>
          <p role="status" className={notice ? "text-sm wrap-anywhere" : "sr-only"}>
            {notice === "missing"
              ? t("errors.NOT_FOUND")
              : notice
                ? t.rich("disconnect.done", { ...notice, n: (chunks) => <Plain>{chunks}</Plain> })
                : ""}
          </p>
        </div>
      }
      rowClassName="flex flex-col gap-3"
    >
      {(account) => <Account {...{ slug, account, refresh, missing }} notify={setNotice} />}
    </CursorList>
  );
}

// «missing»: la cuenta ya no existe (404); `{ name }`: esta pantalla la desconectó.
type Notice = "missing" | { name: string } | null;

function Account({
  slug,
  account,
  refresh,
  notify,
  missing,
}: {
  slug: string;
  account: ChannelAccount;
  refresh: () => void;
  notify: (notice: Notice) => void;
  missing: () => void;
}) {
  const t = useTranslations("channels");
  const label = useCodeLabel();
  const format = useFormatter();
  const route = routeOf(account.platform);
  const detail = account.status_detail;
  const checked = (
    <Row label={t("fields.checked")}>
      {account.last_healthcheck_at
        ? format.dateTime(new Date(account.last_healthcheck_at), {
            ...{ day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" },
          })
        : t("fields.neverChecked")}
    </Row>
  );
  const token = (
    <Row label={t("fields.token")} mono>
      {account.token_last_four ? `…${account.token_last_four}` : t("fields.noToken")}
    </Row>
  );
  return (
    <>
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <p className="flex min-w-0 flex-col leading-snug">
          <span className="font-medium wrap-anywhere">
            <Plain>{account.name}</Plain>
          </span>
          <span className="text-muted text-sm">{label("platform", account.platform)}</span>
        </p>
        <StatusMark status={account.status} />
      </div>
      {detail ? <p className="text-sm">{label("detail", detail)}</p> : null}
      {route === "whatsapp" ? (
        <>
          <Checklist account={account} />
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            <Row label={t("fields.phone")} mono>
              {account.external_account_id}
            </Row>
            {account.external_parent_id ? (
              <Row label={t("fields.waba")} mono>
                {account.external_parent_id}
              </Row>
            ) : null}
            {token}
            <Row label={t("fields.quality")}>{label("quality", account.quality_rating)}</Row>
            <Row label={t("fields.tier")}>{label("tier", account.messaging_limit_tier)}</Row>
            {checked}
          </dl>
        </>
      ) : route ? (
        <>
          {account.has_standby_conversations ? <Standby /> : null}
          <SocialChecklist account={account} />
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            {/* Messenger: la página es la cuenta. Instagram: la cuenta y su página vinculada. */}
            <Row label={t("fields.page")} mono>
              {route === "instagram" ? account.external_parent_id : account.external_account_id}
            </Row>
            {route === "instagram" ? (
              <Row label={t("fields.igAccount")} mono>
                {account.external_account_id}
              </Row>
            ) : null}
            {token}
            {checked}
          </dl>
        </>
      ) : account.platform === "SANDBOX" ? (
        <p className="text-muted text-sm">{t("sandbox")}</p>
      ) : null}
      {route && account.status !== "DISCONNECTED" ? (
        <div className="flex flex-col gap-3">
          <RotateToken
            {...{ slug, route, accountId: account.id }}
            onRotated={refresh}
            onMissing={missing}
          />
          <DisconnectAccount
            {...{ slug, route, accountId: account.id, name: account.name }}
            onDisconnected={() => {
              notify({ name: account.name });
              refresh();
            }}
            onMissing={missing}
          />
        </div>
      ) : null}
    </>
  );
}

function Row({
  label,
  mono,
  children,
}: {
  label: string;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className={cn("min-w-0 wrap-anywhere", mono && "font-mono")}>{children}</dd>
    </>
  );
}
