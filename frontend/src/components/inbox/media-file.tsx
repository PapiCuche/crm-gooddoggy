"use client";

import { useMutation } from "@tanstack/react-query";
import { Download, Hourglass, ShieldEllipsis, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { conversationAttachmentUrl } from "@/lib/api/client";
import type { MessageAttachment } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

// El archivo de un medio entrante (F7-09a3). Solo se pide la URL firmada al pulsar «Descargar» y solo
// si la API dice `available` (DONE, entrante, antivirus CLEAN, tipo del mapa y contacto visible): sin
// antivirus (D-F7-04) nada lo está y la burbuja lo explica. La URL nunca se guarda (ni en el estado ni
// en la caché de TanStack: la mutación no devuelve nada) ni va a un `src`: el almacén la sirve siempre
// como `attachment` y se abre una vez, con un enlace temporal, en otra pestaña: si el almacén responde
// un error (firma caducada, objeto borrado, 5xx) sin `attachment`, el CRM y el borrador no se pierden.
const STATES: Record<string, { icon: ReactNode; text: string }> = {
  PENDING: { icon: <Hourglass aria-hidden />, text: "receiving" },
  DOWNLOADING: { icon: <Hourglass aria-hidden />, text: "receiving" },
  DONE: { icon: <ShieldEllipsis aria-hidden />, text: "waiting" },
  FAILED: { icon: <TriangleAlert aria-hidden />, text: "failed" },
  EXPIRED: { icon: <TriangleAlert aria-hidden />, text: "expired" },
};
const UNKNOWN = { icon: <TriangleAlert aria-hidden />, text: "unknown" };

// Un enlace que no está en el documento, a otra pestaña sin `opener` ni `Referer` (la URL del CRM no
// llega al almacén) y solo a http(s): lo que devuelva la API no puede ser un `javascript:`.
function leave(url: string) {
  const target = new URL(url);
  if (target.protocol !== "https:" && target.protocol !== "http:") throw new Error("unsafe url");
  const link = document.createElement("a");
  link.href = target.href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.referrerPolicy = "no-referrer";
  link.click();
}

// El tamaño de un archivo en KB o MB, con un decimal (también el del adjunto del compositor).
export function fileSize(format: ReturnType<typeof useFormatter>, bytes: number) {
  const mb = bytes / 1_048_576;
  return format.number(mb < 1 ? mb * 1024 : mb, {
    style: "unit",
    unit: mb < 1 ? "kilobyte" : "megabyte",
    maximumFractionDigits: 1,
  });
}

export function MediaFile({
  conversationId,
  attachment,
}: {
  conversationId: string;
  attachment: MessageAttachment;
}) {
  const t = useTranslations("inbox.file");
  const format = useFormatter();
  const { organization } = useTenant();
  const open = useMutation<void, ApiError>({
    mutationFn: async () =>
      leave(
        (await conversationAttachmentUrl(organization.slug, conversationId, attachment.id)).url,
      ),
    networkMode: "always",
    gcTime: 0,
  });
  if (!attachment.available) {
    // `Object.hasOwn`: un estado que esta pantalla no conoce (o `constructor`) no llega a `t()`.
    const state =
      (Object.hasOwn(STATES, attachment.status) && STATES[attachment.status]) || UNKNOWN;
    return (
      <p className="text-muted flex items-start gap-1.5 text-sm [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0">
        {state.icon}
        <span>{t(state.text)}</span>
      </p>
    );
  }
  const size = attachment.size_bytes === null ? null : fileSize(format, attachment.size_bytes);
  const type = attachment.mime_type?.split("/")[1]?.toUpperCase() ?? null;
  const detail = [type, size].filter(Boolean).join(" · ");
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <Button
        variant="accent"
        aria-disabled={open.isPending}
        aria-label={detail ? t("download", { detail }) : undefined}
        onClick={() => open.isPending || open.mutate()}
      >
        <Download aria-hidden />
        {open.isPending ? t("opening") : t("save")}
      </Button>
      {detail ? <span className="text-muted text-xs">{detail}</span> : null}
      {open.isError && open.error?.status !== 401 ? (
        <p role="alert" className="text-danger w-full text-sm">
          {t("error")}
        </p>
      ) : null}
    </div>
  );
}
