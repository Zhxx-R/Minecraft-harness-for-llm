import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Code2,
  ListTree,
  Plus,
  Save,
  Trash2,
  X
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

type EditorMode = "guided" | "json";

interface SkillSpecEditorProps {
  busy: boolean;
  initialSpec: Record<string, unknown>;
  skillId: number;
  onCancel: () => void;
  onSave: (spec: Record<string, unknown>) => void;
}

const SKILL_SPEC_KEYS = new Set([
  "name",
  "version",
  "description",
  "triggers",
  "preconditions",
  "strategy_summary",
  "parameterized_plan",
  "recovery_policy",
  "source_evidence",
  "verifier_stats",
  "action_plan",
  "validation",
  "source_run_id",
  "source_step_range",
  "task_scope",
  "dependencies",
  "metrics",
  "status"
]);

const PLAN_FIELDS = [
  ["type", "Action type", "For example: scan_entities, move_to, use_item"],
  ["target", "Target", "A semantic target such as selected_entity or coal_ore"],
  ["selection", "Selection rule", "How the current-world target should be selected"],
  ["position", "Position reference", "Use a semantic reference instead of source coordinates"],
  ["recovery", "Recovery", "What to do when this step cannot complete"],
  ["postcondition", "Expected result", "The observation that should be true after this step"]
] as const;

