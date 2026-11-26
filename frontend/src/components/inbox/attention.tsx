"use client";

import { useQuery } from "@tanstack/react-query";
import { Bell, BellOff, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  channelsList,
  conversationsUnansweredCount,
  getChannelsListQueryKey,
  getConversationsListQueryKey,
} from "@/lib/api/client";
import type {
  ChannelAccount,
  ConversationsUnansweredCountBox,
  ConversationsUnansweredCountPlatform,
} from "@/lib/api/model";

import { Plain } from "./plain";

// Lo que ayuda a atender rápido (F6-09): cuántas esperan respuesta, el aviso del navegador y las
// cuentas de canal que no pueden atender. La red de cada conversación está en `channel-identity`.

// Cuántas conversaciones abiertas esperan respuesta (lo último lo dijo el cliente), las que la API
// deja ver (`conversations.view` y su alcance) en esa bandeja y esa red (UX-INBOX-02). Cuelga de la
// clave de la lista: lo que la relee (responder, asignar, un evento del tiempo real) también relee
// el número.
export function useUnanswered(
  {
    box = "all",
    platform = "",
  }: {
    box?: ConversationsUnansweredCountBox;
    platform?: ConversationsUnansweredCountPlatform | "";
  },
  enabled = true,
) {
  const slug = useTenant().organization.slug;
  const params = { ...(box === "all" ? {} : { box }), ...(platform ? { platform } : {}) };
  const count = useQuery({
    queryKey: [...getConversationsListQueryKey(slug), "unanswered", box, platform],
    queryFn: ({ signal }) => conversationsUnansweredCount(slug, params, { signal }),
    enabled,
    gcTime: 0, // como la lista: al salir de la organización no queda nada suyo en memoria
  });
  return count.data?.count;
}

// El número del menú «Inbox»: nada si no hay ninguna o la lectura falla.
export function UnansweredCount() {
  const t = useTranslations("inbox");
  const count = useUnanswered({});
  if (!count) return null;
  return (
    <>
      <span aria-hidden className="bg-danger text-surface ml-auto rounded-full px-2 text-xs">
        {count > 99 ? "99+" : count}
      </span>
      <span className="sr-only">, {t("unanswered.count", { count })}</span>
    </>
  );
}

// El aviso del navegador: solo si quien atiende lo enciende con un clic (el permiso se pide ahí,
// nunca al cargar) y la pestaña está oculta. Dice «Nuevo mensaje» y nada del cliente.
const NOTIFY = "inbox.notify";
const supported = () => typeof window !== "undefined" && "Notification" in window;
function wanted(): boolean {
  try {
    return localStorage.getItem(NOTIFY) === "on";
  } catch {
    return false;
  }
}
export function notifyNewMessage(title: string) {
  if (!supported() || Notification.permission !== "granted" || !wanted()) return;
  if (document.visibilityState !== "hidden") return;
  try {
    // `renotify`: cada mensaje vuelve a avisar aunque el anterior siga en pantalla (mismo `tag`).
    const shown = new Notification(title, { tag: NOTIFY, renotify: true } as NotificationOptions);
    shown.onclick = () => window.focus();
  } catch {} // Chrome en Android no deja construirla (`Illegal constructor`): sin aviso, nada más
}
export function NotifyToggle() {
  const t = useTranslations("inbox.notify");
  const [on, setOn] = useState(
    () => supported() && Notification.permission === "granted" && wanted(),
  );
  const [blocked, setBlocked] = useState(() => supported() && Notification.permission === "denied");
  if (!supported()) return null;
  if (blocked) return <p className="text-muted text-sm">{t("blocked")}</p>;
  const toggle = async () => {
    const next = on ? false : (await Notification.requestPermission()) === "granted";
    try {
      localStorage.setItem(NOTIFY, next ? "on" : "off");
    } catch {}
    setBlocked(Notification.permission === "denied");
    setOn(next);
  };
  return (
    <Button
      variant="ghost"
      className="border-border min-h-11 self-start border sm:min-h-9"
      aria-pressed={on}
      onClick={() => void toggle()}
    >
      {on ? <Bell aria-hidden /> : <BellOff aria-hidden />}
      {t("toggle")}
    </Button>
  );
}

// Las cuentas conectadas que no pueden atender: desconectadas o con error, con el token vencido o
// a medio conectar (falta el webhook o un permiso). Sale de la salud de canales (F7-06, F7-11), que
// solo lee quien tiene `channels.manage`: es quien puede arreglarla en «Canales».
const reason = ({ status, status_detail: detail, platform }: ChannelAccount) => {
  if (detail === "TOKEN_INVALID") return "token";
  if (status === "ERROR" || status === "DISCONNECTED") return "down";
  return status === "PENDING" && platform !== "SANDBOX" ? "pending" : null;
};
export function ChannelAlert() {
  const t = useTranslations("inbox.channelAlert");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const allowed = permissions.some((grant) => grant.code === "channels.manage");
  const accounts = useQuery({
    queryKey: [...getChannelsListQueryKey(slug), "inbox-alert"],
    queryFn: ({ signal }) => channelsList(slug, { limit: 200 }, { signal }),
    enabled: allowed,
  });
  const down = (accounts.data?.results ?? []).flatMap((account) => {
    const why = reason(account);
    return why ? [{ account, why }] : [];
  });
  if (down.length === 0) return null;
  return (
    <section
      aria-label={t("label")}
      className="border-danger bg-surface flex flex-col gap-2 rounded-lg border p-3 text-sm"
    >
      <ul className="flex flex-col gap-1">
        {down.map(({ account, why }) => (
          <li key={account.id} className="flex items-start gap-2">
            <TriangleAlert aria-hidden className="text-danger mt-0.5 size-4 shrink-0" />
            <span>
              <span className="font-medium">
                <Plain>{account.name}</Plain>
              </span>
              {`: ${t(why)}`}
            </span>
          </li>
        ))}
      </ul>
      <Link href={`/o/${slug}/canales`} className="self-start font-medium underline">
        {t("link")}
      </Link>
    </section>
  );
}
