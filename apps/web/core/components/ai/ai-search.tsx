/**
 * AISearch — full AI panel for the planeAI add-on.
 *
 * Two modes via a toggle:
 *   - "Поиск"  — semantic Q&A over indexed work items / comments /
 *                pages. Streams via SSE (useAISearch).
 *   - "Агент"  — natural-language project / issue creation. Runs the
 *                tool-use loop server-side (useAIAgent).
 *
 * Both modes share a single composer with text input + microphone
 * button. The mic uses MediaRecorder (useVoiceRecorder), uploads the
 * blob to /api/ai/.../transcribe/ (useTranscribe), and drops the
 * transcript into the input — user reviews, edits if needed, then
 * presses Send.
 *
 * AISearchPanel at the bottom of this file is the slide-over wrapper
 * mounted from the top navigation (TZ 2.6 + voice expansion).
 */

import { useCallback, useMemo, useState } from "react";

import { useAIAgent, type AgentAction } from "../../hooks/ai/use-ai-agent";
import { useAISearch, type SearchSource } from "../../hooks/ai/use-ai-search";
import { useIndexStatus } from "../../hooks/ai/use-index-status";
import { useTranscribe } from "../../hooks/ai/use-transcribe";
import { useVoiceRecorder } from "../../hooks/ai/use-voice-recorder";

export type AISearchProps = {
  workspaceId: string;
  /** Plane workspace slug — used to build links to work items. */
  workspaceSlug: string;
  className?: string;
  /** Initial tab. Default: "search". */
  initialMode?: "search" | "agent";
};

type Mode = "search" | "agent";

export function AISearch({
  workspaceId,
  workspaceSlug,
  className = "",
  initialMode = "search",
}: AISearchProps) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [draft, setDraft] = useState("");

  // Search-mode plumbing
  const { answer, sources, status, error: searchErr, search, cancel } =
    useAISearch(workspaceId);
  // Index status drives both the input-disabled gate and the banner.
  // We poll while it's filling and stop on ready=true (hook handles it).
  const { data: index, loading: indexLoading } = useIndexStatus(workspaceId, 5000);

  // Agent-mode plumbing
  const {
    result: agentResult,
    loading: agentLoading,
    error: agentErr,
    run: runAgent,
    reset: resetAgent,
  } = useAIAgent(workspaceId);

  // Voice — mic + Whisper
  const voice = useVoiceRecorder();
  const { loading: transcribing, error: transcribeErr, transcribe } = useTranscribe(workspaceId);

  const isStreaming = status === "streaming";
  // Search mode requires a ready index; agent mode does not (it
  // doesn't query embeddings, it only creates things).
  const indexReady = index?.ready ?? true;
  const inputDisabled =
    mode === "search" ? !indexReady : false;
  const busy = isStreaming || agentLoading || voice.state !== "idle" || transcribing;
  const canSubmit = draft.trim().length > 0 && !busy && !inputDisabled;

  const onMicToggle = useCallback(async () => {
    if (voice.state === "recording") {
      const blob = await voice.stop();
      if (!blob) return;
      const text = await transcribe(blob);
      if (text) {
        // Append (don't replace) so a user can dictate over a typed prefix.
        setDraft((prev) => (prev ? `${prev.trim()} ${text}` : text));
      }
    } else if (voice.state === "idle") {
      void voice.start();
    }
  }, [voice, transcribe]);

  const onSubmit = useCallback(
    (e?: React.FormEvent) => {
      e?.preventDefault();
      const q = draft.trim();
      if (!q) return;
      if (mode === "search") {
        void search(q);
      } else {
        resetAgent();
        void runAgent(q);
      }
    },
    [draft, mode, search, runAgent, resetAgent]
  );

  const onChangeMode = useCallback((next: Mode) => {
    setMode(next);
    // Don't clear draft — user may want to send the same text in
    // either mode (e.g. "что известно про X" — search, then "создай
    // проект X с задачами Y и Z" — agent).
  }, []);

  const error =
    mode === "search" ? searchErr : agentErr || transcribeErr || voice.error;

  return (
    <div className={`flex flex-col gap-3 p-4 ${className}`}>
      <ModeToggle mode={mode} onChange={onChangeMode} />

      {mode === "search" && (
        <IndexBanner index={index ?? null} loading={indexLoading} />
      )}

      <form onSubmit={onSubmit} className="flex gap-2">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={
            mode === "search"
              ? "Спросите по задачам, комментариям, страницам…"
              : "Например: «создай проект Маркетинг и в нём задачу „Сайт“ исполнитель Илья»"
          }
          disabled={inputDisabled || voice.state !== "idle"}
          className="flex-1 rounded border border-custom-border-200 bg-custom-background-90 px-3 py-2 text-sm outline-none focus:border-custom-primary-100 disabled:opacity-50"
        />
        <MicButton
          state={voice.state}
          transcribing={transcribing}
          durationMs={voice.durationMs}
          onClick={onMicToggle}
          disabled={busy && voice.state === "idle"}
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
            disabled={!canSubmit}
            className="rounded bg-custom-primary-100 px-3 py-2 text-sm text-white hover:bg-custom-primary-200 disabled:opacity-50"
          >
            {mode === "search" ? "Спросить" : "Выполнить"}
          </button>
        )}
      </form>

      {error && (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {mode === "search" ? (
        <AnswerPanel
          answer={answer}
          sources={sources}
          workspaceSlug={workspaceSlug}
          status={status}
        />
      ) : (
        <AgentPanel
          loading={agentLoading}
          result={agentResult}
          workspaceSlug={workspaceSlug}
        />
      )}
    </div>
  );
}

