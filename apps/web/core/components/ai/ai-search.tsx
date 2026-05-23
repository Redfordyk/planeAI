/**
 * AISearch — full AI panel for the planeAI add-on.
 *
 * Two modes via a toggle:
 *   - "Поиск"  — semantic Q&A over indexed work items / comments /
 *                pages. Streams via SSE (useAISearch).
 *   - "Агент"  — natural-language project / issue creation. Runs the
 *                tool-use loop server-side (useAIAgent).
 *
 * Both modes share a composer with text input + microphone button.
 * The mic uses MediaRecorder (useVoiceRecorder), uploads the blob to
 * /api/ai/.../transcribe/ (useTranscribe), and drops the transcript
 * into the input — user reviews, edits, then presses Send.
 *
 * AISearchPanel at the bottom of this file is the slide-over wrapper
 * mounted from the top navigation.
 */

import { useCallback, useMemo, useState } from "react";

import { useAIAgent, type AgentAction } from "../../hooks/ai/use-ai-agent";
import { useAISearch, type SearchSource } from "../../hooks/ai/use-ai-search";
import { useIndexStatus } from "../../hooks/ai/use-index-status";
import { useTranscribe } from "../../hooks/ai/use-transcribe";
import { useVoiceRecorder } from "../../hooks/ai/use-voice-recorder";

