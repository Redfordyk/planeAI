/**
 * AISearch — full AI panel for the planeAI add-on (native Plane styling).
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
 * Visuals: zero emojis, lucide icons everywhere, all colors via
 * Plane's semantic tokens (bg-layer-*, text-primary/secondary/tertiary,
 * border-strong/subtle-1, accent-*, danger-*, warning-*, success-*).
 * Buttons from @plane/propel.
 */

import { useCallback, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  ArrowUp,
  Bot,
  CheckCircle2,
  Clock,
  Eye,
  FileText,
  FolderKanban,
  Hourglass,
  Lightbulb,
  ListChecks,
  Loader2,
  MessageSquare,
  Mic,
  Search,
  Sparkles,
  Square,
  Target,
  Wand2,
  X,
} from "lucide-react";
import { Button } from "@plane/propel/button";

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
    <div className={`flex h-full flex-col gap-4 bg-layer-1 px-5 py-5 ${className}`}>
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

// --- mode toggle ---------------------------------------------------------

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  const item = (val: Mode, label: string, Icon: typeof Search, hint: string) => {
    const active = val === mode;
    return (
      <button
        type="button"
        onClick={() => onChange(val)}
        className={`group flex-1 rounded-md border px-3 py-2.5 text-left transition-colors ${
          active
            ? "border-accent-strong bg-accent-subtle text-accent-primary"
            : "border-strong bg-layer-2 text-secondary hover:bg-layer-2-hover hover:text-primary"
        }`}
      >
        <div className="flex items-center gap-2 text-body-sm font-semibold">
          <Icon className="size-4" strokeWidth={2} />
          <span>{label}</span>
        </div>
        <div
          className={`mt-0.5 text-caption-md leading-tight ${
            active ? "text-accent-primary/80" : "text-tertiary"
          }`}
        >
          {hint}
        </div>
      </button>
    );
  };
  return (
    <div className="flex gap-2">
      {item("search", "Поиск", Search, "Спросить по задачам")}
      {item("agent", "Агент", Wand2, "Создать проект / задачи")}
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
      <div className="relative flex items-stretch rounded-md border border-strong bg-layer-2 transition-colors focus-within:border-accent-strong">
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
          className="flex-1 resize-none bg-transparent px-3 py-2.5 text-body-sm leading-snug text-primary outline-none placeholder:text-placeholder disabled:opacity-50"
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

      <div className="flex items-center justify-between text-caption-md text-tertiary">
        <span className="flex items-center gap-1.5">
          <kbd className="rounded border border-strong bg-layer-2 px-1.5 py-0.5 font-mono text-caption-md">
            Enter
          </kbd>
          <span>— отправить,</span>
          <kbd className="rounded border border-strong bg-layer-2 px-1.5 py-0.5 font-mono text-caption-md">
            Shift+Enter
          </kbd>
          <span>— перенос</span>
        </span>
        {isStreaming ? (
          <Button
            variant="secondary"
            size="base"
            onClick={onCancel}
            type="button"
            prependIcon={<Square />}
          >
            Стоп
          </Button>
        ) : (
          <Button
            variant="primary"
            size="base"
            type="submit"
            disabled={!canSubmit}
            appendIcon={<ArrowRight />}
          >
            {mode === "search" ? "Спросить" : "Выполнить"}
          </Button>
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
        className="flex h-8 items-center gap-1.5 rounded-md bg-danger-primary px-2.5 text-caption-md font-mono text-on-color animate-pulse"
      >
        <span className="size-2 rounded-full bg-on-color" />
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
        className="flex size-8 items-center justify-center rounded-md bg-layer-3 text-tertiary"
      >
        <Loader2 className="size-3.5 animate-spin" />
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
      className="flex size-8 items-center justify-center rounded-md bg-layer-3 text-secondary transition-colors hover:bg-layer-3-hover hover:text-accent-primary disabled:opacity-40"
    >
      <Mic className="size-4" strokeWidth={2} />
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
      <div className="flex items-center gap-2 rounded-md border border-subtle-1 bg-layer-2 px-3 py-2 text-caption-md text-tertiary">
        <Loader2 className="size-3.5 animate-spin" />
        Загрузка статуса индексации…
      </div>
    );
  }
  if (!index) return null;
  if (index.total === 0) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-subtle-1 bg-layer-2 px-3 py-2 text-caption-md text-tertiary">
        <FileText className="size-3.5" />
        В воркспейсе пока нет задач для индексации.
      </div>
    );
  }
  if (index.ready) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-success-strong bg-success-subtle-1 px-3 py-2 text-caption-md text-success-primary">
        <CheckCircle2 className="size-3.5" strokeWidth={2} />
        Индекс готов: {index.indexed} из {index.total} ({Math.round(index.coverage * 100)}%)
      </div>
    );
  }
  return (
    <div className="rounded-md border border-warning-strong bg-warning-subtle px-3 py-2 text-caption-md text-warning-primary">
      <div className="mb-1 flex items-center justify-between">
        <span className="flex items-center gap-1.5">
          <Hourglass className="size-3.5" />
          Индексация: {index.indexed}/{index.total} ({Math.round(index.coverage * 100)}%)
        </span>
        <span className="text-caption-sm opacity-70">Поиск временно выключен</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-warning-subtle-hover">
        <div
          className="h-full bg-warning-primary transition-all"
          style={{ width: `${index.coverage * 100}%` }}
        />
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-danger-strong bg-danger-subtle px-3 py-2 text-body-sm text-danger-primary">
      <AlertCircle className="mt-0.5 size-4 shrink-0" strokeWidth={2} />
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
      <article className="prose prose-sm max-w-none rounded-md border border-strong bg-layer-2 p-4 text-body-sm leading-relaxed text-primary">
        <RenderedAnswer text={answer} />
        {status === "streaming" && (
          <span className="ml-1 inline-block h-3 w-2 animate-pulse bg-tertiary align-middle" />
        )}
      </article>
      <SourcesSidebar sources={sources} workspaceSlug={workspaceSlug} />
    </div>
  );
}

