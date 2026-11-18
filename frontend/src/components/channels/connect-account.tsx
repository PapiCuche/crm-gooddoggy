"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { channelsSocialConnect, channelsWhatsappConnect } from "@/lib/api/client";
import type { ConnectRequest, SocialConnectRequest } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { channelError } from "./channel-errors";
import type { Route } from "./channel-routes";
import { useChannelWrite } from "./use-channel-write";

type Body<T> = Record<string, string> & T;

// Conectar una cuenta: WhatsApp (F7-07b, `POST …/channels/whatsapp/` con el WABA y el
// `phone_number_id`), Messenger (la página) o Instagram (la página vinculada y la cuenta) por
// `POST …/channels/{messenger|instagram}/` (F7b-06). El token es un campo de contraseña que se
// vacía al enviar (`secret`) y solo vive en el cierre de la petición: no se guarda en estado, en la
// caché ni en el navegador. Una cuenta no registrable da el mismo error sin más detalle.
export function ConnectAccount({
  slug,
  route,
  onConnected,
}: {
  slug: string;
  route: Route;
  onConnected: () => void;
}) {
  const t = useTranslations("channels.connect");
  const errors = useTranslations("channels.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar desaparece el control pulsado: el foco vuelve al botón, salvo que ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);
  const social = route !== "whatsapp";
  const text = (key: "open" | "title" | "note") => t(social ? `${route}.${key}` : key);
  // Un id de Meta: cifras, sin corrector ni mayúsculas en el móvil.
  const id = (name: string, label: string, required: string, invalid: string) => ({
    ...{ name, max: 30, label, required, invalid },
    ...{ inputMode: "numeric" as const, literal: true },
  });
  // El mismo texto para cualquier cuenta que no se pueda registrar: no se descompone.
  const own =
    route === "whatsapp"
      ? [
          id("waba_id", t("waba"), t("wabaRequired"), t("wabaInvalid")),
          id("phone_number_id", t("phone"), t("phoneRequired"), errors("badNumber")),
        ]
      : route === "messenger"
        ? [id("page_id", t("page"), t("pageRequired"), errors("badPage"))]
        : [
            id("page_id", t("linkedPage"), t("pageRequired"), errors("badPage")),
            id("instagram_account_id", t("igAccount"), t("igRequired"), errors("badAccount")),
          ];

  return (
    <div className={cn("flex flex-col items-start gap-3", open && "w-full")}>
      {open ? (
        <FieldsForm
          title={text("title")}
          heading={<h2 className="font-medium">{text("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={[
            {
              ...{ name: "name", max: 100, label: t("name"), invalid: t("nameInvalid") },
              required: t("nameRequired"),
            },
            ...own,
            {
              ...{ name: "access_token", max: 2048, label: t("token"), hint: t("tokenHint") },
              ...{ invalid: t("tokenInvalid"), required: t("tokenRequired"), secret: true },
              literal: true,
            },
          ]}
          write={write}
          send={(values) =>
            run(
              // Solo los campos de esta plataforma; la forma la comprueba la API.
              () =>
                social
                  ? channelsSocialConnect(slug, route, values as Body<SocialConnectRequest>)
                  : channelsWhatsappConnect(slug, values as Body<ConnectRequest>),
              () => {
                setDone(values.name ?? "");
                setOpen(false);
                onConnected();
              },
            )
          }
          taken={(error) => channelError(error, errors, social)}
          denied={errors("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          note={<p className="text-muted text-sm">{text("note")}</p>}
          onCancel={() => setOpen(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => {
            write.reset();
            setDone(null);
            setOpen(true);
          }}
        >
          {text("open")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
        {done ? t("done", { name: done }) : ""}
      </p>
      {stepUp}
    </div>
  );
}