/** Human-friendly editor for the common semantic fields in a canonical SkillSpec. */
export function SkillSpecEditor(props: SkillSpecEditorProps) {
  const [draft, setDraft] = useState<Record<string, unknown>>(() => cloneSpec(props.initialSpec));
  const [mode, setMode] = useState<EditorMode>("guided");
  const [jsonText, setJsonText] = useState(() => formatJson(props.initialSpec));
  const [localError, setLocalError] = useState<string | null>(null);
  const [actionPlanError, setActionPlanError] = useState<string | null>(null);

  const initialJson = useMemo(() => formatJson(props.initialSpec), [props.initialSpec]);
  const extensionKeys = Object.keys(draft).filter((key) => !SKILL_SPEC_KEYS.has(key));
  const plan = recordList(draft.parameterized_plan);
  const dirty =
    mode === "json"
      ? jsonText !== initialJson
      : formatJson(draft) !== initialJson;

  useEffect(() => {
    setDraft(cloneSpec(props.initialSpec));
    setJsonText(formatJson(props.initialSpec));
    setMode("guided");
    setLocalError(null);
    setActionPlanError(null);
  }, [props.initialSpec]);

  const setField = (key: string, value: unknown) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const switchMode = (nextMode: EditorMode) => {
    if (nextMode === mode) {
      return;
    }
    setLocalError(null);
    if (nextMode === "json") {
      if (actionPlanError) {
        setLocalError(`${actionPlanError} Fix the action arguments before opening Advanced JSON.`);
        return;
      }
      setJsonText(formatJson(draft));
      setMode(nextMode);
      return;
    }
    const parsed = parseSpecJson(jsonText);
    if ("error" in parsed) {
      setLocalError(parsed.error);
      return;
    }
    setDraft(parsed.spec);
    setActionPlanError(null);
    setMode(nextMode);
  };

  const save = () => {
    let nextSpec = draft;
    if (mode === "json") {
      const parsed = parseSpecJson(jsonText);
      if ("error" in parsed) {
        setLocalError(parsed.error);
        return;
      }
      nextSpec = parsed.spec;
      setDraft(parsed.spec);
    }

    const validationError = validateGuidedFields(nextSpec);
    if (validationError) {
      setLocalError(validationError);
      return;
    }
    if (mode === "guided" && actionPlanError) {
      setLocalError(actionPlanError);
      return;
    }
    setLocalError(null);
    props.onSave(nextSpec);
  };

  const cancel = () => {
    if (!dirty || window.confirm("Discard the unsaved Skill changes?")) {
      props.onCancel();
    }
  };

  return (
    <section
      className="skill-spec-editor"
      aria-label={`Edit ${stringValue(draft.name) || "skill"}`}
      aria-busy={props.busy}
    >
      <header className="skill-editor-header">
        <div>
          <strong>Edit Skill</strong>
          <small>
            Use the guided form for normal changes. Advanced JSON remains available for evidence and extension fields.
          </small>
        </div>
        <div className="skill-editor-mode" role="tablist" aria-label="Skill editor mode">
          <button
            className={`segment-button ${mode === "guided" ? "active" : ""}`}
            type="button"
            role="tab"
            id={`skill-guided-tab-${props.skillId}`}
            aria-controls={`skill-guided-panel-${props.skillId}`}
            aria-selected={mode === "guided"}
            onClick={() => switchMode("guided")}
          >
            <ListTree size={15} aria-hidden="true" />
            Guided
          </button>
          <button
            className={`segment-button ${mode === "json" ? "active" : ""}`}
            type="button"
            role="tab"
            id={`skill-json-tab-${props.skillId}`}
            aria-controls={`skill-json-panel-${props.skillId}`}
            aria-selected={mode === "json"}
            onClick={() => switchMode("json")}
          >
            <Code2 size={15} aria-hidden="true" />
            Advanced JSON
          </button>
        </div>
      </header>

      {mode === "guided" ? (
        <div
          className="skill-guided-editor"
          id={`skill-guided-panel-${props.skillId}`}
          role="tabpanel"
          aria-labelledby={`skill-guided-tab-${props.skillId}`}
        >
          <EditorSection
            title="Identity & purpose"
            description="How this skill is named, understood, and presented during review."
          >
            <div className="skill-form-grid">
              <EditorField label="Name" hint="Stable skill identifier">
                <input
                  value={stringValue(draft.name)}
                  disabled={props.busy}
                  onChange={(event) => setField("name", event.target.value)}
                />
              </EditorField>
              <EditorField label="Version" hint="Semantic version">
                <input
                  value={stringValue(draft.version)}
                  disabled={props.busy}
                  onChange={(event) => setField("version", event.target.value)}
                />
              </EditorField>
              <EditorField label="Lifecycle" hint="Use Promote or Deprecate to change">
                <input value={stringValue(draft.status) || "draft"} disabled readOnly />
              </EditorField>
              <EditorField label="Source run" hint="Immutable audit provenance">
                <input value={stringValue(draft.source_run_id) || "Not recorded"} disabled readOnly />
              </EditorField>
            </div>
            <EditorField label="Description" hint="Short reusable capability summary">
              <textarea
                value={stringValue(draft.description)}
                disabled={props.busy}
                onChange={(event) => setField("description", event.target.value)}
              />
            </EditorField>
            <EditorField label="Strategy summary" hint="The guidance injected when this skill is recalled">
              <textarea
                className="skill-strategy-input"
                value={nullableStringValue(draft.strategy_summary)}
                disabled={props.busy}
                onChange={(event) => setField("strategy_summary", event.target.value || null)}
              />
            </EditorField>
          </EditorSection>

          <EditorSection
            title="Recall conditions"
            description="Control when the harness should consider this skill relevant."
          >
            <div className="skill-form-grid">
              <TagListEditor
                label="Triggers"
                hint="Keywords matched during skill retrieval"
                values={stringList(draft.triggers)}
                disabled={props.busy}
                onChange={(values) => setField("triggers", values)}
              />
              <TagListEditor
                label="Task scope"
                hint="Task families, entities, items, and actions"
                values={stringList(draft.task_scope)}
                disabled={props.busy}
                onChange={(values) => setField("task_scope", values)}
              />
              <TagListEditor
                label="Dependencies"
                hint="Required tools, actions, items, or concepts"
                values={stringList(draft.dependencies)}
                disabled={props.busy}
                onChange={(values) => setField("dependencies", values)}
              />
            </div>
            <RuleListEditor
              label="Preconditions"
              hint="Facts that should hold before the skill is applied"
              addLabel="Add precondition"
              values={stringList(draft.preconditions)}
              disabled={props.busy}
              onChange={(values) => setField("preconditions", values)}
            />
          </EditorSection>

          <EditorSection
            title="Execution guidance"
            description="A coordinate-free plan the agent can adapt to the current Minecraft world."
          >
            <PlanEditor
              values={plan}
              disabled={props.busy}
              onChange={(values) => setField("parameterized_plan", values)}
            />
            <ActionPlanEditor
              listId={`skill-action-types-${props.skillId}`}
              values={actionList(draft.action_plan)}
              disabled={props.busy}
              onChange={(values) => setField("action_plan", values)}
              onValidationChange={setActionPlanError}
            />
            <RuleListEditor
              label="Recovery policy"
              hint="Fallback guidance after navigation, perception, or action failures"
              addLabel="Add recovery rule"
              values={stringList(draft.recovery_policy)}
              disabled={props.busy}
              onChange={(values) => setField("recovery_policy", values)}
            />
          </EditorSection>

          <section className="skill-preserved-fields">
            <div>
              <strong>Evidence and runtime fields are preserved</strong>
              <p>
                Source evidence, verifier statistics, replay actions, validation, metrics, step range, and
                {extensionKeys.length ? ` ${extensionKeys.length} extension field${extensionKeys.length === 1 ? "" : "s"}` : " extension metadata"}
                {" "}remain unchanged in Guided mode.
              </p>
            </div>
            <button className="icon-button labeled" type="button" onClick={() => switchMode("json")}>
              <Code2 size={15} aria-hidden="true" />
              Review advanced fields
            </button>
          </section>
        </div>
      ) : (
        <div
          className="skill-json-mode"
          id={`skill-json-panel-${props.skillId}`}
          role="tabpanel"
          aria-labelledby={`skill-json-tab-${props.skillId}`}
        >
          <label htmlFor={`skill-json-editor-${props.skillId}`}>
            <strong>Canonical SkillSpec</strong>
            <small>Use this only for fields not covered by the guided editor.</small>
          </label>
          <textarea
            id={`skill-json-editor-${props.skillId}`}
            value={jsonText}
            disabled={props.busy}
            onChange={(event) => setJsonText(event.target.value)}
            spellCheck={false}
          />
          <p className="skill-editor-note">
            Lifecycle is managed by Promote and Deprecate. <code>source_run_id</code> is immutable audit provenance;
            changing either field here will be rejected.
          </p>
        </div>
      )}

      {localError ? <p className="skill-editor-error" role="alert">{localError}</p> : null}

      <footer className="skill-editor-footer">
        <p>
          {dirty ? "Unsaved changes" : "No changes yet"} · updates apply to the next Skill snapshot.
        </p>
        <div className="skill-editor-actions">
          <button
            className="icon-button labeled"
            type="button"
            disabled={props.busy || !dirty}
            onClick={save}
          >
            <Save size={15} aria-hidden="true" />
            Save changes
          </button>
          <button className="icon-button labeled" type="button" disabled={props.busy} onClick={cancel}>
            <X size={15} aria-hidden="true" />
            Cancel
          </button>
        </div>
      </footer>
    </section>
  );
}