// --- mode toggle ----------------------------------------------------------

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  const item = (val: Mode, label: string) => (
    <button
      type="button"
      onClick={() => onChange(val)}
      className={`flex-1 rounded px-3 py-1.5 text-xs font-medium transition-colors ${
        mode === val
          ? "bg-custom-primary-100 text-white"
          : "bg-custom-background-80 text-custom-text-200 hover:bg-custom-background-70"
      }`}
    >
      {label}
    </button>
  );
  return (
    <div className="flex gap-1 rounded bg-custom-background-90 p-1">
      {item("search", "🔍 Поиск")}
      {item("agent", "⚙️ Агент")}
    </div>
  );
}

// --- mic button -----------------------------------------------------------

function MicButton({
  state,
  transcribing,
  durationMs,
  onClick,
  disabled,
}: {
  state: ReturnType<typeof useVoiceRecorder>["state"];
  transcribing: boolean;
  durationMs: number;
  onClick: () => void;
  disabled: boolean;
}) {
  const recording = state === "recording";
  const processing = state === "processing" || state === "requesting" || transcribing;
  const seconds = Math.floor(durationMs / 1000);
  const label = recording
    ? `⏺ ${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`
    : processing
      ? "…"
      : "🎙";
  const title = recording
    ? "Нажмите, чтобы остановить запись"
    : processing
      ? "Распознаём…"
      : "Запись голосом";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || processing}
      title={title}
      aria-label={title}
      className={`rounded px-3 py-2 text-sm transition-colors ${
        recording
          ? "bg-red-500/20 text-red-600 hover:bg-red-500/30 animate-pulse"
          : "bg-custom-background-80 text-custom-text-200 hover:bg-custom-background-70"
      } disabled:opacity-50`}
    >
      {label}
    </button>
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
        В воркспейсе пока нет задач для индексации. Поиск будет полезен после создания первой задачи.
      </div>
    );
  }
  if (index.ready) {
    return (
      <div className="rounded bg-green-50 px-3 py-2 text-xs text-green-700">
        Индекс готов ({index.indexed}/{index.total}, {Math.round(index.coverage * 100)}%).
      </div>
    );
  }
  return (
    <div className="rounded bg-yellow-50 px-3 py-2 text-xs text-yellow-800">
      <div className="mb-1">
        Идёт индексация: {index.indexed}/{index.total} ({Math.round(index.coverage * 100)}%). Поиск временно выключен — вернитесь через минуту.
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded bg-yellow-100">
        <div className="h-full bg-yellow-400 transition-all" style={{ width: `${index.coverage * 100}%` }} />
      </div>
    </div>
  );
}

// --- search-mode answer + sources -----------------------------------------

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

function SourcesSidebar({ sources, workspaceSlug }: { sources: SearchSource[]; workspaceSlug: string }) {
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
  if (s.source_type === "work_item") {
    return `/${workspaceSlug}/projects/${s.project_id ?? ""}/issues/${s.source_id}`;
  }
  if (s.source_type === "comment") return null;
  if (s.source_type === "page") return `/${workspaceSlug}/pages/${s.source_id}`;
  return null;
}

function SourceLink({ source, workspaceSlug }: { source: SearchSource; workspaceSlug: string }) {
  const href = sourceHref(source, workspaceSlug);
  if (!href) return <span className="text-custom-text-300">{sourceLabel(source)}</span>;
  return (
    <a href={href} className="text-custom-primary-100 hover:underline" target="_blank" rel="noreferrer">
      {sourceLabel(source)}
    </a>
  );
}

// --- agent-mode panel -----------------------------------------------------