export type AISearchProps = {
  workspaceId: string;
  workspaceSlug: string;
  className?: string;
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

  const { answer, sources, status, error: searchErr, search, cancel } =
    useAISearch(workspaceId);
  const { data: index, loading: indexLoading } = useIndexStatus(workspaceId, 5000);

  const {
    result: agentResult,
    loading: agentLoading,
    error: agentErr,
    run: runAgent,
    reset: resetAgent,
  } = useAIAgent(workspaceId);

  const voice = useVoiceRecorder();
  const { loading: transcribing, error: transcribeErr, transcribe } = useTranscribe(workspaceId);

  const isStreaming = status === "streaming";
  const indexReady = index?.ready ?? true;
  const inputDisabled = mode === "search" ? !indexReady : false;
  const busy = isStreaming || agentLoading || voice.state !== "idle" || transcribing;
  const canSubmit = draft.trim().length > 0 && !busy && !inputDisabled;

  const onMicToggle = useCallback(async () => {
    if (voice.state === "recording") {
      const blob = await voice.stop();
      if (!blob) return;
      const text = await transcribe(blob);
      if (text) {
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

  const error =
    mode === "search" ? searchErr : agentErr || transcribeErr || voice.error;

  return (
    <div className={`flex h-full flex-col gap-4 px-5 py-5 ${className}`}>
      <ModeToggle mode={mode} onChange={setMode} />

      {mode === "search" && (
        <IndexBanner index={index ?? null} loading={indexLoading} />
      )}

      <Composer
        draft={draft}
        setDraft={setDraft}
        mode={mode}
        inputDisabled={inputDisabled}
        voice={voice}
        transcribing={transcribing}
        onMicToggle={onMicToggle}
        onSubmit={onSubmit}
        isStreaming={isStreaming}
        canSubmit={canSubmit}
        onCancel={cancel}
      />

      {error && <ErrorBanner message={error} />}

      <div className="flex-1 min-h-0 overflow-y-auto pr-1">
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
    </div>
  );
}

// --- mode toggle ----------------------------------------------------------

function ModeToggle({
  mode,
  onChange,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
}) {
  const item = (val: Mode, label: string, icon: string, hint: string) => {
    const active = mode === val;
    return (
      <button
        type="button"
        onClick={() => onChange(val)}
        className={`group flex-1 rounded-lg px-4 py-3 text-left transition-all ${
          active
            ? "bg-custom-primary-100 text-white shadow-md"
            : "bg-custom-background-90 text-custom-text-200 hover:bg-custom-background-80"
        }`}
      >
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span className="text-lg leading-none">{icon}</span>
          <span>{label}</span>
        </div>
        <div className={`mt-0.5 text-[11px] leading-tight ${active ? "text-white/80" : "text-custom-text-300"}`}>
          {hint}
        </div>
      </button>
    );
  };
  return (
    <div className="flex gap-2">
      {item("search", "Поиск", "🔍", "Спросить по задачам")}
      {item("agent", "Агент", "⚙️", "Создать проект/задачи")}
    </div>
  );
}

// --- composer (input + mic + submit) --------------------------------------

function Composer({
  draft,
  setDraft,
  mode,
  inputDisabled,
  voice,
  transcribing,
  onMicToggle,
  onSubmit,
  isStreaming,
  canSubmit,
  onCancel,
}: {
  draft: string;
  setDraft: (v: string) => void;
  mode: Mode;
  inputDisabled: boolean;
  voice: ReturnType<typeof useVoiceRecorder>;
  transcribing: boolean;
  onMicToggle: () => void;
  onSubmit: (e?: React.FormEvent) => void;
  isStreaming: boolean;
  canSubmit: boolean;
  onCancel: () => void;
}) {
  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-2">
      <div className="relative flex items-stretch rounded-xl border border-custom-border-200 bg-custom-background-90 focus-within:border-custom-primary-100 focus-within:ring-2 focus-within:ring-custom-primary-100/20 transition-colors">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (canSubmit) onSubmit();
            }
          }}
          placeholder={
            mode === "search"
              ? "Спросите по задачам, комментариям, страницам…"
              : "Например: «создай проект Маркетинг и в нём задачи: лендинг, тексты, аналитика. Назначь Илью»"
          }
          disabled={inputDisabled || voice.state !== "idle"}
          rows={2}
          className="flex-1 resize-none bg-transparent px-4 py-3 text-sm leading-snug outline-none placeholder:text-custom-text-300 disabled:opacity-50"
        />
        <div className="flex items-end gap-1 px-2 pb-2">
          <MicButton
            state={voice.state}
            transcribing={transcribing}
            durationMs={voice.durationMs}
            onClick={onMicToggle}
            disabled={inputDisabled}
          />
        </div>
      </div>

      <div className="flex items-center justify-between text-[11px] text-custom-text-300">
        <span>
          <kbd className="rounded border border-custom-border-200 px-1.5 py-0.5 font-mono">Enter</kbd>
          {" "}— отправить, <kbd className="rounded border border-custom-border-200 px-1.5 py-0.5 font-mono">Shift+Enter</kbd> — перенос строки
        </span>
        {isStreaming ? (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md bg-custom-background-80 px-3 py-1.5 text-xs font-medium hover:bg-custom-background-70"
          >
            ⏹ Стоп
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSubmit}
            className="rounded-md bg-gradient-to-r from-custom-primary-100 to-custom-primary-200 px-4 py-1.5 text-xs font-semibold text-white shadow-sm hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {mode === "search" ? "Спросить →" : "Выполнить →"}
          </button>
        )}
      </div>
    </form>
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
  const timer = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  const title = recording
    ? "Нажмите, чтобы остановить запись"
    : processing
      ? "Распознаём…"
      : "Запись голосом";

  if (recording) {
    return (
      <button
        type="button"
        onClick={onClick}
        title={title}
        aria-label={title}
        className="flex h-9 items-center gap-1.5 rounded-md bg-red-500 px-3 text-xs font-mono text-white shadow-sm animate-pulse"
      >
        <span className="h-2 w-2 rounded-full bg-white" />
        {timer}
      </button>
    );
  }
  if (processing) {
    return (
      <button
        type="button"
        disabled
        title={title}
        aria-label={title}
        className="flex h-9 w-9 items-center justify-center rounded-md bg-custom-background-80 text-custom-text-300"
      >
        <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-custom-text-300 border-t-transparent" />
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className="flex h-9 w-9 items-center justify-center rounded-md bg-custom-background-80 text-custom-text-200 hover:bg-custom-background-70 hover:text-custom-primary-100 disabled:opacity-40"
    >
      <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 1a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z"/>
        <path d="M19 10v1a7 7 0 0 1-14 0v-1"/>
        <line x1="12" y1="18" x2="12" y2="22"/>
        <line x1="8" y1="22" x2="16" y2="22"/>
      </svg>
    </button>
  );
}

// --- index banner ---------------------------------------------------------

function IndexBanner({
  index,
  loading,
}: {
  index: import("../../hooks/ai/use-index-status").IndexStatus | null;
  loading: boolean;
}) {
  if (loading && !index) {
    return (
      <div className="rounded-lg bg-custom-background-90 px-3 py-2 text-xs text-custom-text-300">
        Загрузка статуса индексации…
      </div>
    );
  }
  if (!index) return null;
  if (index.total === 0) {
    return (
      <div className="rounded-lg border border-custom-border-100 bg-custom-background-90 px-3 py-2 text-xs text-custom-text-300">
        В воркспейсе пока нет задач для индексации. Поиск заработает после первой задачи.
      </div>
    );
  }
  if (index.ready) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-700 dark:border-green-900 dark:bg-green-950 dark:text-green-300">
        <span>✓</span>
        <span>Индекс готов: {index.indexed} из {index.total} ({Math.round(index.coverage * 100)}%)</span>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-yellow-200 bg-yellow-50 px-3 py-2 text-xs text-yellow-800 dark:border-yellow-900 dark:bg-yellow-950 dark:text-yellow-200">
      <div className="mb-1 flex items-center justify-between">
        <span>⏳ Индексация: {index.indexed}/{index.total} ({Math.round(index.coverage * 100)}%)</span>
        <span className="text-[10px] opacity-70">Поиск временно выключен</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-yellow-100 dark:bg-yellow-900">
        <div className="h-full bg-yellow-500 transition-all" style={{ width: `${index.coverage * 100}%` }} />
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
      <span aria-hidden="true">⚠️</span>
      <span className="flex-1">{message}</span>
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
    return <EmptyState mode="search" />;
  }
  return (
    <div className="flex flex-col gap-4 md:grid md:grid-cols-[1fr_220px] md:gap-4">
      <article className="prose prose-sm max-w-none rounded-xl border border-custom-border-200 bg-custom-background-90 p-4 text-sm leading-relaxed">
        <RenderedAnswer text={answer} />
        {status === "streaming" && (
          <span className="ml-1 inline-block h-3 w-2 animate-pulse bg-custom-text-300 align-middle" />
        )}
      </article>
      <SourcesSidebar sources={sources} workspaceSlug={workspaceSlug} />
    </div>
  );
}