function EmptyState({ mode }: { mode: Mode }) {
  if (mode === "search") {
    return (
      <div className="flex flex-col items-center gap-2 rounded-md border border-dashed border-strong bg-layer-2 px-4 py-8 text-center">
        <Lightbulb className="size-6 text-tertiary" strokeWidth={1.75} />
        <div className="text-body-sm font-medium text-secondary">
          Спросите что-нибудь по проекту
        </div>
        <div className="text-body-xs text-tertiary">
          Например: «что известно про новый релиз?», «какие задачи у Ильи?»
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-2 rounded-md border border-dashed border-strong bg-layer-2 px-4 py-8 text-center">
      <Sparkles className="size-6 text-accent-primary" strokeWidth={1.75} />
      <div className="text-body-sm font-medium text-secondary">
        ИИ-агент создаст проект и задачи
      </div>
      <div className="text-body-xs text-tertiary">
        Например: «создай проект „Маркетинг“ и в нём задачи: лендинг, тексты, аналитика. Назначь Илью».
      </div>
    </div>
  );
}

function SourcesSidebar({ sources, workspaceSlug }: { sources: SearchSource[]; workspaceSlug: string }) {
  if (!sources.length) {
    return (
      <aside className="rounded-md border border-dashed border-strong bg-layer-2 p-3 text-body-xs text-tertiary">
        Источники появятся здесь
      </aside>
    );
  }
  return (
    <aside className="rounded-md border border-strong bg-layer-2 p-3 text-body-xs">
      <div className="mb-2 font-semibold text-secondary">Источники</div>
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

function sourceIcon(source_type: string): typeof Target {
  switch (source_type) {
    case "work_item":
      return Target;
    case "comment":
      return MessageSquare;
    case "page":
      return FileText;
    default:
      return FileText;
  }
}

function sourceLabel(s: SearchSource): { Icon: typeof Target; label: string; suffix: string } {
  switch (s.source_type) {
    case "work_item":
      return { Icon: Target, label: "Задача", suffix: s.source_id.slice(0, 8) };
    case "comment":
      return { Icon: MessageSquare, label: "Комментарий", suffix: s.source_id.slice(0, 8) };
    case "page":
      return { Icon: FileText, label: "Страница", suffix: s.source_id.slice(0, 8) };
    default:
      return { Icon: FileText, label: "Источник", suffix: s.source_id.slice(0, 8) };
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
  const { Icon, label, suffix } = sourceLabel(source);
  const inner = (
    <span className="flex items-center gap-1.5">
      <Icon className="size-3 shrink-0 text-tertiary" strokeWidth={2} />
      <span className="text-secondary">{label}</span>
      <span className="font-mono text-tertiary">·</span>
      <span className="font-mono text-tertiary">{suffix}</span>
    </span>
  );
  if (!href) {
    return <span className="block px-1.5 py-1">{inner}</span>;
  }
  return (
    <a
      href={href}
      className="block rounded px-1.5 py-1 text-accent-primary hover:bg-layer-3 hover:underline"
      target="_blank"
      rel="noreferrer"
    >
      {inner}
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
      <div className="flex items-center gap-3 rounded-md border border-strong bg-layer-2 px-4 py-5 text-body-sm">
        <Loader2 className="size-4 animate-spin text-accent-primary" strokeWidth={2} />
        <span className="text-secondary">
          Агент работает — выполняет шаги, создаёт проекты и задачи…
        </span>
      </div>
    );
  }
  if (!result) {
    return <EmptyState mode="agent" />;
  }
  return (
    <div className="flex flex-col gap-3">
      <article className="rounded-md border border-strong bg-layer-2 p-4 text-body-sm leading-relaxed text-primary whitespace-pre-wrap">
        {result.reply}
      </article>
      {result.actions.length > 0 && (
        <details
          className="rounded-md border border-strong bg-layer-2 p-3 text-body-xs"
          open
        >
          <summary className="flex cursor-pointer items-center gap-1.5 select-none text-secondary">
            <ListChecks className="size-3.5 text-tertiary" strokeWidth={2} />
            <span className="font-semibold">Действия</span>
            <span className="text-tertiary">
              · {result.actions.length} вызов{plural(result.actions.length, "", "а", "ов")}
              · {result.turns} шаг{plural(result.turns, "", "а", "ов")}
              · ${result.total_cost_usd}
            </span>
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
  let Icon: typeof Target = Target;
  let summary = "";
  let href: string | null = null;
  let tone: "ok" | "warn" = ok ? "ok" : "warn";

  if (action.tool === "create_project" && ok) {
    const reused = result["reused"] === true;
    Icon = FolderKanban;
    summary = `${reused ? "Использован существующий" : "Создан"} проект «${result["name"]}» (${result["identifier"]})`;
    href = `/${workspaceSlug}/projects/${result["project_id"]}/issues/`;
  } else if (action.tool === "create_issue" && ok) {
    const priority = result["priority"];
    Icon = CheckCircle2;
    summary = `Задача «${result["name"]}»${
      priority && priority !== "none" ? ` · ${priority}` : ""
    }`;
    href = result["project_id"] && result["issue_id"]
      ? `/${workspaceSlug}/projects/${result["project_id"]}/issues/${result["issue_id"]}`
      : null;
  } else if (action.tool === "list_projects" && ok) {
    const ps = (result["projects"] as unknown[]) ?? [];
    Icon = Eye;
    summary = `Список проектов (${ps.length})`;
  } else if (action.tool === "list_members" && ok) {
    const ms = (result["members"] as unknown[]) ?? [];
    Icon = Eye;
    summary = `Список участников (${ms.length})`;
  } else if (!ok) {
    Icon = AlertCircle;
    summary = `${action.tool}: ${String(result["error"] ?? "ошибка")}`;
    tone = "warn";
  } else {
    summary = action.tool;
  }

  const toneClass = tone === "ok" ? "text-secondary" : "text-warning-primary";
  const inner = (
    <span className="flex items-start gap-1.5">
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${tone === "ok" ? "text-tertiary" : "text-warning-primary"}`} strokeWidth={2} />
      <span>{summary}</span>
    </span>
  );

  return (
    <li className={toneClass}>
      {href ? (
        <a href={href} className="hover:underline" target="_blank" rel="noreferrer">
          {inner}
        </a>
      ) : (
        inner
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
          <pre key={i} className="overflow-x-auto rounded-md border border-subtle-1 bg-layer-3 p-2 text-body-xs">
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
            className="rounded bg-layer-3 px-1.5 py-0.5 text-caption-md font-mono text-tertiary"
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
      className="fixed inset-0 z-50 flex justify-end bg-overlay backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex h-full w-full max-w-2xl flex-col bg-layer-1 shadow-2xl">
        <header className="flex items-center justify-between border-b border-subtle-1 bg-layer-1 px-5 py-3">
          <div className="flex items-center gap-2.5">
            <div className="flex size-8 items-center justify-center rounded-md bg-accent-subtle text-accent-primary">
              <Sparkles className="size-4" strokeWidth={2} />
            </div>
            <div>
              <div className="text-body-sm font-semibold text-primary">ИИ-помощник</div>
              <div className="text-caption-md text-tertiary">DeepSeek · OpenAI embeddings</div>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-md text-tertiary transition-colors hover:bg-layer-2-hover hover:text-primary"
            aria-label="Закрыть"
          >
            <X className="size-4" strokeWidth={2} />
          </button>
        </header>
        <div className="flex-1 min-h-0 overflow-hidden">
          <AISearch workspaceId={workspaceId} workspaceSlug={workspaceSlug} />
        </div>
      </div>
    </div>
  );
}