interface ActionRow {
  id: string;
  action: Record<string, unknown>;
  argsText: string;
  error: string | null;
}

const ACTION_TYPES = [
  "resolve_terms",
  "get_recipe",
  "retrieve_docs",
  "scan_blocks",
  "scan_entities",
  "scan_dropped_items",
  "move_to",
  "follow",
  "dig_block_at",
  "wait_ticks",
  "process_item",
  "craft_item",
  "smelt_item",
  "place_block",
  "equip_item",
  "fight_entity",
  "use_item",
  "consume_item",
  "move_to_and_engage_combat",
  "engage_combat",
  "query_inventory",
  "execute_skill",
  "request_visual_snapshot",
  "submit_for_evaluation"
] as const;

let actionRowSequence = 0;

function ActionPlanEditor(props: {
  listId: string;
  values: Record<string, unknown>[];
  disabled: boolean;
  onChange: (values: Record<string, unknown>[]) => void;
  onValidationChange: (error: string | null) => void;
}) {
  const [rows, setRows] = useState<ActionRow[]>(() => props.values.map(createActionRow));
  const legacyRows = rows
    .map((row, index) => ({ index, type: stringValue(row.action.type) }))
    .filter((row) => row.type === "mine_block");

  const commitRows = (next: ActionRow[]) => {
    setRows(next);
    const firstError = next.find((row) => row.error)?.error ?? null;
    props.onValidationChange(firstError);
    props.onChange(next.map((row) => row.action));
  };

  const updateType = (index: number, type: string) => {
    commitRows(rows.map((row, currentIndex) => (
      currentIndex === index ? { ...row, action: { ...row.action, type } } : row
    )));
  };

  const updateArgs = (index: number, argsText: string) => {
    const parsed = parseActionArgs(argsText);
    commitRows(rows.map((row, currentIndex) => {
      if (currentIndex !== index) {
        return row;
      }
      return {
        ...row,
        argsText,
        error: "error" in parsed ? parsed.error : null,
        action: "args" in parsed ? { ...row.action, args: parsed.args } : row.action
      };
    }));
  };

  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= rows.length) {
      return;
    }
    const next = [...rows];
    [next[index], next[target]] = [next[target], next[index]];
    commitRows(next);
  };

  return (
    <details className="skill-action-plan">
      <summary>
        <span>
          <strong>Source replay actions</strong>
          <small>{rows.length} recorded actions · edit only when the reusable source trace needs correction</small>
        </span>
        {legacyRows.length ? <span className="skill-action-warning">{legacyRows.length} legacy action</span> : null}
      </summary>
      <div className="skill-action-plan-body">
        {legacyRows.length ? (
          <div className="skill-inline-warning" role="note">
            <AlertTriangle size={16} aria-hidden="true" />
            <p>
              <strong>Legacy action needs migration.</strong>
              {" "}Replace <code>mine_block</code> with <code>dig_block_at</code> before saving this Skill.
            </p>
          </div>
        ) : null}
        <datalist id={props.listId}>
          {ACTION_TYPES.map((type) => <option value={type} key={type} />)}
        </datalist>
        <div className="skill-action-list">
          {rows.map((row, index) => (
            <article className="skill-action-row" key={row.id}>
              <header>
                <span>Action {index + 1}</span>
                <div className="skill-plan-actions">
                  <button
                    className="icon-button"
                    type="button"
                    disabled={props.disabled || index === 0}
                    onClick={() => move(index, -1)}
                    aria-label={`Move source action ${index + 1} up`}
                  >
                    <ChevronUp size={14} aria-hidden="true" />
                  </button>
                  <button
                    className="icon-button"
                    type="button"
                    disabled={props.disabled || index === rows.length - 1}
                    onClick={() => move(index, 1)}
                    aria-label={`Move source action ${index + 1} down`}
                  >
                    <ChevronDown size={14} aria-hidden="true" />
                  </button>
                  <button
                    className="icon-button danger"
                    type="button"
                    disabled={props.disabled}
                    onClick={() => commitRows(rows.filter((_, currentIndex) => currentIndex !== index))}
                    aria-label={`Remove source action ${index + 1}`}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </div>
              </header>
              <div className="skill-action-fields">
                <EditorField label="Action type" hint="Choose an implemented harness action">
                  <input
                    list={props.listId}
                    value={stringValue(row.action.type)}
                    disabled={props.disabled}
                    onChange={(event) => updateType(index, event.target.value)}
                  />
                </EditorField>
                <EditorField label="Arguments" hint="Only this action's argument object">
                  <textarea
                    className="skill-action-args"
                    value={row.argsText}
                    disabled={props.disabled}
                    spellCheck={false}
                    aria-invalid={Boolean(row.error)}
                    onChange={(event) => updateArgs(index, event.target.value)}
                  />
                </EditorField>
              </div>
              {row.error ? <p className="skill-action-error" role="alert">{row.error}</p> : null}
            </article>
          ))}
        </div>
        <button
          className="icon-button labeled skill-add-row"
          type="button"
          disabled={props.disabled}
          onClick={() => commitRows([...rows, createActionRow({ type: "", args: {} })])}
        >
          <Plus size={14} aria-hidden="true" />
          Add source action
        </button>
      </div>
    </details>
  );
}

