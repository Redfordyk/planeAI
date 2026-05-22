/**
 * AISearch — semantic-search panel for the planeAI add-on.
 *
 * Self-contained component that pairs `useAISearch` (TZ 2.4) with
 * `useIndexStatus` (TZ 2.5). Designed to drop into either a route
 * page (e.g. `/workspaces/<slug>/ai-search`) or a slide-over panel
 * — see `<AISearchPanel>` at the bottom of this file for the latter.
 *
 * Styling uses Tailwind utilities matching Plane's existing palette
 * (`bg-custom-background-*` / `text-custom-text-*` design tokens
 * which Plane defines in tailwind.config). If those tokens are
 * absent in a given build target, the component still renders;
 * neutral grays from the regular Tailwind palette fall through.
 *
 * Markdown: we render a minimal subset — paragraphs, inline code,
 * fenced code blocks, and the [source_type:UUID] citation tokens
 * which become clickable links into Plane's work-item / page views.
 * Pulling in a full markdown library here would add 100 kB+ of JS
 * for a feature that mostly streams short answers.
 */

import { useCallback, useMemo, useState } from "react";

import { useAISearch, type SearchSource } from "../../hooks/ai/use-ai-search";
import { useIndexStatus } from "../../hooks/ai/use-index-status";

export type AISearchProps = {
  workspaceId: string;
  /** Plane workspace slug — used to build links to work items. */
  workspaceSlug: string;
  /** Optional className for the outer container. */
  className?: string;
};

export function AISearch({
  workspaceId,
  workspaceSlug,
  className = "",
}: AISearchProps) {
  const [draft, setDraft] = useState("");
  const { answer, sources, status, error, search, cancel } = useAISearch(workspaceId);
  // Poll every 5s while the index is filling — auto-stops on ready=true.
  const { data: index, loading: indexLoading } = useIndexStatus(workspaceId, 5000);

  const indexReady = index?.ready ?? true;
  const isStreaming = status === "streaming";
  const disabled = !indexReady || isStreaming || !draft.trim();

  const onSubmit = useCallback(
    (e?: React.FormEvent) => {
      e?.preventDefault();
      const q = draft.trim();
      if (!q) return;
      void search(q);
    },
    [draft, search]
  );

  return (
    <div className={`flex flex-col gap-4 p-4 ${className}`}>
      <IndexBanner
        index={index ?? null}
        loading={indexLoading}
      />

      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Спросите по задачам, комментариям, страницам…"
          disabled={!indexReady}
          className="flex-1 rounded border border-custom-border-200 bg-custom-background-90 px-3 py-2 text-sm outline-none focus:border-custom-primary-100 disabled:opacity-50"
        />
        {isStreaming ? (
          <button
            type="button"
            onClick={cancel}
            className="rounded bg-custom-background-80 px-3 py-2 text-sm hover:bg-custom-background-70"
          >
            Стоп
          </button>
        ) : (
          <button
            type="submit"
            disabled={disabled}
            className="rounded bg-custom-primary-100 px-3 py-2 text-sm text-white hover:bg-custom-primary-200 disabled:opacity-50"
          >
            Спросить
          </button>
        )}
      </form>

      {error && (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      <AnswerPanel
        answer={answer}
        sources={sources}
        workspaceSlug={workspaceSlug}
        status={status}
      />
    </div>
  );
}

// --- index status banner ---------------------------------------------------

function IndexBanner({
  index,
  loading,
}: {
  index: import("../../hooks/ai/use-index-status").IndexStatus | null;
  loading: boolean;
}) {
  if (loading && !index) {
    return (
      <div className="rounded bg-custom-background-90 px-3 py-2 text-xs text-custom-text-300">
        Загрузка статуса индексации…
      </div>
    );
  }
  if (!index) return null;
  if (index.total === 0) {
    return (
      <div className="rounded bg-custom-background-90 px-3 py-2 text-xs text-custom-text-300">
        В воркспейсе пока нет задач для индексации.
      </div>
    );
  }
  if (index.ready) {
    return (
      <div className="rounded bg-green-50 px-3 py-2 text-xs text-green-700">
        Индекс готов ({index.indexed}/{index.total},{" "}
        {Math.round(index.coverage * 100)}%).
      </div>
    );
  }
  return (
    <div className="rounded bg-yellow-50 px-3 py-2 text-xs text-yellow-800">
      <div className="mb-1">
        Идёт индексация: {index.indexed}/{index.total} (
        {Math.round(index.coverage * 100)}%). Поиск временно выключен —
        вернитесь через минуту.
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded bg-yellow-100">
        <div
          className="h-full bg-yellow-400 transition-all"
          style={{ width: `${index.coverage * 100}%` }}
        />
      </div>
    </div>
  );
}

// --- answer + sources ------------------------------------------------------

function AnswerPanel({
  answer,
  sources,
  workspaceSlug,
  status,
}: {
  answer: string;
  sources: SearchSource[];
  workspaceSlug: string;
  status: string;
}) {
  if (status === "idle" && !answer && sources.length === 0) {
    return (
      <div className="rounded bg-custom-background-90 px-3 py-4 text-sm text-custom-text-300">
        Введите запрос — ответ соберётся из ваших задач и комментариев.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_220px]">
      <article className="prose prose-sm max-w-none rounded border border-custom-border-200 bg-custom-background-90 p-4 text-sm">
        <RenderedAnswer text={answer} />
        {status === "streaming" && (
          <span className="ml-1 inline-block h-3 w-2 animate-pulse bg-custom-text-300 align-middle" />
        )}
      </article>
      <SourcesSidebar sources={sources} workspaceSlug={workspaceSlug} />
    </div>
  );
}

function SourcesSidebar({
  sources,
  workspaceSlug,
}: {
  sources: SearchSource[];
  workspaceSlug: string;
}) {
  if (!sources.length) {
    return (
      <aside className="rounded border border-custom-border-200 bg-custom-background-90 p-3 text-xs text-custom-text-300">
        Источники появятся здесь
      </aside>
    );
  }
  return (
    <aside className="rounded border border-custom-border-200 bg-custom-background-90 p-3 text-xs">
      <div className="mb-2 font-medium text-custom-text-200">Источники</div>
      <ul className="flex flex-col gap-1">
        {sources.map((s) => (
          <li key={`${s.source_type}:${s.source_id}`}>
            <SourceLink source={s} workspaceSlug={workspaceSlug} />
          </li>
        ))}
      </ul>
    </aside>
  );
}

function sourceLabel(s: SearchSource): string {
  switch (s.source_type) {
    case "work_item":
      return `Задача · ${s.source_id.slice(0, 8)}`;
    case "comment":
      return `Комментарий · ${s.source_id.slice(0, 8)}`;
    case "page":
      return `Страница · ${s.source_id.slice(0, 8)}`;
    default:
      return s.source_id.slice(0, 8);
  }
}

function sourceHref(s: SearchSource, workspaceSlug: string): string | null {
  // Plane's canonical URLs (verified via apps/web routing). For
  // comments we lack the issue id at this point — we deep-link to
  // the work item's detail page; the comment thread loads with it.
  if (s.source_type === "work_item") {
    return `/${workspaceSlug}/projects/${s.project_id ?? ""}/issues/${s.source_id}`;
  }
  if (s.source_type === "comment") {
    return null; // resolved server-side in a later iteration
  }
  if (s.source_type === "page") {
    return `/${workspaceSlug}/pages/${s.source_id}`;
  }
  return null;
}

function SourceLink({
  source,
  workspaceSlug,
}: {
  source: SearchSource;
  workspaceSlug: string;
}) {
  const href = sourceHref(source, workspaceSlug);
  if (!href) {
    return (
      <span className="text-custom-text-300">{sourceLabel(source)}</span>
    );
  }
  return (
    <a
      href={href}
      className="text-custom-primary-100 hover:underline"
      target="_blank"
      rel="noreferrer"
    >
      {sourceLabel(source)}
    </a>
  );
}

// --- minimal markdown-ish rendering ---------------------------------------

const CITATION_RE = /\[(work_item|comment|page):([0-9a-f-]{36})\]/gi;
const FENCE_RE = /```([\s\S]*?)```/g;

function RenderedAnswer({ text }: { text: string }) {
  // Split into fenced-code segments and prose; render each prose
  // segment with citation linkification, render code segments verbatim.
  const segments = useMemo(() => {
    const out: { kind: "code" | "text"; value: string }[] = [];
    let last = 0;
    const matches = [...text.matchAll(FENCE_RE)];
    for (const m of matches) {
      const start = m.index ?? 0;
      if (start > last) out.push({ kind: "text", value: text.slice(last, start) });
      out.push({ kind: "code", value: m[1] });
      last = start + m[0].length;
    }
    if (last < text.length) out.push({ kind: "text", value: text.slice(last) });
    return out;
  }, [text]);

  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === "code" ? (
          <pre
            key={i}
            className="overflow-x-auto rounded bg-custom-background-80 p-2 text-xs"
          >
            <code>{seg.value}</code>
          </pre>
        ) : (
          <ProseSegment key={i} text={seg.value} />
        )
      )}
    </>
  );
}

