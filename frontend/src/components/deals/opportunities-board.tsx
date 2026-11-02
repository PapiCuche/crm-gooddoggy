"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { useReads } from "@/components/inventory/cell-picker";
import { moneyText } from "@/components/pricing/margins";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { getOpportunitiesListQueryKey, opportunitiesList } from "@/lib/api/client";
import type { Opportunity, OpportunitiesListParams, Stage } from "@/lib/api/model";

import { SEARCH_DELAY, searchTerm } from "../contacts/contact-search";
import { cents, OpportunityCard } from "./opportunity-card";
import { OpportunityTable } from "./opportunity-table";
import { useOwners, usePipelines } from "./use-deals";

type View = "board" | "table";
const STATUSES = ["OPEN", "WON", "LOST", ""] as const; // «» : todas

// Oportunidades (F5-05a): las que devuelve `GET /api/v1/o/{slug}/opportunities/` (F5-04b) del
// pipeline elegido, en una columna por etapa o en tabla. Solo lee: mover, crear, ganar y perder
// llegan en las partes siguientes. Quién ve qué lo decide la API (`opportunities.view` y su
// alcance); un 403 solo se explica. La lista viene por cursor en orden de creación (ADR-016),
// así que los totales de las columnas son los de lo cargado, y la pantalla lo dice.
export function OpportunitiesBoard() {
  const t = useTranslations("deals");
  const { organization, user } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const owners = useOwners();
  const pipelines = usePipelines(slug);
  const [chosen, setChosen] = useState("");
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("OPEN");
  const [ownerId, setOwnerId] = useState("");
  const [view, setView] = useState<View>("board");
  // La búsqueda va a la API tras la espera y solo si llega al mínimo (como Clientes).
  const [draft, setDraft] = useState("");
  const [term, setTerm] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setTerm(searchTerm(draft)), SEARCH_DELAY);
    return () => clearTimeout(timer);
  }, [draft]);
  const [now] = useState(() => Date.now()); // los días en la etapa se cuentan desde la apertura

  // Lo elegido que ya no existe (se desactivó) cuenta como no elegido: manda el predeterminado.
  const pipeline = pipelines.data?.find(({ id }) => id === chosen) ?? pipelines.data?.[0];
  const filters: OpportunitiesListParams = {
    ...{ pipeline_id: pipeline?.id },
    ...(status ? { status } : {}),
    ...(ownerId ? { assigned_user_id: ownerId } : {}),
    ...(term ? { q: term } : {}),
  };
  // Cada filtro es su propia lista; los filtros viajan también con «Cargar más».
  const list = useInfiniteQuery({
    queryKey: [...getOpportunitiesListQueryKey(slug, filters), "pages"],
    queryFn: ({ pageParam, signal }) =>
      opportunitiesList(
        slug,
        { ...filters, ...(pageParam ? { cursor: pageParam } : {}) },
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    enabled: !!pipeline,
    gcTime: 0,
  });
  const rows = list.data?.pages.flatMap((page) => page.results) ?? [];
  const byStage = new Map<string, Opportunity[]>();
  for (const row of rows) byStage.set(row.stage.id, [...(byStage.get(row.stage.id) ?? []), row]);
  // Una columna por etapa activa; una inactiva solo si aún guarda alguna oportunidad cargada.
  const stages = (pipeline?.stages ?? []).filter(
    (stage) =>
      (stage.is_active || byStage.has(stage.id)) && (!status || stage.stage_type === status),
  );
  const problem = failed(pipelines, t("denied")) ?? failed(list, t("denied"));

  let body;
  if (problem) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {problem}
        </p>
        {retry(pipelines.isError ? pipelines : list)}
      </div>
    );
  } else if (pipelines.data?.length === 0) {
    body = <p className="text-muted">{t("noPipelines")}</p>;
  } else if (!list.data) {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  } else {
    body = (
      <>
        <p role="status" className="sr-only">
          {t("count", { count: rows.length })}
        </p>
        {rows.length === 0 ? (
          <p className="text-muted wrap-anywhere">
            {term || ownerId ? t("emptyFiltered") : t("empty", { status })}
          </p>
        ) : view === "table" ? (
          <OpportunityTable rows={rows} owner={owners.name} now={now} />
        ) : (
          <Board
            stages={stages}
            byStage={byStage}
            currency={pipeline?.currency ?? ""}
            owner={owners.name}
            now={now}
          />
        )}
        {list.hasNextPage ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-muted text-sm">{t("partial")}</p>
            <Button
              variant="accent"
              size="lg"
              aria-disabled={list.isFetchingNextPage}
              onClick={() => list.isFetchingNextPage || void list.fetchNextPage()}
            >
              {list.isFetchingNextPage ? t("loadingMore") : t("more")}
            </Button>
          </div>
        ) : null}
      </>
    );
  }

  return (
    <section className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {pipelines.data?.length ? (
        <div
          role="search"
          aria-label={t("filters.landmark")}
          className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"
        >
          <Choice
            label={t("filters.pipeline")}
            value={pipeline?.id ?? ""}
            busy={false}
            onChange={setChosen}
            options={pipelines.data.map(({ id, name }): [string, string] => [id, name])}
          />
          <Choice
            label={t("filters.status")}
            value={status}
            busy={false}
            onChange={(value) => setStatus(value as typeof status)}
            options={STATUSES.map((one): [string, string] => [
              one,
              t(`filters.statuses.${one || "ANY"}`),
            ])}
          />
          <Choice
            label={t("filters.owner")}
            value={ownerId}
            busy={false}
            onChange={setOwnerId}
            options={[["", t("filters.anyOwner")], [user.id, t("owner.you")], ...owners.options]}
          />
          <TextField
            type="search"
            label={t("filters.search")}
            hint={t("filters.searchHint")}
            value={draft}
            autoComplete="off"
            onChange={(event) => setDraft(event.target.value)}
          />
        </div>
      ) : null}
      {pipeline ? (
        <div role="group" aria-label={t("view.label")} className="flex gap-2">
          {(["board", "table"] as const).map((one) => (
            <Button
              key={one}
              variant={view === one ? "accent" : "ghost"}
              className="border-border min-h-11 border sm:min-h-9"
              aria-pressed={view === one}
              onClick={() => setView(one)}
            >
              {t(`view.${one}`)}
            </Button>
          ))}
        </div>
      ) : null}
      {body}
    </section>
  );
}

