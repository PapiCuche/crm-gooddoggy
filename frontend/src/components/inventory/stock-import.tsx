"use client";

import { useMutation } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  inventoryImportsErrors,
  inventoryImportsTemplate,
  useInventoryImportsApply,
  useInventoryImportsCreate,
  useInventoryImportsRevert,
} from "@/lib/api/client";
import type { StockImport } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { StockImportRows } from "./stock-import-rows";

// Los topes de la API (`apps.imports.csvfile`): se miran antes de enviar, con su mensaje.
export const MAX_BYTES = 1_048_576;
export const MAX_ROWS = 1_000;
const ZIP = [0x50, 0x4b, 0x03, 0x04]; // la firma de un XLSX, como la mira la API
// Cómo se leyó el archivo, si no es el CSV en UTF-8 de siempre (`encoding` de la API).
const READ_AS = new Map<string, "windows1252" | "xlsx">([
  ["windows-1252", "windows1252"],
  ["xlsx", "xlsx"],
]);
type Problem = "noFile" | "empty" | "tooBig" | "tooManyRows" | "unsupported" | "localRead";
type Step = { name: "pick" } | { name: "preview" | "result"; job: StockImport };

// Las líneas con texto, sin la cabecera: como cuenta la API (una celda con saltos de línea
// entre comillas cuenta de más; es raro en esta plantilla).
function rowsOf(bytes: Uint8Array): number {
  let lines = 0;
  let blank = true;
  for (const byte of bytes) {
    if (byte === 10) {
      if (!blank) lines += 1;
      blank = true;
    } else if (![9, 13, 32, 34, 44, 59].includes(byte)) blank = false; // `,,` cuenta como vacía, como en la API
  }
  return lines + (blank ? 0 : 1) - 1;
}

// El archivo, en base64 como lo pide la API, o por qué no se envía.
async function encode(file: File): Promise<{ content: string } | { problem: Problem }> {
  if (/\.(xls|ods|numbers)$/i.test(file.name)) return { problem: "unsupported" }; // solo ayuda
  if (file.size > MAX_BYTES) return { problem: "tooBig" };
  let bytes: Uint8Array;
  try {
    bytes = new Uint8Array(await file.arrayBuffer());
  } catch {
    return { problem: "localRead" };
  }
  // Las filas de un XLSX no se cuentan aquí (va comprimido): las cuenta la API.
  if (!ZIP.every((byte, at) => bytes[at] === byte)) {
    const rows = rowsOf(bytes);
    if (rows < 0) return { problem: "empty" };
    if (rows > MAX_ROWS) return { problem: "tooManyRows" };
  }
  let binary = "";
  for (let at = 0; at < bytes.length; at += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
  }
  return { content: btoa(binary) };
}

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

// Una descarga en CSV de la API (la plantilla, el informe de errores): un `GET` por pulsación;
// un 401 lo trata `Providers` (es una mutación: pasa por su caché); otro error se dice al lado.
function Download({
  label,
  name,
  fetch,
}: {
  label: string;
  name: string;
  fetch: () => Promise<Blob>;
}) {
  const t = useTranslations();
  const download = useMutation<Blob, ApiError>({
    mutationFn: fetch,
    networkMode: "always",
    gcTime: 0,
    onSuccess: (blob) => save(blob, name),
  });
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button
        variant="accent"
        size="lg"
        aria-disabled={download.isPending}
        onClick={() => download.isPending || download.mutate()}
      >
        {download.isPending ? t("stockImport.downloading") : label}
      </Button>
      {download.isError && download.error.status !== 401 ? (
        <p role="alert" className="text-danger text-sm">
          {t(`errors.api.${apiErrorKey(download.error)}`)}
        </p>
      ) : null}
    </div>
  );
}