function createActionRow(action: Record<string, unknown>): ActionRow {
  actionRowSequence += 1;
  return {
    id: `action-row-${actionRowSequence}`,
    action: { ...action, args: isRecord(action.args) ? action.args : {} },
    argsText: formatJson(isRecord(action.args) ? action.args : {}),
    error: null
  };
}

function EditorSection(props: { title: string; description: string; children: React.ReactNode }) {
  return (
    <section className="skill-editor-section">
      <header>
        <strong>{props.title}</strong>
        <p>{props.description}</p>
      </header>
      <div className="skill-editor-section-body">{props.children}</div>
    </section>
  );
}

function EditorField(props: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <label className="skill-form-field">
      <span>
        <strong>{props.label}</strong>
        <small>{props.hint}</small>
      </span>
      {props.children}
    </label>
  );
}

function TagListEditor(props: {
  label: string;
  hint: string;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  const [pending, setPending] = useState("");

  const addPending = () => {
    const additions = pending
      .split(/[,\n]/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!additions.length) {
      return;
    }
    props.onChange(Array.from(new Set([...props.values, ...additions])));
    setPending("");
  };

  return (
    <div className="skill-list-field">
      <span className="skill-field-label">
        <strong>{props.label}</strong>
        <small>{props.hint}</small>
      </span>
      <div className="skill-tag-editor">
        <div className="skill-tag-list">
          {props.values.length ? props.values.map((value) => (
            <span className="skill-editable-tag" key={value}>
              {value}
              <button
                type="button"
                disabled={props.disabled}
                onClick={() => props.onChange(props.values.filter((candidate) => candidate !== value))}
                aria-label={`Remove ${value}`}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </span>
          )) : <small>No values yet</small>}
        </div>
        <div className="skill-tag-input">
          <input
            value={pending}
            disabled={props.disabled}
            placeholder="Type a value and press Enter"
            aria-label={`Add ${props.label.toLowerCase()} value`}
            onChange={(event) => setPending(event.target.value)}
            onBlur={addPending}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === ",") {
                event.preventDefault();
                addPending();
              }
            }}
          />
          <button className="icon-button" type="button" disabled={props.disabled || !pending.trim()} onClick={addPending}>
            <Plus size={15} aria-hidden="true" />
            <span className="sr-only">Add {props.label.toLowerCase()} value</span>
          </button>
        </div>
      </div>
    </div>
  );
}