function AgentPanel({
  loading,
  result,
  workspaceSlug,
}: {
  loading: boolean;
  result: import("../../hooks/ai/use-ai-agent").AgentResult | null;
  workspaceSlug: string;
}) {
  if (loading) {
    return (
      <div className="rounded bg-custom-background-90 px-3 py-4 text-sm text-custom-text-300">
        <span className="animate-pulse">⚙️ Агент работает… выполняет шаги, создаёт проекты и задачи.</span>
      </div>
    );
  }
  if (!result) {
    return (
      <div className="rounded bg-custom-background-90 px-3 py-4 text-sm text-custom-text-300">
        Опишите задачу — например: «создай проект „Маркетинг“ и в нём задачи: дизайн лендинга, тексты, аналитика. Назначь исполнителя Илья».
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <article className="prose prose-sm max-w-none rounded border border-custom-border-200 bg-custom-background-90 p-4 text-sm whitespace-pre-wrap">
        {result.reply}
      </article>
      {result.actions.length > 0 && (
        <details className="rounded border border-custom-border-200 bg-custom-background-90 p-3 text-xs" open>
          <summary className="cursor-pointer text-custom-text-200">
            Действия ({result.actions.length}) · {result.turns} шаг{result.turns === 1 ? "" : result.turns < 5 ? "а" : "ов"} · ${result.total_cost_usd}
          </summary>
          <ul className="mt-2 flex flex-col gap-1">
            {result.actions.map((a, i) => (
              <ActionLine key={i} action={a} workspaceSlug={workspaceSlug} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function ActionLine({ action, workspaceSlug }: { action: AgentAction; workspaceSlug: string }) {
  const ok = action.ok;
  const result = action.result as Record<string, unknown>;
  let summary = "";
  let href: string | null = null;

  if (action.tool === "create_project" && ok) {
    summary = `📁 Проект «${result["name"]}» (${result["identifier"]})`;
    href = `/${workspaceSlug}/projects/${result["project_id"]}/issues/`;
  } else if (action.tool === "create_issue" && ok) {
    const priority = result["priority"];
    summary = `✅ Задача «${result["name"]}»${priority && priority !== "none" ? ` · ${priority}` : ""}`;
    href = result["project_id"] && result["issue_id"]
      ? `/${workspaceSlug}/projects/${result["project_id"]}/issues/${result["issue_id"]}`
      : null;
  } else if (action.tool === "list_projects" && ok) {
    const ps = (result["projects"] as unknown[]) ?? [];
    summary = `👁 Прочитал список проектов (${ps.length})`;
  } else if (action.tool === "list_members" && ok) {
    const ms = (result["members"] as unknown[]) ?? [];
    summary = `👁 Прочитал список участников (${ms.length})`;
  } else if (!ok) {
    summary = `⚠️ ${action.tool}: ${String(result["error"] ?? "ошибка")}`;
  } else {
    summary = `• ${action.tool}`;
  }

  return (
    <li className={ok ? "text-custom-text-200" : "text-yellow-700"}>
      {href ? (
        <a href={href} className="hover:underline" target="_blank" rel="noreferrer">
          {summary}
        </a>
      ) : (
        summary
      )}
    </li>
  );
}

// --- minimal markdown rendering for search answers ------------------------

const CITATION_RE = /\[(work_item|comment|page):([0-9a-f-]{36})\]/gi;
const FENCE_RE = /```([\s\S]*?)```/g;

function RenderedAnswer({ text }: { text: string }) {
  const segments = useMemo(() => {
    const out: { kind: "code" | "text"; value: string }[] = [];
    let last = 0;
    for (const m of [...text.matchAll(FENCE_RE)]) {
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
          <pre key={i} className="overflow-x-auto rounded bg-custom-background-80 p-2 text-xs">
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
  const parts: (string | { kind: "cite"; raw: string })[] = [];
  let last = 0;
  for (const m of text.matchAll(CITATION_RE)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push(text.slice(last, idx));
    parts.push({ kind: "cite", raw: m[0] });
    last = idx + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
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
      <div className="h-full w-full max-w-2xl overflow-y-auto bg-custom-background-100 shadow-xl">
        <div className="flex items-center justify-between border-b border-custom-border-200 px-4 py-3">
          <div className="text-sm font-medium text-custom-text-200">ИИ-помощник</div>
          <button
            type="button"
            onClick={onClose}
            className="text-custom-text-300 hover:text-custom-text-100"
            aria-label="Закрыть"
          >
            ✕
          </button>
        </div>
        <AISearch workspaceId={workspaceId} workspaceSlug={workspaceSlug} />
      </div>
    </div>
  );
}