function Counts({ items }: { items: [string, number, boolean?][] }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {items.map(([label, value, alarm]) => (
        <div key={label} className="border-border bg-surface rounded-lg border px-3 py-2">
          <dt className="text-muted text-[13px]">{label}</dt>
          <dd className={cn("text-xl font-semibold tabular-nums", alarm && value && "text-danger")}>
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// Importar stock desde un CSV (F3-22, E04-07): plantilla, archivo, revisión con sus recuentos,
// confirmación y resultado, con `POST …/inventory/imports/` y `…/{id}/apply/` (F3-20, F3-21);
// en la revisión y en el resultado, las filas una a una (`StockImportRows`, F3-22b).
// El archivo se lee en el navegador al enviarlo y viaja en base64 en el JSON; no se guarda en
// el navegador (la escritura no queda en la caché: `gcTime: 0` y `reset`). Aplicar otra vez la
// misma importación no repite ajustes (la API la aplica una vez): tras perder una respuesta,
// reintentar es seguro y enseña el mismo resultado. En el resultado, «Revertir importación»
// (`…/{id}/revert/`, F3-24) devuelve las celdas al saldo de antes, también con confirmación.
export function StockImport() {
  const t = useTranslations("stockImport");
  const errors = useTranslations("errors.api");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  // Comodidad: la frontera es la API (`inventory.adjust` en cada ruta de la importación).
  const allowed = permissions.some((grant) => grant.code === "inventory.adjust");
  const [denied, setDenied] = useState(false);
  const deny = useCallback(() => setDenied(true), []);
  const [step, setStep] = useState<Step>({ name: "pick" });
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<Problem | "gone" | null>(null);
  const [reading, setReading] = useState(false);
  const [asking, setAsking] = useState<"apply" | "revert" | null>(null);
  const [announce, setAnnounce] = useState("");
  const sending = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const fileId = useId();
  const upload = useInventoryImportsCreate({ mutation: { networkMode: "always", gcTime: 0 } });
  const apply = useInventoryImportsApply({ mutation: { networkMode: "always", gcTime: 0 } });
  const revert = useInventoryImportsRevert({ mutation: { networkMode: "always", gcTime: 0 } });
  const writes = [upload, apply, revert];
  const ended = writes.some((write) => write.error?.status === 401); // `Providers` → login
  const busy = reading || writes.some((write) => write.isPending) || ended;
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });
  for (const { error } of writes) {
    if (error?.status === 403 && !denied) setDenied(true);
  }

  const shown = useRef(step); // al abrir la página el foco no se mueve; al cambiar de paso, sí
  useEffect(() => {
    if (shown.current !== step) (step.name === "pick" ? input : heading).current?.focus();
    shown.current = step;
  }, [step]);
  const opened = useRef<Step | null>(null); // el paso en que se abrió la confirmación
  useEffect(() => {
    if (asking) cancel.current?.focus();
    else if (opened.current === step) trigger.current?.focus(); // cancelada: el mismo paso
    opened.current = asking ? step : null;
  }, [asking, step]);

  async function send(event: FormEvent) {
    event.preventDefault();
    if (sending.current) return;
    if (!file) return setProblem("noFile");
    sending.current = true;
    setReading(true);
    const encoded = await encode(file);
    if ("problem" in encoded) {
      setReading(false);
      sending.current = false;
      return setProblem(encoded.problem);
    }
    setProblem(null);
    upload.mutate(
      { orgSlug: slug, data: { file_name: file.name.slice(0, 255), content: encoded.content } },
      {
        onSuccess: (job) => {
          upload.reset(); // el archivo, fuera de la caché de escrituras
          setFile(null);
          setStep({ name: "preview", job });
          setAnnounce(t("ready", { rows: job.total_rows, errors: job.error_rows }));
        },
        // Hasta aquí sigue ocupado: entre la lectura y la petición no cabe otro envío.
        onSettled: () => setReading(false),
      },
    );
  }

  function confirm() {
    if (sending.current || step.name === "pick") return;
    sending.current = true;
    const onError = (error: ApiError) => {
      if (error.status !== 404) return;
      setAsking(null);
      setStep({ name: "pick" });
      setProblem("gone");
    };
    const importId = step.job.id;
    if (asking === "revert") {
      revert.mutate(
        { orgSlug: slug, importId, data: { confirm: true } },
        {
          onSuccess: (job) => {
            setAsking(null);
            setStep({ name: "result", job });
            setAnnounce(t("undone", { reverted: job.revert?.reverted ?? 0 }));
          },
          onError,
        },
      );
      return;
    }
    apply.mutate(
      { orgSlug: slug, importId },
      {
        onSuccess: (job) => {
          setAsking(null);
          setStep({ name: "result", job });
          setAnnounce(t("done", { applied: job.result?.applied ?? 0 }));
        },
        onError,
      },
    );
  }

  function restart() {
    apply.reset();
    revert.reset();
    setProblem(null);
    setAsking(null);
    setAnnounce("");
    setStep({ name: "pick" });
  }

  const uploadError = upload.error && upload.error.status !== 401 ? upload.error : null;
  // Por qué no se pudo leer el archivo: la API lo escribe para quien lo sube (`csvfile.Unreadable`).
  const given: unknown = uploadError?.code === "VALIDATION_ERROR" && uploadError.fields?.content;
  const reason: unknown = Array.isArray(given) && (given[0] as { message?: unknown })?.message;
  const pickError = problem
    ? t(problem)
    : uploadError
      ? uploadError.code === "VALIDATION_ERROR"
        ? typeof reason === "string"
          ? t("unreadable", { reason })
          : t("unreadableGeneric")
        : errors(apiErrorKey(uploadError))
      : null;

  let body;
  if (!allowed || denied) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (step.name === "pick") {
    body = (
      <div className="border-border bg-surface flex flex-col gap-5 rounded-lg border p-4 sm:p-6">
        <div className="flex flex-col gap-2">
          <Download
            label={t("template")}
            name="plantilla-stock.csv"
            fetch={() => inventoryImportsTemplate(slug)}
          />
          <p className="text-muted text-sm">{t("templateHint")}</p>
        </div>
        <form
          method="post"
          className="flex flex-col items-start gap-3"
          onSubmit={(event) => void send(event)}
        >
          <label htmlFor={fileId} className="text-sm font-medium">
            {t("file")}
          </label>
          <input
            ref={input}
            id={fileId}
            type="file"
            accept=".csv,text/csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            aria-describedby={`${fileId}-hint${pickError ? ` ${fileId}-error` : ""}`}
            aria-invalid={!!pickError}
            className="file:bg-surface-raised file:text-foreground text-sm file:mr-3 file:h-11 file:rounded-[10px] file:border-0 file:px-4 file:font-medium"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null); // lo que se ve es lo que se enviará
              if (busy) return;
              setProblem(null);
              upload.reset();
            }}
          />
          <p id={`${fileId}-hint`} className="text-muted text-sm">
            {t("fileHint")}
          </p>
          {pickError ? (
            <p id={`${fileId}-error`} role="alert" className="text-danger text-sm wrap-anywhere">
              {pickError}
            </p>
          ) : null}
          <Button type="submit" variant="primary" size="lg" aria-disabled={busy}>
            {busy ? t("uploading") : t("upload")}
          </Button>
        </form>
      </div>
    );
  } else {
    const { job } = step;
    const changes = job.new_count + job.update_count;
    const { result, revert: undone } = job;
    const wrong =
      job.error_rows +
      (result ? result.stale + result.failed : 0) +
      (undone ? undone.stale + undone.failed : 0);
    const read = READ_AS.get(job.encoding);
    const reverting = asking === "revert";
    const failed = (reverting ? revert : apply).error;
    const writeError = failed && ![401, 403, 404].includes(failed.status) ? failed : null;
    const label = reverting ? t("revert") : t("apply", { count: changes });
    body = (
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-1">
          <h2 ref={heading} tabIndex={-1} className="text-xl font-semibold outline-none">
            {undone ? t("revertedTitle") : result ? t("resultTitle") : t("previewTitle")}
          </h2>
          <p className="text-muted wrap-anywhere">
            {t("fileLine", { name: job.file_name, rows: job.total_rows })}
          </p>
        </div>
        {undone ? (
          <Counts
            items={[
              [t("count.reverted"), undone.reverted],
              [t("count.STALE"), undone.stale, true],
              [t("count.ERROR"), undone.failed, true],
            ]}
          />
        ) : result ? (
          <Counts
            items={[
              [t("count.applied"), result.applied],
              [t("count.UNCHANGED"), result.unchanged],
              [t("count.STALE"), result.stale, true],
              [t("count.ERROR"), job.error_rows + result.failed, true],
            ]}
          />
        ) : (
          <Counts
            items={[
              [t("count.CREATE"), job.new_count],
              [t("count.UPDATE"), job.update_count],
              [t("count.UNCHANGED"), job.unchanged_count],
              [t("count.ERROR"), job.error_rows, true],
            ]}
          />
        )}
        {!result && job.duplicate_of ? (
          <p className="border-accent bg-accent/15 rounded-lg border-l-4 px-3 py-2 text-sm">
            {t("duplicate")}
          </p>
        ) : null}
        {undone?.stale ? (
          <p className="text-sm">{t("revertStaleHint")}</p>
        ) : !undone && result?.stale ? (
          <p className="text-sm">{t("staleHint")}</p>
        ) : null}
        {read || job.ignored_columns.length ? (
          <p className="text-muted text-sm wrap-anywhere">
            {read ? `${t(read)} ` : ""}
            {job.ignored_columns.length
              ? t("ignored", { columns: job.ignored_columns.join(", ") })
              : ""}
          </p>
        ) : null}
        <div className="flex flex-wrap items-start gap-3">
          {!result && changes > 0 && !asking ? (
            <Button ref={trigger} variant="primary" size="lg" onClick={() => setAsking("apply")}>
              {label}
            </Button>
          ) : null}
          {job.status === "APPLIED" && (result?.applied ?? 0) > 0 && !asking ? (
            <Button
              ref={trigger}
              variant="ghost"
              size="lg"
              className="border-border border"
              onClick={() => setAsking("revert")}
            >
              {t("revert")}
            </Button>
          ) : null}
          {wrong > 0 ? (
            <Download
              label={t("report")}
              name="errores-stock.csv"
              fetch={() => inventoryImportsErrors(slug, job.id)}
            />
          ) : null}
          {!asking ? (
            <Button variant="ghost" size="lg" className="border-border border" onClick={restart}>
              {t("restart")}
            </Button>
          ) : null}
        </div>
        {!result && changes === 0 ? <p className="text-muted">{t("nothing")}</p> : null}
        {asking ? (
          <div
            role="group"
            aria-label={label}
            aria-describedby={`${fileId}-ask`}
            className="border-foreground/20 bg-surface flex flex-col items-start gap-3 rounded-lg border p-4"
          >
            <p id={`${fileId}-ask`} className="text-sm">
              {reverting
                ? t("revertAsk", { count: result?.applied ?? 0 })
                : t("ask", { count: changes, errors: job.error_rows })}
            </p>
            {writeError ? (
              <p role="alert" className="text-danger text-sm">
                {errors(apiErrorKey(writeError))} {t(reverting ? "revertRetryHint" : "retryHint")}
              </p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button
                ref={cancel}
                variant="ghost"
                size="lg"
                aria-disabled={busy}
                onClick={() => sending.current || setAsking(null)}
              >
                {t("cancel")}
              </Button>
              <Button variant="primary" size="lg" aria-disabled={busy} onClick={confirm}>
                {reverting
                  ? t(busy ? "reverting" : "confirmRevert")
                  : t(busy ? "applying" : "confirm")}
              </Button>
            </div>
          </div>
        ) : null}
        <StockImportRows key={job.id} slug={slug} job={job} onDenied={deny} />
      </div>
    );
  }

  return (
    <section
      className="flex max-w-4xl flex-col gap-6"
      // Enter mantenido repite la pulsación: enviaría o aplicaría sin querer.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {body}
      <p role="status" className="sr-only">
        {announce}
      </p>
    </section>
  );
}