// Las columnas: se desplazan en horizontal y se detienen en cada una (en 390 px asoma la
// siguiente). La región recibe el foco para que el teclado la recorra.
function Board({
  stages,
  byStage,
  currency,
  owner,
  now,
}: {
  stages: Stage[];
  byStage: Map<string, Opportunity[]>;
  currency: string;
  owner: (id: string | null) => string;
  now: number;
}) {
  const t = useTranslations("deals");
  const format = useFormatter();
  return (
    <div
      role="region"
      aria-label={t("board.label")}
      tabIndex={0}
      className="-mx-4 flex snap-x snap-mandatory scroll-px-4 gap-3 overflow-x-auto px-4 pb-2 lg:mx-0 lg:px-0"
    >
      {stages.map((stage) => {
        const cards = byStage.get(stage.id) ?? [];
        const total = cards.reduce((sum, card) => sum + cents(card.amount), 0);
        return (
          <section
            key={stage.id}
            aria-labelledby={`stage-${stage.id}`}
            className="border-border bg-surface flex w-[85%] shrink-0 snap-start flex-col gap-3 rounded-lg border p-3 sm:w-72"
          >
            <header className="flex flex-col leading-snug">
              <h2 id={`stage-${stage.id}`} className="flex items-center gap-2 font-medium">
                <span
                  aria-hidden
                  className="size-2.5 rounded-full"
                  style={{ backgroundColor: stage.color }}
                />
                {stage.name}
              </h2>
              <p className="text-muted text-sm">
                {t("board.column", { count: cards.length })}
                {" · "}
                {moneyText(format, (total / 100).toFixed(2), currency)}
              </p>
            </header>
            {cards.length === 0 ? (
              <p className="text-muted text-sm">{t("board.emptyColumn")}</p>
            ) : (
              <ul aria-labelledby={`stage-${stage.id}`} className="flex flex-col gap-2">
                {cards.map((card) => (
                  <OpportunityCard
                    key={card.id}
                    opportunity={card}
                    owner={owner(card.assigned_user_id)}
                    now={now}
                  />
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}