function RuleListEditor(props: {
  label: string;
  hint: string;
  addLabel: string;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  const update = (index: number, value: string) => {
    props.onChange(props.values.map((current, currentIndex) => currentIndex === index ? value : current));
  };

  return (
    <div className="skill-list-field">
      <span className="skill-field-label">
        <strong>{props.label}</strong>
        <small>{props.hint}</small>
      </span>
      <div className="skill-rule-list">
        {props.values.map((value, index) => (
          <div className="skill-rule-row" key={`${index}-${props.label}`}>
            <span>{index + 1}</span>
            <textarea
              value={value}
              disabled={props.disabled}
              rows={2}
              aria-label={`${props.label} ${index + 1}`}
              onChange={(event) => update(index, event.target.value)}
            />
            <button
              className="icon-button danger"
              type="button"
              disabled={props.disabled}
              onClick={() => props.onChange(props.values.filter((_, currentIndex) => currentIndex !== index))}
              aria-label={`Remove ${props.label.toLowerCase()} ${index + 1}`}
            >
              <Trash2 size={14} aria-hidden="true" />
            </button>
          </div>
        ))}
        {!props.values.length ? <p className="skill-list-empty">No rules recorded.</p> : null}
        <button
          className="icon-button labeled skill-add-row"
          type="button"
          disabled={props.disabled}
          onClick={() => props.onChange([...props.values, ""])}
        >
          <Plus size={14} aria-hidden="true" />
          {props.addLabel}
        </button>
      </div>
    </div>
  );
}

function PlanEditor(props: {
  values: Record<string, unknown>[];
  disabled: boolean;
  onChange: (values: Record<string, unknown>[]) => void;
}) {
  const updateField = (index: number, key: string, value: string) => {
    props.onChange(props.values.map((step, currentIndex) => {
      if (currentIndex !== index) {
        return step;
      }
      const next = { ...step };
      if (!value && !["type", "target"].includes(key)) {
        delete next[key];
      } else {
        next[key] = value;
      }
      return next;
    }));
  };

  const move = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= props.values.length) {
      return;
    }
    const next = [...props.values];
    [next[index], next[target]] = [next[target], next[index]];
    props.onChange(next);
  };

  return (
    <div className="skill-plan-editor">
      <span className="skill-field-label">
        <strong>Parameterized plan</strong>
        <small>Semantic steps are reused; source-world coordinates remain evidence only</small>
      </span>
      <div className="skill-plan-list">
        {props.values.map((step, index) => {
          const advancedCount = Object.keys(step).filter(
            (key) => !PLAN_FIELDS.some(([field]) => field === key)
          ).length;
          return (
            <details className="skill-plan-step" key={`plan-step-${index}`}>
              <summary>
                <div>
                  <span>Step {index + 1}</span>
                  <strong>{stringValue(step.type) || "New action"}</strong>
                  {stringValue(step.target) ? <small>{stringValue(step.target)}</small> : null}
                </div>
                <div className="skill-plan-actions">
                  <button
                    className="icon-button"
                    type="button"
                    disabled={props.disabled || index === 0}
                    onClick={(event) => {
                      event.preventDefault();
                      move(index, -1);
                    }}
                    aria-label={`Move step ${index + 1} up`}
                  >
                    <ChevronUp size={14} aria-hidden="true" />
                  </button>
                  <button
                    className="icon-button"
                    type="button"
                    disabled={props.disabled || index === props.values.length - 1}
                    onClick={(event) => {
                      event.preventDefault();
                      move(index, 1);
                    }}
                    aria-label={`Move step ${index + 1} down`}
                  >
                    <ChevronDown size={14} aria-hidden="true" />
                  </button>
                  <button
                    className="icon-button danger"
                    type="button"
                    disabled={props.disabled}
                    onClick={(event) => {
                      event.preventDefault();
                      props.onChange(props.values.filter((_, currentIndex) => currentIndex !== index));
                    }}
                    aria-label={`Remove step ${index + 1}`}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </div>
              </summary>
              <div className="skill-plan-fields">
                {PLAN_FIELDS.map(([key, label, hint]) => (
                  <EditorField label={label} hint={hint} key={key}>
                    <input
                      value={stringValue(step[key])}
                      disabled={props.disabled}
                      placeholder={key === "type" || key === "target" ? "Required for a useful step" : "Optional"}
                      onChange={(event) => updateField(index, key, event.target.value)}
                    />
                  </EditorField>
                ))}
              </div>
              {advancedCount ? (
                <p className="skill-plan-advanced-note">
                  {advancedCount} additional step field{advancedCount === 1 ? "" : "s"} preserved in Advanced JSON.
                </p>
              ) : null}
            </details>
          );
        })}
        {!props.values.length ? (
          <div className="skill-plan-empty">
            <ListTree size={20} aria-hidden="true" />
            <p>No reusable steps recorded. Add the first semantic action below.</p>
          </div>
        ) : null}
        <button
          className="icon-button labeled skill-add-row"
          type="button"
          disabled={props.disabled}
          onClick={() => props.onChange([...props.values, { type: "", target: "" }])}
        >
          <Plus size={14} aria-hidden="true" />
          Add plan step
        </button>
      </div>
    </div>
  );
}