function ProseSegment({ text }: { text: string }) {
  // Split text on citation tokens and render the citations as inline
  // muted spans. Real navigation lives in the sidebar — citations
  // inline would clutter the body.
  const parts: (string | { kind: "cite"; raw: string })[] = [];
  let last = 0;
  for (const m of text.matchAll(CITATION_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push(text.slice(last, idx));
    parts.push({ kind: "cite", raw: m[0] });
    last = idx + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));

  // Preserve paragraph breaks (double newline) — Tailwind's `prose`
  // doesn't auto-paragraph plain text.
  return (
    <p className="whitespace-pre-wrap">
      {parts.map((p, i) =>
        typeof p === "string" ? (
          <span key={i}>{p}</span>
        ) : (
          <span
            key={i}
            className="rounded bg-custom-background-80 px-1 text-xs text-custom-text-300"
            title="Источник в правой колонке"
          >
            {p.raw}
          </span>
        )
      )}
    </p>
  );
}

// --- side-panel wrapper ----------------------------------------------------

/**
 * Convenience wrapper that renders AISearch inside a fixed-position
 * slide-over panel. Use as:
 *
 *   <AISearchPanel
 *     open={open}
 *     onClose={() => setOpen(false)}
 *     workspaceId={ws.id}
 *     workspaceSlug={ws.slug}
 *   />
 */
export function AISearchPanel({
  open,
  onClose,
  workspaceId,
  workspaceSlug,
}: {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceSlug: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30">
      <div className="h-full w-full max-w-2xl bg-custom-background-100 shadow-xl">
        <div className="flex items-center justify-between border-b border-custom-border-200 px-4 py-3">
          <div className="text-sm font-medium text-custom-text-200">
            ИИ-поиск по воркспейсу
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-custom-text-300 hover:text-custom-text-100"
          >
            ✕
          </button>
        </div>
        <AISearch
          workspaceId={workspaceId}
          workspaceSlug={workspaceSlug}
        />
      </div>
    </div>
  );
}
