"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import { getOpportunitiesListQueryKey, opportunitiesList } from "@/lib/api/client";
import type {
  Opportunity,
  OpportunitiesListParams,
  PaginatedOpportunityList,
} from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { OpportunityCreate } from "./create-opportunity";
import { CustomerOpportunity } from "./customer-opportunity";
import { useOwners, usePipelines } from "./use-deals";

// Abiertas primero y, dentro de cada grupo, las más nuevas: el identificador es un UUIDv7, así
// que ordena por creación (ADR-004).
const byOpen = (a: Opportunity, b: Opportunity) =>
  Number(b.status === "OPEN") - Number(a.status === "OPEN") || (a.id < b.id ? 1 : -1);

// La pestaña «Oportunidades» de la ficha (F5-06): las del contacto (`?contact_id=`, F5-04b) y
// «Nueva oportunidad» con el cliente ya elegido (el formulario del tablero, sin buscador). Quién
// ve qué lo decide la API; un 403 solo se explica. La API pagina por creación, la más antigua
// primero (ADR-016): la pantalla ordena lo cargado y, si hay más páginas, lo dice. Sin pantalla
// de detalle (otro work item), cada fila trae todo lo que da la lista.
export function CustomerOpportunities({
  contactId,
  contactName,
}: {
  contactId: string;
  contactName: string;
}) {
  const t = useTranslations("deals");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const owners = useOwners();
  const pipelines = usePipelines(slug);
  const [chosen, setChosen] = useState("");
  const [now] = useState(() => Date.now()); // los días en la etapa se cuentan desde la apertura
  const params: OpportunitiesListParams = { contact_id: contactId, limit: 200 };
  const list = useInfiniteQuery<PaginatedOpportunityList, ApiError>({
    queryKey: [...getOpportunitiesListQueryKey(slug, params), "pages"],
    queryFn: ({ pageParam, signal }) =>
      opportunitiesList(
        slug,
        { ...params, ...(pageParam ? { cursor: pageParam as string } : {}) },
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    gcTime: 0, // como la ficha: al salir no queda en memoria
  });
  const rows = (list.data?.pages.flatMap((page) => page.results) ?? []).sort(byOpen);
  // Lo elegido que ya no existe (se desactivó) cuenta como no elegido: manda el predeterminado.
  const pipeline = pipelines.data?.find(({ id }) => id === chosen) ?? pipelines.data?.[0];
  const ready = !!pipeline?.stages.some((stage) => stage.is_active && stage.stage_type === "OPEN");
  const canCreate = permissions.some((grant) => grant.code === "opportunities.create");
  const denied = list.error?.status === 403;
  const problem = failed(list, t("denied"));
  const pipelineProblem = failed(pipelines, t("denied"));

  let body;
  if (denied) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (!list.data && problem) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {problem}
        </p>
        {retry(list)}
      </div>
    );
  } else if (!list.data) {
    body = (
      <p role="status" className="text-muted">
        {t("tab.loading")}
      </p>
    );
  } else if (rows.length === 0) {
    body = <p className="text-muted">{t("tab.empty")}</p>;
  } else {
    body = (
      <ul aria-label={t("tab.title")} className="flex flex-col gap-2">
        {rows.map((row) => (
          <CustomerOpportunity
            key={row.id}
            opportunity={row}
            owner={owners.name(row.assigned_user_id)}
            now={now}
          />
        ))}
      </ul>
    );
  }

  // Mientras cargan los pipelines no hay nada que poner en el recuadro: no se pinta vacío.
  let create;
  if (!canCreate || denied) {
    create = null;
  } else if (pipelineProblem) {
    create = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {pipelineProblem}
        </p>
        {retry(pipelines)}
      </div>
    );
  } else if (pipelines.data?.length === 0) {
    create = <p className="text-muted">{t("noPipelines")}</p>;
  } else if (pipeline && pipelines.data) {
    const several = pipelines.data.length > 1;
    create = (
      <>
        {several ? (
          <div className="w-full max-w-xs">
            <Choice
              label={t("filters.pipeline")}
              value={pipeline.id}
              busy={false}
              onChange={setChosen}
              options={pipelines.data.map(({ id, name }): [string, string] => [id, name])}
            />
          </div>
        ) : null}
        <OpportunityCreate
          slug={slug}
          pipelineId={pipeline.id}
          ready={ready}
          customer={{ id: contactId, name: contactName }}
          onlyPipeline={!several}
        />
      </>
    );
  }

  return (
    <>
      {create ? (
        <section className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4">
          {create}
        </section>
      ) : null}
      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">{t("tab.title")}</h2>
        {body}
        {list.hasNextPage && !denied ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-muted text-sm">{t("tab.partial")}</p>
            <Button
              variant="accent"
              size="lg"
              aria-disabled={list.isFetchingNextPage}
              onClick={() => list.isFetchingNextPage || void list.fetchNextPage()}
            >
              {list.isFetchingNextPage ? t("loadingMore") : t("more")}
            </Button>
            {list.isFetchNextPageError && problem ? (
              <p role="alert" className="text-danger text-sm">
                {problem}
              </p>
            ) : null}
          </div>
        ) : null}
      </section>
    </>
  );
}
