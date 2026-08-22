import {
  Archive,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  FilePlus2,
  LoaderCircle,
  RefreshCw,
  Save,
  Search,
  Tag
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  KnowledgeChunk,
  KnowledgeChunkCreate,
  KnowledgeChunkPage,
  archiveKnowledgeChunk,
  createKnowledgeChunk,
  getKnowledgeChunks,
  updateKnowledgeChunk
} from "../../api/client";

const PAGE_SIZE = 30;

interface KnowledgeWorkspaceProps {
  refreshToken?: number;
}

interface KnowledgeFilters {
  q: string;
  source: string;
  kind: string;
  enabled: "all" | "true" | "false";
}

interface KnowledgeDraft {
  id: string;
  source: string;
  title: string;
  content: string;
  tagsText: string;
  metadataText: string;
  enabled: boolean;
  version: number;
}

/** SQL-backed knowledge editor for the retrieve_docs corpus. */
export function KnowledgeWorkspace(props: KnowledgeWorkspaceProps) {
  const [page, setPage] = useState<KnowledgeChunkPage | null>(null);
  const [filters, setFilters] = useState<KnowledgeFilters>({
    q: "",
    source: "",
    kind: "",
    enabled: "all"
  });
  const [searchDraft, setSearchDraft] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<KnowledgeDraft | null>(null);
  const [baseline, setBaseline] = useState<KnowledgeDraft | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<"list" | "save" | "archive" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const dirty = useMemo(
    () => Boolean(draft && baseline && JSON.stringify(draft) !== JSON.stringify(baseline)),
    [baseline, draft]
  );

  const loadPage = useCallback(async () => {
    setBusy((current) => current ?? "list");
    setError(null);
    try {
      const nextPage = await getKnowledgeChunks({
        q: filters.q,
        source: filters.source || undefined,
        kind: filters.kind || undefined,
        enabled: filters.enabled === "all" ? undefined : filters.enabled === "true",
        offset,
        limit: PAGE_SIZE
      });
      setPage(nextPage);
      const selected =
        nextPage.items.find((item) => item.id === selectedId) ??
        nextPage.items[0] ??
        null;
      if (!creating || !draft) {
        setSelectedId(selected?.id ?? null);
        const nextDraft = selected ? draftFromChunk(selected) : null;
        setDraft(nextDraft);
        setBaseline(nextDraft);
        setCreating(false);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy((current) => (current === "list" ? null : current));
    }
  }, [creating, draft, filters, offset, selectedId]);

  useEffect(() => {
    void loadPage();
    // Explicit refresh tokens and filters are the only automatic reload triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, offset, props.refreshToken]);

  const confirmDiscard = () =>
    !dirty || window.confirm("Discard the unsaved knowledge changes?");

  const chooseChunk = (chunk: KnowledgeChunk) => {
    if (!confirmDiscard()) {
      return;
    }
    const nextDraft = draftFromChunk(chunk);
    setSelectedId(chunk.id);
    setDraft(nextDraft);
    setBaseline(nextDraft);
    setCreating(false);
    setError(null);
    setNotice(null);
  };

  const beginCreate = () => {
    if (!confirmDiscard()) {
      return;
    }
    const nextDraft: KnowledgeDraft = {
      id: "",
      source: "dashboard",
      title: "",
      content: "",
      tagsText: "",
      metadataText: "{\n  \"kind\": \"document\"\n}",
      enabled: true,
      version: 0
    };
    setSelectedId(null);
    setDraft(nextDraft);
    setBaseline(nextDraft);
    setCreating(true);
    setError(null);
    setNotice(null);
  };

  const save = async () => {
    if (!draft) {
      return;
    }
    const validation = validateDraft(draft);
    if (validation.error) {
      setError(validation.error);
      return;
    }
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      let saved: KnowledgeChunk;
      const fields: KnowledgeChunkCreate = {
        id: draft.id.trim(),
        source: draft.source.trim(),
        title: draft.title.trim(),
        content: draft.content.trim(),
        tags: parseTags(draft.tagsText),
        metadata: validation.metadata,
        enabled: draft.enabled
      };
      if (creating) {
        saved = await createKnowledgeChunk(fields);
      } else {
        saved = await updateKnowledgeChunk(draft.id, {
          source: fields.source,
          title: fields.title,
          content: fields.content,
          tags: fields.tags,
          metadata: fields.metadata,
          enabled: fields.enabled,
          expected_version: draft.version
        });
      }
      const nextDraft = draftFromChunk(saved);
      setSelectedId(saved.id);
      setDraft(nextDraft);
      setBaseline(nextDraft);
      setCreating(false);
      setNotice(creating ? "Knowledge chunk created." : "Knowledge chunk saved.");
      await loadPage();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const archive = async () => {
    if (!draft || creating || !draft.enabled) {
      return;
    }
    if (!window.confirm(`Archive “${draft.title || draft.id}”? It will stop appearing in retrieve_docs.`)) {
      return;
    }
    setBusy("archive");
    setError(null);
    setNotice(null);
    try {
      const saved = await archiveKnowledgeChunk(draft.id, draft.version);
      const nextDraft = draftFromChunk(saved);
      setDraft(nextDraft);
      setBaseline(nextDraft);
      setNotice("Knowledge chunk archived.");
      await loadPage();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const updateDraft = <K extends keyof KnowledgeDraft>(key: K, value: KnowledgeDraft[K]) => {
    setDraft((current) => current ? { ...current, [key]: value } : current);
    setNotice(null);
  };

  const submitSearch = () => {
    if (!confirmDiscard()) {
      return;
    }
    setOffset(0);
    setFilters((current) => ({ ...current, q: searchDraft.trim() }));
  };

  const sourceOptions = Object.keys(page?.sources ?? {}).sort();
  const kindOptions = Object.keys(page?.kinds ?? {}).sort();
  const selectedChunk = page?.items.find((item) => item.id === selectedId) ?? null;
  const rangeStart = page?.total ? page.offset + 1 : 0;
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0;

  return (
    <section className="management-workspace knowledge-workspace">
      <aside className="management-index" aria-label="Knowledge chunks">
        <header className="management-index-header">
          <div>
            <Database size={17} aria-hidden="true" />
            <strong>Knowledge Chunks</strong>
          </div>
          <button className="icon-button" type="button" onClick={beginCreate} title="New knowledge chunk">
            <FilePlus2 size={16} aria-hidden="true" />
          </button>
        </header>

        <form
          className="management-search"
          onSubmit={(event) => {
            event.preventDefault();
            submitSearch();
          }}
        >
          <Search size={15} aria-hidden="true" />
          <input
            aria-label="Search knowledge"
            placeholder="Search title, content, or tags"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
          />
          <button type="submit">Search</button>
        </form>

        <div className="management-filters">
          <select
            aria-label="Knowledge source"
            value={filters.source}
            onChange={(event) => {
              if (!confirmDiscard()) return;
              setOffset(0);
              setFilters((current) => ({ ...current, source: event.target.value }));
            }}
          >
            <option value="">All sources</option>
            {sourceOptions.map((source) => (
              <option key={source} value={source}>{source}</option>
            ))}
          </select>
          <select
            aria-label="Knowledge kind"
            value={filters.kind}
            onChange={(event) => {
              if (!confirmDiscard()) return;
              setOffset(0);
              setFilters((current) => ({ ...current, kind: event.target.value }));
            }}
          >
            <option value="">All kinds</option>
            {kindOptions.map((kind) => (
              <option key={kind} value={kind}>{kind}</option>
            ))}
          </select>
          <select
            aria-label="Knowledge status"
            value={filters.enabled}
            onChange={(event) => {
              if (!confirmDiscard()) return;
              setOffset(0);
              setFilters((current) => ({
                ...current,
                enabled: event.target.value as KnowledgeFilters["enabled"]
              }));
            }}
          >
            <option value="all">All states</option>
            <option value="true">Active</option>
            <option value="false">Archived</option>
          </select>
        </div>

        <div className="management-list">
          {busy === "list" && !page ? <LoadingState label="Loading knowledge" /> : null}
          {page?.items.map((chunk) => (
            <button
              className={`management-row ${chunk.id === selectedId ? "selected" : ""}`}
              key={chunk.id}
              type="button"
              onClick={() => chooseChunk(chunk)}
            >
              <span className={`status-dot ${chunk.enabled ? "ok" : "neutral"}`} aria-hidden="true" />
              <span>
                <strong>{chunk.title}</strong>
                <small>{chunk.source} · v{chunk.version}</small>
                <span className="management-row-tags">
                  {chunk.tags.slice(0, 3).map((tag) => <em key={tag}>{tag}</em>)}
                </span>
              </span>
            </button>
          ))}
          {page && !page.items.length ? <EmptyState label="No knowledge chunks match these filters" /> : null}
        </div>

        <footer className="management-pagination">
          <span>{rangeStart}-{rangeEnd} of {page?.total ?? 0}</span>
          <div>
            <button
              type="button"
              disabled={!page || page.offset <= 0}
              onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
              title="Previous page"
            >
              <ChevronLeft size={15} />
            </button>
            <button
              type="button"
              disabled={!page || page.offset + page.items.length >= page.total}
              onClick={() => setOffset((current) => current + PAGE_SIZE)}
              title="Next page"
            >
              <ChevronRight size={15} />
            </button>
          </div>
        </footer>
      </aside>

      <section className="management-editor">
        <header className="management-editor-header">
          <div>
            <span className="eyebrow">{creating ? "New chunk" : "Knowledge chunk"}</span>
            <h2>{draft?.title || draft?.id || "Select a knowledge chunk"}</h2>
            {selectedChunk ? (
              <p>
                {selectedChunk.has_embedding ? "Embedding available" : "Lexical retrieval"} ·
                updated {formatDate(selectedChunk.updated_at)}
              </p>
            ) : null}
          </div>
          <div className="management-header-actions">
            <button
              className="secondary-command"
              type="button"
              disabled={busy !== null}
              onClick={() => void loadPage()}
            >
              <RefreshCw size={15} className={busy === "list" ? "spin" : ""} />
              Reload
            </button>
            <button
              className="primary-command"
              type="button"
              disabled={!draft || busy !== null || (!creating && !dirty)}
              onClick={() => void save()}
            >
              {busy === "save" ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
              {creating ? "Create" : "Save"}
            </button>
          </div>
        </header>

        <div className="management-scope-note">
          <Database size={17} aria-hidden="true" />
          <div>
            <strong>Retrieval scope</strong>
            <p>
              Changes become available to <code>retrieve_docs</code>. Deterministic term resolution
              and recipe lookup still retain their static fallback.
            </p>
          </div>
        </div>

        {error ? <InlineMessage kind="error" message={error} /> : null}
        {notice ? <InlineMessage kind="success" message={notice} /> : null}

        {draft ? (
          <form
            className="management-form"
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <div className="management-form-grid">
              <FormField label="Chunk ID" hint={creating ? "Stable identifier; cannot be changed later." : "Immutable"}>
                <input
                  value={draft.id}
                  disabled={!creating}
                  onChange={(event) => updateDraft("id", event.target.value)}
                  placeholder="guide:sheep-interaction"
                />
              </FormField>
              <FormField label="Source">
                <input
                  value={draft.source}
                  onChange={(event) => updateDraft("source", event.target.value)}
                  placeholder="dashboard"
                />
              </FormField>
            </div>
            <FormField label="Title">
              <input
                value={draft.title}
                onChange={(event) => updateDraft("title", event.target.value)}
                placeholder="Human-readable knowledge title"
              />
            </FormField>
            <FormField label="Content" hint="Text returned to the agent under the retrieval character budget.">
              <textarea
                className="large-editor"
                value={draft.content}
                onChange={(event) => updateDraft("content", event.target.value)}
                placeholder="Write the bounded Minecraft or Mineflayer guidance here."
              />
            </FormField>
            <FormField label="Tags" hint="Comma or newline separated.">
              <div className="input-with-icon">
                <Tag size={15} aria-hidden="true" />
                <input
                  value={draft.tagsText}
                  onChange={(event) => updateDraft("tagsText", event.target.value)}
                  placeholder="sheep, interaction, shears"
                />
              </div>
            </FormField>
            <FormField label="Metadata JSON" hint="Use kind to make the filter and audit view more useful.">
              <textarea
                className="json-editor"
                spellCheck={false}
                value={draft.metadataText}
                onChange={(event) => updateDraft("metadataText", event.target.value)}
              />
            </FormField>
            <label className="management-check">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) => updateDraft("enabled", event.target.checked)}
              />
              <span>
                <strong>Enabled for retrieval</strong>
                <small>Archived chunks remain auditable but are excluded from agent retrieval.</small>
              </span>
            </label>
            <footer className="management-form-footer">
              <span>{dirty ? "Unsaved changes" : creating ? "Complete the required fields" : `Version ${draft.version}`}</span>
              {!creating && draft.enabled ? (
                <button
                  className="danger-command"
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void archive()}
                >
                  <Archive size={15} />
                  Archive
                </button>
              ) : null}
            </footer>
          </form>
        ) : (
          <EmptyState label="Select a knowledge chunk or create a new one" />
        )}
      </section>
    </section>
  );
}

function FormField(props: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="management-field">
      <span>
        <strong>{props.label}</strong>
        {props.hint ? <small>{props.hint}</small> : null}
      </span>
      {props.children}
    </label>
  );
}

function InlineMessage(props: { kind: "error" | "success"; message: string }) {
  return (
    <div className={`management-message ${props.kind}`} role={props.kind === "error" ? "alert" : "status"}>
      <CheckCircle2 size={16} aria-hidden="true" />
      <span>{props.message}</span>
    </div>
  );
}

function LoadingState(props: { label: string }) {
  return (
    <div className="management-empty">
      <LoaderCircle className="spin" size={17} />
      <span>{props.label}</span>
    </div>
  );
}

function EmptyState(props: { label: string }) {
  return <div className="management-empty">{props.label}</div>;
}

function draftFromChunk(chunk: KnowledgeChunk): KnowledgeDraft {
  return {
    id: chunk.id,
    source: chunk.source,
    title: chunk.title,
    content: chunk.content,
    tagsText: chunk.tags.join(", "),
    metadataText: JSON.stringify(chunk.metadata ?? {}, null, 2),
    enabled: chunk.enabled,
    version: chunk.version
  };
}

function validateDraft(draft: KnowledgeDraft): {
  error: string | null;
  metadata: Record<string, unknown>;
} {
  if (!draft.id.trim()) {
    return { error: "Chunk ID is required.", metadata: {} };
  }
  if (!draft.source.trim()) {
    return { error: "Source is required.", metadata: {} };
  }
  if (!draft.title.trim()) {
    return { error: "Title is required.", metadata: {} };
  }
  if (!draft.content.trim()) {
    return { error: "Content is required.", metadata: {} };
  }
  try {
    const parsed = JSON.parse(draft.metadataText || "{}") as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { error: "Metadata must be a JSON object.", metadata: {} };
    }
    return { error: null, metadata: parsed as Record<string, unknown> };
  } catch (caught) {
    return { error: `Metadata JSON is invalid: ${errorMessage(caught)}`, metadata: {} };
  }
}

function parseTags(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[,\n]/)
        .map((tag) => tag.trim())
        .filter(Boolean)
    )
  );
}

function formatDate(value: string | null): string {
  if (!value) {
    return "not recorded";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