function parseSpecJson(value: string): { spec: Record<string, unknown> } | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (caught) {
    return {
      error: `SkillSpec is not valid JSON: ${caught instanceof Error ? caught.message : String(caught)}`
    };
  }
  if (!isRecord(parsed)) {
    return { error: "Canonical SkillSpec must be a JSON object." };
  }
  return { spec: parsed };
}

function validateGuidedFields(spec: Record<string, unknown>): string | null {
  if (!stringValue(spec.name).trim()) {
    return "Name is required.";
  }
  if (!stringValue(spec.version).trim()) {
    return "Version is required.";
  }
  if (!stringValue(spec.description).trim()) {
    return "Description is required.";
  }
  const invalidStep = recordList(spec.parameterized_plan).findIndex(
    (step) => !stringValue(step.type).trim()
  );
  if (invalidStep >= 0) {
    return `Plan step ${invalidStep + 1} needs an action type.`;
  }
  const legacyAction = actionList(spec.action_plan).findIndex(
    (action) => stringValue(action.type) === "mine_block"
  );
  if (legacyAction >= 0) {
    return `Source action ${legacyAction + 1} uses legacy type mine_block. Replace it with dig_block_at before saving.`;
  }
  const missingActionType = recordList(spec.action_plan).findIndex(
    (action) => !stringValue(action.type).trim()
  );
  if (missingActionType >= 0) {
    return `Source action ${missingActionType + 1} needs an action type.`;
  }
  const invalidActionArgs = recordList(spec.action_plan).findIndex(
    (action) => !isRecord(action.args)
  );
  if (invalidActionArgs >= 0) {
    return `Source action ${invalidActionArgs + 1} arguments must be a JSON object.`;
  }
  return null;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableStringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function recordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function actionList(value: unknown): Record<string, unknown>[] {
  return recordList(value).map((action) => ({
    ...action,
    args: isRecord(action.args) ? action.args : {}
  }));
}

function parseActionArgs(value: string): { args: Record<string, unknown> } | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch (caught) {
    return {
      error: `Arguments are not valid JSON: ${caught instanceof Error ? caught.message : String(caught)}`
    };
  }
  if (!isRecord(parsed)) {
    return { error: "Arguments must be a JSON object." };
  }
  return { args: parsed };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cloneSpec(spec: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(spec)) as Record<string, unknown>;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