function EmptyState({ mode }: { mode: Mode }) {
  if (mode === "search") {
    return (
      <div className="rounded-xl border border-dashed border-custom-border-200 bg-custom-background-90 px-4 py-8 text-center">
        <div className="text-3xl">💡</div>
        <div className="mt-2 text-sm font-medium text-custom-text-200">Спросите что-нибудь по проекту</div>
        <div className="mt-1 text-xs text-custom-text-300">
          Например: «что известно про новый релиз?», «какие задачи у Ильи?»
        </div>
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-dashed border-custom-border-200 bg-custom-background-90 px-4 py-8 text-center">
      <div className="text-3xl">✨</div>
      <div className="mt-2 text-sm font-medium text-custom-text-200">ИИ-агент создаст проект и задачи</div>
      <div className="mt-1 text-xs text-custom-text-300">
        Например: «создай проект „Маркетинг“ и в нём задачи: лендинг, тексты, аналитика. Назначь Илью».
      </div>
    </div>
  );
}

function SourcesSidebar({ sources, workspaceSlug }: { sources: SearchSource[]; workspaceSlug: string }) {
  if (!sources.length) {
    return (
      <aside className="rounded-xl border border-dashed border-custom-border-200 bg-custom-background-90 p-3 text-xs text-custom-text-300">
        Источники появятся здесь
      </aside>
    );
  }
  return (
    <aside className="rounded-xl border border-custom-border-200 bg-custom-background-90 p-3 text-xs">
      <div className="mb-2 font-semibold text-custom-text-200">Источники</div>
      <ul className="flex flex-col gap-1.5">
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
      return `🎯 Задача · ${s.source_id.slice(0, 8)}`;
    case "comment":
      return `💬 Комментарий · ${s.source_id.slice(0, 8)}`;
    case "page":
      return `📄 Страница · ${s.source_id.slice(0, 8)}`;
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
    <a href={href} className="block rounded px-1.5 py-1 text-custom-primary-100 hover:bg-custom-background-80 hover:underline" target="_blank" rel="noreferrer">
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
      <div className="flex items-center gap-3 rounded-xl border border-custom-border-200 bg-custom-background-90 px-4 py-6 text-sm">
        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-custom-primary-100 border-t-transparent" />
        <span className="text-custom-text-200">⚙️ Агент работает… выполняет шаги, создаёт проекты и задачи.</span>
      </div>
    );
  }
  if (!result) {
    return <EmptyState mode="agent" />;
  }
  return (
    <div className="flex flex-col gap-3">
      <article className="rounded-xl border border-custom-border-200 bg-custom-background-90 p-4 text-sm leading-relaxed whitespace-pre-wrap">
        {result.reply}
      </article>
      {result.actions.length > 0 && (
        <details
          className="rounded-xl border border-custom-border-200 bg-custom-background-90 p-3 text-xs"
          open
        >
          <summary className="cursor-pointer text-custom-text-200 select-none">
            <span className="font-semibold">Действия</span> · {result.actions.length} вызов{plural(result.actions.length, "", "а", "ов")} · {result.turns} шаг{plural(result.turns, "", "а", "ов")} · ${result.total_cost_usd}
          </summary>
          <ul className="mt-2 flex flex-col gap-1.5">
            {result.actions.map((a, i) => (
              <ActionLine key={i} action={a} workspaceSlug={workspaceSlug} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

function ActionLine({ action, workspaceSlug }: { action: AgentAction; workspaceSlug: string }) {
  const ok = action.ok;
  const result = action.result as Record<string, unknown>;
  let summary = "";
  let href: string | null = null;

  if (action.tool === "create_project" && ok) {
    const reused = result["reused"] === true;
    summary = `📁 ${reused ? "Использован существующий" : "Создан"} проект «${result["name"]}» (${result["identifier"]})`;
    href = `/${workspaceSlug}/projects/${result["project_id"]}/issues/`;
  } else if (action.tool === "create_issue" && ok) {
    const priority = result["priority"];
    summary = `✅ Задача «${result["name"]}»${priority && priority !== "none" ? ` · ${priority}` : ""}`;
    href = result["project_id"] && result["issue_id"]
      ? `/${workspaceSlug}/projects/${result["project_id"]}/issues/${result["issue_id"]}`
      : null;
  } else if (action.tool === "list_projects" && ok) {
    const ps = (result["projects"] as unknown[]) ?? [];
    summary = `👁 Список проектов (${ps.length})`;
  } else if (action.tool === "list_members" && ok) {
    const ms = (result["members"] as unknown[]) ?? [];
    summary = `👁 Список участников (${ms.length})`;
  } else if (!ok) {
    summary = `⚠️ ${action.tool}: ${String(result["error"] ?? "ошибка")}`;
  } else {
    summary = `• ${action.tool}`;
  }

  return (
    <li className={ok ? "text-custom-text-200" : "text-yellow-700 dark:text-yellow-300"}>
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
            className="rounded bg-custom-background-80 px-1.5 py-0.5 text-[11px] font-mono text-custom-text-300"
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
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full w-full max-w-2xl flex-col bg-custom-background-100 shadow-2xl">
        <header className="flex items-center justify-between border-b border-custom-border-200 px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="text-lg">✨</span>
            <div>
              <div className="text-sm font-semibold text-custom-text-100">ИИ-помощник</div>
              <div className="text-[11px] text-custom-text-300">DeepSeek + OpenAI embeddings</div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-custom-text-300 hover:bg-custom-background-80 hover:text-custom-text-100"
            aria-label="Закрыть"
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </header>
        <div className="flex-1 min-h-0 overflow-hidden">
          <AISearch workspaceId={workspaceId} workspaceSlug={workspaceSlug} />
        </div>
      </div>
    </div>
  );
}
