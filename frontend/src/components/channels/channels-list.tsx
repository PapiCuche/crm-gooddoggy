"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useRef } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { channelsList, getChannelsListQueryKey } from "@/lib/api/client";
import type { ChannelAccount } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { Plain } from "../inbox/plain";
import { Checklist, StatusMark, useCodeLabel } from "./account-health";
import { ConnectAccount } from "./connect-account";
import { DisconnectAccount } from "./disconnect-account";
import { RotateToken } from "./rotate-token";

// Los canales (F7-07b): las cuentas que devuelve `GET /api/v1/o/{slug}/channels/` (F7-07a), la
// Sandbox y las de WhatsApp, con su estado y su salud. Quién las ve lo decide la API
// (`channels.manage`); un 403 solo se explica. Ninguna pantalla enseña un secreto: del token solo
// los últimos cuatro caracteres. Lo que la API dice de una cuenta es texto que no controlamos:
// los nombres van por `Plain`, y los códigos de estado se traducen, nunca se pintan tal cual.
export function ChannelsList() {
  const { organization } = useTenant();
  const slug = organization.slug;
  const list = useRef<CursorListHandle>(null);
  const refresh = () => list.current?.refetch();

  return (
    <CursorList<ChannelAccount>
      ref={list}
      section="channels"
      organization={organization.name}
      listKey={[...getChannelsListQueryKey(slug), "pages"]}
      fetchPage={(cursor, signal) =>
        channelsList(slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={<ConnectAccount slug={slug} onConnected={refresh} />}
      rowClassName="flex flex-col gap-3"
    >
      {(account) => <Account slug={slug} account={account} refresh={refresh} />}
    </CursorList>
  );
}

function Account({
  slug,
  account,
  refresh,
}: {
  slug: string;
  account: ChannelAccount;
  refresh: () => void;
}) {
  const t = useTranslations("channels");
  const label = useCodeLabel();
  const format = useFormatter();
  const whatsapp = account.platform === "WHATSAPP";
  const detail = account.status_detail;
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
      {whatsapp ? (
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
            <Row label={t("fields.token")} mono>
              {account.token_last_four ? `…${account.token_last_four}` : t("fields.noToken")}
            </Row>
            <Row label={t("fields.quality")}>{label("quality", account.quality_rating)}</Row>
            <Row label={t("fields.tier")}>{label("tier", account.messaging_limit_tier)}</Row>
            <Row label={t("fields.checked")}>
              {account.last_healthcheck_at
                ? format.dateTime(new Date(account.last_healthcheck_at), {
                    ...{ day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" },
                  })
                : t("fields.neverChecked")}
            </Row>
          </dl>
          {account.status !== "DISCONNECTED" ? (
            <div className="flex flex-col gap-3">
              <RotateToken slug={slug} accountId={account.id} onRotated={refresh} />
              <DisconnectAccount
                slug={slug}
                accountId={account.id}
                name={account.name}
                onDisconnected={refresh}
              />
            </div>
          ) : null}
        </>
      ) : account.platform === "SANDBOX" ? (
        <p className="text-muted text-sm">{t("sandbox")}</p>
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
