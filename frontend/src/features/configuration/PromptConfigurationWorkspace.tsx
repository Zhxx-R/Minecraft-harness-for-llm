import {
  AlertTriangle,
  Braces,
  CheckCircle2,
  Code2,
  Eye,
  EyeOff,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Settings2,
  Zap
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActionPromptConfiguration,
  PromptConfigurations,
  getPromptConfigurations,
  resetActionPromptConfiguration,
  resetSystemPromptConfiguration,
  updateActionPromptConfiguration,
  updatePromptHotReload,
  updateSystemPromptConfiguration
} from "../../api/client";

type ConfigurationTab = "system" | "actions";

interface PromptConfigurationWorkspaceProps {
  refreshToken?: number;
}

interface SystemPromptDraft {
  content: string;
  enabled: boolean;
  version: number;
}

interface ActionPromptDraft {
  actionType: string;
  promptVisible: boolean;
  purpose: string;
  argsText: string;
  returns: string;
  whenToUse: string;
  recommendationsText: string;
  version: number;
}

/** Hot-reload configuration center for the static system prompt and action guides. */
export function PromptConfigurationWorkspace(props: PromptConfigurationWorkspaceProps) {
  const [snapshot, setSnapshot] = useState<PromptConfigurations | null>(null);
  const [activeTab, setActiveTab] = useState<ConfigurationTab>("system");
  const [selectedActionType, setSelectedActionType] = useState<string | null>(null);
  const [systemDraft, setSystemDraft] = useState<SystemPromptDraft | null>(null);
  const [systemBaseline, setSystemBaseline] = useState<SystemPromptDraft | null>(null);
  const [actionDraft, setActionDraft] = useState<ActionPromptDraft | null>(null);
  const [actionBaseline, setActionBaseline] = useState<ActionPromptDraft | null>(null);
  const [actionQuery, setActionQuery] = useState("");
  const [actionCategory, setActionCategory] = useState("");
  const [busy, setBusy] = useState<
    "load" |
    "hot-reload" |
    "system-save" |
    "system-reset" |
    "action-save" |
    "action-reset" |
    null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const systemDirty = useMemo(
    () => Boolean(
      systemDraft &&
      systemBaseline &&
      JSON.stringify(systemDraft) !== JSON.stringify(systemBaseline)
    ),
    [systemBaseline, systemDraft]
  );
  const actionDirty = useMemo(
    () => Boolean(
      actionDraft &&
      actionBaseline &&
      JSON.stringify(actionDraft) !== JSON.stringify(actionBaseline)
    ),
    [actionBaseline, actionDraft]
  );

  const applySnapshot = useCallback((next: PromptConfigurations, preferredAction?: string | null) => {
    setSnapshot(next);
    const nextSystem = draftFromSystem(next);
    setSystemDraft(nextSystem);
    setSystemBaseline(nextSystem);
    const selected =
      next.actions.find((action) => action.action_type === preferredAction) ??
      next.actions.find((action) => action.runtime_supported) ??
      next.actions[0] ??
      null;
    setSelectedActionType(selected?.action_type ?? null);
    const nextAction = selected ? draftFromAction(selected) : null;
    setActionDraft(nextAction);
    setActionBaseline(nextAction);
  }, []);

  const load = useCallback(async (preferredAction?: string | null) => {
    setBusy("load");
    setError(null);
    try {
      const next = await getPromptConfigurations();
      applySnapshot(next, preferredAction ?? selectedActionType);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  }, [applySnapshot, selectedActionType]);

  useEffect(() => {
    void load();
    // Reload only for navigation-level refreshes. Form edits remain local otherwise.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.refreshToken]);

  const selectAction = (action: ActionPromptConfiguration) => {
    if (actionDirty && !window.confirm("Discard the unsaved action prompt changes?")) {
      return;
    }
    const nextDraft = draftFromAction(action);
    setSelectedActionType(action.action_type);
    setActionDraft(nextDraft);
    setActionBaseline(nextDraft);
    setError(null);
    setNotice(null);
  };

  const setHotReload = async (enabled: boolean) => {
    if (!snapshot) {
      return;
    }
    setBusy("hot-reload");
    setError(null);
    setNotice(null);
    try {
      await updatePromptHotReload({
        enabled,
        expected_version: snapshot.hot_reload.version
      });
      // Refresh only the effective snapshot metadata so unsaved form edits remain intact.
      const next = await getPromptConfigurations();
      setSnapshot(next);
      setNotice(
        enabled
          ? "Prompt hot reload enabled for new and unpinned runs. Runs already pinned remain fixed until they finish."
          : "Prompt hot reload disabled. Running agents pin the configuration at their next decision; later changes affect new runs."
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const saveSystem = async () => {
    if (!systemDraft) {
      return;
    }
    if (systemDraft.enabled && !systemDraft.content.trim()) {
      setError("An enabled system prompt cannot be empty.");
      return;
    }
    setBusy("system-save");
    setError(null);
    setNotice(null);
    try {
      await updateSystemPromptConfiguration({
        content: systemDraft.content,
        enabled: systemDraft.enabled,
        expected_version: systemDraft.version
      });
      await load(selectedActionType);
      setNotice(promptChangeNotice("System prompt saved.", snapshot?.hot_reload.enabled));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const resetSystem = async () => {
    if (!systemDraft || !snapshot?.system_prompt.persisted) {
      return;
    }
    if (!window.confirm("Reset the system prompt to the code default?")) {
      return;
    }
    setBusy("system-reset");
    setError(null);
    setNotice(null);
    try {
      await resetSystemPromptConfiguration(systemDraft.version);
      await load(selectedActionType);
      setNotice(
        promptChangeNotice(
          "System prompt override reset to the code default.",
          snapshot?.hot_reload.enabled
        )
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const saveAction = async () => {
    if (!actionDraft || !selectedAction) {
      return;
    }
    if (!selectedAction.runtime_supported) {
      setError("Only actions implemented by the harness runtime can be configured.");
      return;
    }
    const args = parseJsonObject(actionDraft.argsText, "Argument guide");
    if (args.error) {
      setError(args.error);
      return;
    }
    if (!actionDraft.purpose.trim() || !actionDraft.returns.trim() || !actionDraft.whenToUse.trim()) {
      setError("Purpose, returns, and when-to-use guidance are required.");
      return;
    }
    setBusy("action-save");
    setError(null);
    setNotice(null);
    try {
      await updateActionPromptConfiguration(actionDraft.actionType, {
        prompt_visible: selectedAction.hard_hidden ? false : actionDraft.promptVisible,
        purpose: actionDraft.purpose.trim(),
        args: args.value,
        returns: actionDraft.returns.trim(),
        when_to_use: actionDraft.whenToUse.trim(),
        recommended_next_actions: parseLines(actionDraft.recommendationsText),
        expected_version: actionDraft.version
      });
      await load(actionDraft.actionType);
      setNotice(promptChangeNotice("Action prompt saved.", snapshot?.hot_reload.enabled));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const resetAction = async () => {
    if (!actionDraft || !selectedAction?.persisted) {
      return;
    }
    if (!window.confirm(`Reset ${actionDraft.actionType} to its code-defined prompt guide?`)) {
      return;
    }
    setBusy("action-reset");
    setError(null);
    setNotice(null);
    try {
      await resetActionPromptConfiguration(actionDraft.actionType, actionDraft.version);
      await load(actionDraft.actionType);
      setNotice(
        promptChangeNotice(
          "Action prompt override reset to the code default.",
          snapshot?.hot_reload.enabled
        )
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const updateSystemDraft = <K extends keyof SystemPromptDraft>(
    key: K,
    value: SystemPromptDraft[K]
  ) => {
    setSystemDraft((current) => current ? { ...current, [key]: value } : current);
    setNotice(null);
  };

  const updateActionDraft = <K extends keyof ActionPromptDraft>(
    key: K,
    value: ActionPromptDraft[K]
  ) => {
    setActionDraft((current) => current ? { ...current, [key]: value } : current);
    setNotice(null);
  };

  const selectedAction =
    snapshot?.actions.find((action) => action.action_type === selectedActionType) ?? null;
  const categories = Array.from(new Set((snapshot?.actions ?? []).map((action) => action.category))).sort();
  const visibleActions = (snapshot?.actions ?? []).filter((action) => {
    const query = actionQuery.trim().toLowerCase();
    const matchesQuery =
      !query ||
      action.action_type.toLowerCase().includes(query) ||
      action.display_name.toLowerCase().includes(query) ||
      action.purpose.toLowerCase().includes(query);
    return matchesQuery && (!actionCategory || action.category === actionCategory);
  });

  return (
    <section className="configuration-workspace">
      <div className="configuration-summary">
        <div className="configuration-summary-details">
          <span className={`configuration-live ${snapshot?.hot_reload.enabled ? "active" : ""}`}>
            <Zap size={14} aria-hidden="true" />
            {snapshot?.hot_reload.enabled ? "Live updates on" : "Run snapshots pinned"}
          </span>
          <strong>Snapshot {snapshot?.snapshot_revision ?? "loading"}</strong>
          <small>
            {snapshot?.hot_reload.enabled
              ? "New and unpinned runs read saved prompt changes at their next decision; already pinned runs remain fixed."
              : "Running agents pin the current configuration at their next decision; new runs pin it at their first. Later changes affect new runs only."}
            {" "}Existing runtime actions are not created or replaced by this page.
          </small>
        </div>
        <div className="configuration-summary-controls">
          <label className="configuration-hot-reload">
            <input
              type="checkbox"
              role="switch"
              aria-label="Apply saved prompt changes to running agents"
              checked={snapshot?.hot_reload.enabled ?? false}
              disabled={!snapshot || busy !== null}
              onChange={(event) => void setHotReload(event.target.checked)}
            />
            <span className="configuration-switch" aria-hidden="true">
              <span />
            </span>
            <span>
              <strong>Prompt hot reload</strong>
              <small>
                {snapshot?.hot_reload.enabled
                  ? "New and unpinned runs read the latest saved configuration."
                  : "Running agents pin the current snapshot at their next decision."}
              </small>
            </span>
            {busy === "hot-reload" ? (
              <LoaderCircle className="spin" size={15} aria-label="Saving hot reload setting" />
            ) : null}
          </label>
          <button
            className="secondary-command"
            type="button"
            disabled={busy !== null}
            onClick={() => void load(selectedActionType)}
          >
            <RefreshCw size={15} className={busy === "load" ? "spin" : ""} />
            Reload snapshot
          </button>
        </div>
      </div>

      <div className="configuration-tabs" role="tablist" aria-label="Prompt configuration sections">
        <button
          className={`segment-button ${activeTab === "system" ? "active" : ""}`}
          role="tab"
          aria-selected={activeTab === "system"}
          type="button"
          onClick={() => setActiveTab("system")}
        >
          <Code2 size={15} />
          System Prompt
          {systemDirty ? <span className="dirty-dot" title="Unsaved changes" /> : null}
        </button>
        <button
          className={`segment-button ${activeTab === "actions" ? "active" : ""}`}
          role="tab"
          aria-selected={activeTab === "actions"}
          type="button"
          onClick={() => setActiveTab("actions")}
        >
          <Braces size={15} />
          Action Prompt Registry
          {actionDirty ? <span className="dirty-dot" title="Unsaved changes" /> : null}
        </button>
      </div>

      {error ? <InlineMessage kind="error" message={error} /> : null}
      {notice ? <InlineMessage kind="success" message={notice} /> : null}

      {activeTab === "system" ? (
        <section className="configuration-panel">
          <header className="configuration-panel-header">
            <div>
              <span className="eyebrow">Static prefix</span>
              <h2>System Prompt</h2>
              <p>
                Source: <strong>{snapshot?.system_prompt.effective_source ?? "unknown"}</strong> ·
                version {snapshot?.system_prompt.version ?? 0} ·
                updated {formatDate(snapshot?.system_prompt.updated_at ?? null)}
              </p>
            </div>
            <span className={`status-badge ${snapshot?.system_prompt.persisted ? "warn" : "neutral"}`}>
              {snapshot?.system_prompt.persisted ? "override" : "code default"}
            </span>
          </header>
          {systemDraft ? (
            <form
              className="configuration-form"
              onSubmit={(event) => {
                event.preventDefault();
                void saveSystem();
              }}
            >
              <label className="management-check">
                <input
                  type="checkbox"
                  checked={systemDraft.enabled}
                  onChange={(event) => updateSystemDraft("enabled", event.target.checked)}
                />
                <span>
                  <strong>Enable this system prompt</strong>
                  <small>The stable harness and task contracts remain independently enforced.</small>
                </span>
              </label>
              <label className="management-field">
                <span>
                  <strong>Prompt content</strong>
                  <small>Changing this static prefix invalidates the model prefix cache once per new revision.</small>
                </span>
                <textarea
                  className="system-prompt-editor"
                  spellCheck={false}
                  value={systemDraft.content}
                  onChange={(event) => updateSystemDraft("content", event.target.value)}
                />
              </label>
              <footer className="configuration-actions">
                <span>{systemDirty ? "Unsaved changes" : "No unsaved changes"}</span>
                <div>
                  <button
                    className="secondary-command"
                    type="button"
                    disabled={busy !== null || !snapshot?.system_prompt.persisted}
                    onClick={() => void resetSystem()}
                  >
                    {busy === "system-reset" ? <LoaderCircle className="spin" size={15} /> : <RotateCcw size={15} />}
                    Reset override
                  </button>
                  <button
                    className="primary-command"
                    type="submit"
                    disabled={busy !== null || !systemDirty}
                  >
                    {busy === "system-save" ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
                    Save system prompt
                  </button>
                </div>
              </footer>
            </form>
          ) : (
            <LoadingState label="Loading system prompt" />
          )}
        </section>
      ) : (
        <section className="action-registry">
          <aside className="action-registry-index">
            <header>
              <Settings2 size={17} />
              <strong>Implemented Actions</strong>
              <span>{snapshot?.actions.filter((action) => action.runtime_supported).length ?? 0}</span>
            </header>
            <div className="action-registry-filters">
              <label>
                <Search size={15} />
                <input
                  aria-label="Search actions"
                  placeholder="Search actions"
                  value={actionQuery}
                  onChange={(event) => setActionQuery(event.target.value)}
                />
              </label>
              <select
                aria-label="Action category"
                value={actionCategory}
                onChange={(event) => setActionCategory(event.target.value)}
              >
                <option value="">All categories</option>
                {categories.map((category) => (
                  <option key={category} value={category}>{category}</option>
                ))}
              </select>
            </div>
            <div className="action-registry-list">
              {visibleActions.map((action) => (
                <button
                  className={`action-registry-row ${action.action_type === selectedActionType ? "selected" : ""}`}
                  key={action.action_type}
                  type="button"
                  onClick={() => selectAction(action)}
                >
                  <span className={`status-dot ${action.runtime_supported ? "ok" : "bad"}`} />
                  <span>
                    <strong>{action.display_name || action.action_type}</strong>
                    <small>{action.action_type} · {action.category}</small>
                  </span>
                  {action.prompt_visible && !action.hard_hidden ? <Eye size={14} /> : <EyeOff size={14} />}
                </button>
              ))}
              {!visibleActions.length ? <LoadingState label="No actions match these filters" /> : null}
            </div>
          </aside>

          <section className="action-registry-editor">
            {selectedAction && actionDraft ? (
              <>
                <header className="configuration-panel-header">
                  <div>
                    <span className="eyebrow">{selectedAction.category}</span>
                    <h2>{selectedAction.display_name || selectedAction.action_type}</h2>
                    <p>
                      {selectedAction.action_type} · source {selectedAction.effective_source} ·
                      version {selectedAction.version}
                    </p>
                  </div>
                  <div className="configuration-badges">
                    <span className={`status-badge ${selectedAction.runtime_supported ? "ok" : "bad"}`}>
                      {selectedAction.runtime_supported ? "runtime supported" : "not implemented"}
                    </span>
                    {selectedAction.hard_hidden ? <span className="status-badge warn">hard hidden</span> : null}
                    {selectedAction.persisted ? <span className="status-badge neutral">override</span> : null}
                  </div>
                </header>

                <div className="management-scope-note">
                  <AlertTriangle size={17} aria-hidden="true" />
                  <div>
                    <strong>Prompt metadata, not an executor plugin</strong>
                    <p>
                      This registry configures descriptions for actions already implemented by the
                      harness. It cannot register a new runtime handler.
                    </p>
                  </div>
                </div>

                <form
                  className="configuration-form action-configuration-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveAction();
                  }}
                >
                  <label className="management-check">
                    <input
                      type="checkbox"
                      checked={actionDraft.promptVisible && !selectedAction.hard_hidden}
                      disabled={selectedAction.hard_hidden || !selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("promptVisible", event.target.checked)}
                    />
                    <span>
                      <strong>Visible in the model action contract</strong>
                      <small>
                        {selectedAction.hard_hidden
                          ? "This compatibility action is permanently hidden from normal prompts."
                          : "Disable to keep runtime support without advertising the action to the model."}
                      </small>
                    </span>
                  </label>

                  <PromptField label="Purpose">
                    <textarea
                      value={actionDraft.purpose}
                      disabled={!selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("purpose", event.target.value)}
                    />
                  </PromptField>
                  <PromptField label="Argument guide" hint="JSON object shown in the stable action contract.">
                    <textarea
                      className="json-editor"
                      spellCheck={false}
                      value={actionDraft.argsText}
                      disabled={!selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("argsText", event.target.value)}
                    />
                  </PromptField>
                  <PromptField label="Returns">
                    <textarea
                      value={actionDraft.returns}
                      disabled={!selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("returns", event.target.value)}
                    />
                  </PromptField>
                  <PromptField label="When to use">
                    <textarea
                      value={actionDraft.whenToUse}
                      disabled={!selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("whenToUse", event.target.value)}
                    />
                  </PromptField>
                  <PromptField
                    label="Recommended next actions"
                    hint="One natural-language recommendation per line, for example use_item: Use when …"
                  >
                    <textarea
                      value={actionDraft.recommendationsText}
                      disabled={!selectedAction.runtime_supported}
                      onChange={(event) => updateActionDraft("recommendationsText", event.target.value)}
                      placeholder={"use_item: Use when the held item should be used on the target.\nmove_to_and_engage_combat: Use when the target should be attacked."}
                    />
                  </PromptField>

                  <footer className="configuration-actions">
                    <span>
                      {actionDirty
                        ? "Unsaved changes"
                        : `Updated ${formatDate(selectedAction.updated_at)}`}
                    </span>
                    <div>
                      <button
                        className="secondary-command"
                        type="button"
                        disabled={busy !== null || !selectedAction.persisted}
                        onClick={() => void resetAction()}
                      >
                        {busy === "action-reset" ? <LoaderCircle className="spin" size={15} /> : <RotateCcw size={15} />}
                        Reset override
                      </button>
                      <button
                        className="primary-command"
                        type="submit"
                        disabled={busy !== null || !actionDirty || !selectedAction.runtime_supported}
                      >
                        {busy === "action-save" ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
                        Save action prompt
                      </button>
                    </div>
                  </footer>
                </form>
              </>
            ) : (
              <LoadingState label="Select an implemented action" />
            )}
          </section>
        </section>
      )}
    </section>
  );
}

function PromptField(props: {
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
      {props.kind === "error" ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}
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

function draftFromSystem(snapshot: PromptConfigurations): SystemPromptDraft {
  return {
    content: snapshot.system_prompt.content,
    enabled: snapshot.system_prompt.enabled,
    version: snapshot.system_prompt.version
  };
}

function draftFromAction(action: ActionPromptConfiguration): ActionPromptDraft {
  return {
    actionType: action.action_type,
    promptVisible: action.prompt_visible,
    purpose: action.purpose,
    argsText: JSON.stringify(action.args ?? {}, null, 2),
    returns: action.returns,
    whenToUse: action.when_to_use,
    recommendationsText: action.recommended_next_actions.join("\n"),
    version: action.version
  };
}

function parseJsonObject(
  text: string,
  label: string
): { error: string | null; value: Record<string, unknown> } {
  try {
    const parsed = JSON.parse(text || "{}") as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { error: `${label} must be a JSON object.`, value: {} };
    }
    return { error: null, value: parsed as Record<string, unknown> };
  } catch (caught) {
    return { error: `${label} JSON is invalid: ${errorMessage(caught)}`, value: {} };
  }
}

function parseLines(text: string): string[] {
  return Array.from(
    new Set(
      text
        .split("\n")
        .map((line) => line.trim())
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

function promptChangeNotice(change: string, hotReloadEnabled: boolean | undefined): string {
  return hotReloadEnabled
    ? `${change} Unpinned running agents will use it at their next decision.`
    : `${change} Pinned runs keep their snapshots; new runs will use the change.`;
}
