"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { channelOf, TONES, useChannelName } from "@/components/channels/channel-identity";
import { conversationsAccountOptions, getConversationsListQueryKey } from "@/lib/api/client";
import type { AccountOption } from "@/lib/api/model";

// UX-INBOX-02d: las cuentas por las que se puede filtrar, de `…/conversations/accounts/`: solo las
// de conversaciones que quien atiende ya ve, con su id, su red y su nombre (nada del canal en sí).
// Cuelga de la clave de la lista: lo que la relee (el tiempo real, responder) también la relee.
export function useAccountOptions() {
  const slug = useTenant().organization.slug;
  return useQuery({
    queryKey: [...getConversationsListQueryKey(slug), "accounts"],
    queryFn: async ({ signal }) => {
      const found: AccountOption[] = [];
      let cursor: string | null = "";
      while (cursor !== null) {
        const page = await conversationsAccountOptions(
          slug,
          { limit: 200, ...(cursor ? { cursor } : {}) },
          { signal },
        );
        found.push(...page.results);
        cursor = page.next;
      }
      return found;
    },
    gcTime: 0, // como la lista: al salir de la organización no queda nada suyo en memoria
  });
}

// El selector de cuenta: «Red · nombre» en texto (el color nunca va solo) y, al lado, el icono de
// la red de la elegida. Con una sola cuenta no hay nada que elegir; sin opciones (aún o por un
// fallo) no se muestra, salvo que ya haya una elegida, para poder quitarla.
export function AccountFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (account: string) => void;
}) {
  const t = useTranslations("inbox.filters");
  const network = useChannelName();
  const accounts = useAccountOptions().data ?? [];
  if (accounts.length < 2 && !value) return null;
  const chosen = accounts.find((one) => one.id === value);
  const { icon: Icon, tone } = channelOf(chosen?.platform ?? "");
  const options: [string, string][] = [
    ["", t("allAccounts")],
    ...accounts.map((one): [string, string] => [one.id, `${network(one.platform)} · ${one.name}`]),
  ];
  if (value && !chosen) options.push([value, t("otherAccount")]); // ya no está entre las suyas
  return (
    <div className="flex min-w-0 items-end gap-2">
      {chosen ? (
        <Icon
          aria-hidden
          data-account-icon
          className={`mb-3.5 size-4 shrink-0 ${TONES[tone].icon}`}
        />
      ) : null}
      <div className="min-w-0 flex-1">
        <Choice
          label={t("account")}
          value={value}
          busy={false}
          onChange={onChange}
          options={options}
        />
      </div>
    </div>
  );
}
