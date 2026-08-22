import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Dices,
  Eye,
  LoaderCircle,
  Play,
  RefreshCw,
  Search,
  Settings2,
  TerminalSquare,
  UserRound,
  XCircle
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  LaunchJob,
  LaunchPreflight,
  LaunchRequest,
  LaunchTaskDetail,
  LaunchTaskPage,
  LaunchTaskSummary,
  cancelLaunchJob,
  getLaunchJob,
  getLaunchJobLogs,
  getLaunchJobs,
  getLaunchTask,
  getLaunchTasks,
  getLauncherConfig,
  getRandomLaunchTask,
  isActiveLaunchJobStatus,
  preflightLaunch,
  startLaunchJob
} from "../../api/client";

const PAGE_SIZE = 32;

/** Parent callbacks used to connect quick start with global navigation and counters. */
interface QuickStartWorkspaceProps {
  onOpenRuntime: () => void;
  onJobsChanged: () => void;
}

/** Editable launch fields that are independent of the selected task id. */
type LaunchForm = Omit<LaunchRequest, "task_id">;

/** Filter state shared by catalog browsing and server-side random selection. */
interface TaskFilters {
  query: string;
  kind: "all" | "programmatic" | "creative";
  category: string;
}

/** Four-stage local task launcher with catalog search, preflight, process status, and logs. */
export function QuickStartWorkspace(props: QuickStartWorkspaceProps) {
  const [page, setPage] = useState<LaunchTaskPage | null>(null);
  const [filters, setFilters] = useState<TaskFilters>({
    query: "",
    kind: "all",
    category: ""
  });
  const [searchDraft, setSearchDraft] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedTask, setSelectedTask] = useState<LaunchTaskDetail | null>(null);
  const [form, setForm] = useState<LaunchForm | null>(null);
  const [preflight, setPreflight] = useState<LaunchPreflight | null>(null);
  const [job, setJob] = useState<LaunchJob | null>(null);
  const [logText, setLogText] = useState("");
  const [busy, setBusy] = useState<"catalog" | "random" | "preflight" | "launch" | "cancel" | null>(null);
  const [drawLabel, setDrawLabel] = useState("READY");
  const [error, setError] = useState<string | null>(null);
  const drawTimerRef = useRef<number | null>(null);
  const logOffsetRef = useRef(0);

  const loadCatalog = useCallback(async () => {
    setBusy((current) => current ?? "catalog");
    try {
      const nextPage = await getLaunchTasks({
        query: filters.query,
        kind: filters.kind,
        category: filters.category || undefined,
        offset,
        limit: PAGE_SIZE
      });
      setPage(nextPage);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy((current) => (current === "catalog" ? null : current));
    }
  }, [filters, offset]);

  useEffect(() => {
    void Promise.all([getLauncherConfig(), getLaunchJobs()])
      .then(([config, jobs]) => {
        setForm({
          view_mode: "agent",
          client_player: config.default_client_player ?? "",
          server_host: config.server_host,
          server_port: config.server_port,
          rcon_host: config.rcon_host,
          rcon_port: config.rcon_port,
          max_steps: 40,
          max_runtime_sec: 900,
          threat_pause: true,
          random_spawn: true,
          auto_promote: false
        });
        setJob(
          jobs.find((candidate) => isActiveLaunchJobStatus(candidate.status)) ??
            jobs[0] ??
            null
        );
      })
      .catch((caught) => setError(errorMessage(caught)));
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    if (!job) {
      return;
    }
    logOffsetRef.current = 0;
    setLogText("");
  }, [job?.job_id]);

  useEffect(() => {
    if (!job || !isActiveLaunchJobStatus(job.status)) {
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const [nextJob, nextLog] = await Promise.all([
          getLaunchJob(job.job_id),
          getLaunchJobLogs(job.job_id, logOffsetRef.current)
        ]);
        if (cancelled) {
          return;
        }
        setJob(nextJob);
        logOffsetRef.current = nextLog.next_offset;
        if (nextLog.content) {
          setLogText((current) => `${current}${nextLog.content}`.slice(-80_000));
        }
        if (!isActiveLaunchJobStatus(nextJob.status)) {
          props.onJobsChanged();
        }
      } catch (caught) {
        if (!cancelled) {
          setError(errorMessage(caught));
        }
      }
    };
    void poll();
    const intervalId = window.setInterval(() => void poll(), 1200);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [job?.job_id, job?.status, props.onJobsChanged]);

  useEffect(() => {
    return () => {
      if (drawTimerRef.current !== null) {
        window.clearInterval(drawTimerRef.current);
      }
    };
  }, []);

  const selectTask = useCallback(async (task: LaunchTaskSummary) => {
    setError(null);
    setPreflight(null);
    try {
      const detail = await getLaunchTask(task.task_id);
      setSelectedTask(detail);
      setForm((current) =>
        current
          ? {
              ...current,
              view_mode: detail.kind === "creative" ? "agent" : current.view_mode,
              max_steps: detail.kind === "creative" ? 80 : 40,
              max_runtime_sec: detail.kind === "creative" ? 1800 : 900
            }
          : current
      );
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, []);

  const submitSearch = () => {
    setOffset(0);
    setFilters((current) => ({ ...current, query: searchDraft.trim() }));
  };

  const drawRandom = async () => {
    if (busy) {
      return;
    }
    setBusy("random");
    setError(null);
    setPreflight(null);
    const candidates = page?.items ?? [];
    let index = 0;
    drawTimerRef.current = window.setInterval(() => {
      const candidate = candidates[index % Math.max(candidates.length, 1)];
      setDrawLabel(candidate ? compactTaskName(candidate.task_id) : `TASK ${index + 1}`);
      index += 1;
    }, 80);
    try {
      const [winner] = await Promise.all([
        getRandomLaunchTask({
          query: filters.query,
          kind: filters.kind,
          category: filters.category || undefined
        }),
        delay(1550)
      ]);
      if (drawTimerRef.current !== null) {
        window.clearInterval(drawTimerRef.current);
        drawTimerRef.current = null;
      }
      setDrawLabel(compactTaskName(winner.task_id));
      await selectTask(winner);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      if (drawTimerRef.current !== null) {
        window.clearInterval(drawTimerRef.current);
        drawTimerRef.current = null;
      }
      setBusy(null);
    }
  };

  const launchRequest = useMemo(
    () =>
      selectedTask && form
        ? {
            ...form,
            task_id: selectedTask.task_id
          }
        : null,
    [form, selectedTask]
  );

  const runPreflight = async () => {
    if (!launchRequest) {
      return;
    }
    setBusy("preflight");
    try {
      setPreflight(await preflightLaunch(launchRequest));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const launch = async () => {
    if (!launchRequest) {
      return;
    }
    setBusy("launch");
    try {
      const result = await startLaunchJob(launchRequest);
      setPreflight(result.preflight);
      setJob(result.job);
      setError(null);
      props.onJobsChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    if (!job) {
      return;
    }
    setBusy("cancel");
    try {
      setJob(await cancelLaunchJob(job.job_id));
      props.onJobsChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const activeJob = Boolean(job && isActiveLaunchJobStatus(job.status));
  const creativeViewConflict =
    selectedTask?.kind === "creative" && form?.view_mode !== "agent";

  return (
    <section className="quick-start-workspace">
      <div className="launch-step-grid">
        <section className="launch-stage view-stage">
          <StageHeader number="1" title="Choose View" icon={<Eye size={17} />} />
          <div className="view-mode-control" role="radiogroup" aria-label="Minecraft client view">
            <button
              className={form?.view_mode === "agent" ? "active" : ""}
              type="button"
              role="radio"
              aria-checked={form?.view_mode === "agent"}
              onClick={() => setForm((current) => current ? { ...current, view_mode: "agent" } : current)}
            >
              <Eye size={19} aria-hidden="true" />
              <span>
                <strong>Agent POV</strong>
                <small>Client follows the bot. Required for creative evidence.</small>
              </span>
            </button>
            <button
              className={form?.view_mode === "player" ? "active" : ""}
              type="button"
              role="radio"
              aria-checked={form?.view_mode === "player"}
              disabled={selectedTask?.kind === "creative"}
              onClick={() => setForm((current) => current ? { ...current, view_mode: "player" } : current)}
            >
              <UserRound size={19} aria-hidden="true" />
              <span>
                <strong>Player View</strong>
                <small>Keep normal client control while the bot runs independently.</small>
              </span>
            </button>
          </div>
        </section>

        <section className="launch-stage configuration-stage">
          <StageHeader number="2" title="Local Environment" icon={<Settings2 size={17} />} />
          {form ? <LaunchConfigurationForm form={form} onChange={setForm} /> : <LoadingLine />}
        </section>
      </div>

      <section className="launch-stage catalog-stage">
        <StageHeader
          number="3"
          title="Select Task"
          icon={<Search size={17} />}
          meta={page ? `${page.total.toLocaleString()} matching tasks` : "Loading catalog"}
        />
        <div className="catalog-toolbar">
          <form
            className="task-search"
            onSubmit={(event) => {
              event.preventDefault();
              submitSearch();
            }}
          >
            <Search size={16} aria-hidden="true" />
            <input
              value={searchDraft}
              placeholder="Search task id, goal, family, or category"
              aria-label="Search executable tasks"
              onChange={(event) => setSearchDraft(event.target.value)}
            />
            <button type="submit">Search</button>
          </form>
          <select
            aria-label="Task kind"
            value={filters.kind}
            onChange={(event) => {
              setOffset(0);
              setSelectedTask(null);
              setFilters((current) => ({
                ...current,
                kind: event.target.value as TaskFilters["kind"]
              }));
            }}
          >
            <option value="all">All task types</option>
            <option value="programmatic">Programmatic</option>
            <option value="creative">Creative</option>
          </select>
          <select
            aria-label="Task category"
            value={filters.category}
            onChange={(event) => {
              setOffset(0);
              setSelectedTask(null);
              setFilters((current) => ({ ...current, category: event.target.value }));
            }}
          >
            <option value="">All categories</option>
            {Object.entries(page?.categories ?? {})
              .sort(([left], [right]) => left.localeCompare(right))
              .map(([category, count]) => (
                <option value={category} key={category}>
                  {category} ({count})
                </option>
              ))}
          </select>
          <button className="random-task-button" type="button" onClick={() => void drawRandom()} disabled={Boolean(busy)}>
            <Dices size={17} aria-hidden="true" />
            Random Task
          </button>
        </div>

        <div className="task-lottery" aria-live="polite">
          <span>RANDOM SELECTOR</span>
          <strong className={busy === "random" ? "spinning" : ""}>{drawLabel}</strong>
          <small>Uses the active search and category filters</small>
        </div>

        <div className="catalog-layout">
          <div className="launch-task-list" aria-busy={busy === "catalog"}>
            {page?.items.map((task) => (
              <button
                className={selectedTask?.task_id === task.task_id ? "selected" : ""}
                type="button"
                key={task.task_id}
                onClick={() => void selectTask(task)}
              >
                <span className={`task-kind-mark ${task.kind}`} aria-hidden="true" />
                <span>
                  <strong>{task.goal}</strong>
                  <small>{task.task_id}</small>
                </span>
                <span className="task-list-meta">
                  <b>{task.category}</b>
                  <small>{task.verifier_type}</small>
                </span>
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            ))}
            {page && !page.items.length ? <div className="catalog-empty">No tasks match these filters.</div> : null}
          </div>
          <TaskConfigurationPanel task={selectedTask} />
        </div>
        <footer className="catalog-pagination">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
            aria-label="Previous task page"
          >
            <ChevronLeft size={16} />
          </button>
          <span>
            {page?.total
              ? `${offset + 1}-${Math.min(offset + PAGE_SIZE, page.total)} of ${page.total}`
              : "0 tasks"}
          </span>
          <button
            type="button"
            disabled={!page || offset + PAGE_SIZE >= page.total}
            onClick={() => setOffset((current) => current + PAGE_SIZE)}
            aria-label="Next task page"
          >
            <ChevronRight size={16} />
          </button>
        </footer>
      </section>

      <section className="launch-stage run-stage">
        <StageHeader number="4" title="Preflight & Run" icon={<Play size={17} />} />
        {error ? (
          <div className="launcher-alert" role="alert">
            <AlertTriangle size={17} />
            <span>{error}</span>
          </div>
        ) : null}
        {creativeViewConflict ? (
          <div className="launcher-alert" role="alert">
            <AlertTriangle size={17} />
            <span>Creative tasks require Agent POV so the final evidence is attributable to the bot.</span>
          </div>
        ) : null}
        <div className="launch-control-row">
          <div>
            <span>Selected task</span>
            <strong>{selectedTask?.goal ?? "Choose a task from the catalog"}</strong>
            <small>{selectedTask?.task_id ?? "No executable manifest selected"}</small>
          </div>
          <div className="launch-commands">
            <button
              className="secondary-command"
              type="button"
              disabled={!launchRequest || Boolean(busy) || activeJob}
              onClick={() => void runPreflight()}
            >
              <RefreshCw size={16} />
              Check Environment
            </button>
            <button
              className="primary-command"
              type="button"
              disabled={!launchRequest || Boolean(busy) || activeJob || creativeViewConflict}
              onClick={() => void launch()}
            >
              {busy === "launch" ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              Start Task
            </button>
          </div>
        </div>
        {preflight ? <PreflightEvidence preflight={preflight} /> : null}
        {job ? (
          <LaunchJobPanel
            job={job}
            logText={logText}
            busy={busy === "cancel"}
            onCancel={() => void cancel()}
            onOpenRuntime={props.onOpenRuntime}
          />
        ) : null}
      </section>
    </section>
  );
}

/** Numbered section header shared by the four quick-start stages. */
function StageHeader(props: { number: string; title: string; icon: React.ReactNode; meta?: string }) {
  return (
    <header className="launch-stage-header">
      <span className="stage-number">{props.number}</span>
      <span className="stage-icon">{props.icon}</span>
      <strong>{props.title}</strong>
      {props.meta ? <small>{props.meta}</small> : null}
    </header>
  );
}

/** Bounded local environment controls; credentials remain backend-owned. */
function LaunchConfigurationForm(props: {
  form: LaunchForm;
  onChange: React.Dispatch<React.SetStateAction<LaunchForm | null>>;
}) {
  const patch = (next: Partial<LaunchForm>) =>
    props.onChange((current) => current ? { ...current, ...next } : current);
  return (
    <div className="launch-config-form">
      <label>
        <span>Client player</span>
        <input
          value={props.form.client_player}
          placeholder="Minecraft username"
          onChange={(event) => patch({ client_player: event.target.value })}
        />
      </label>
      <label>
        <span>Server</span>
        <span className="inline-fields">
          <input
            value={props.form.server_host}
            aria-label="Minecraft server host"
            onChange={(event) => patch({ server_host: event.target.value })}
          />
          <input
            type="number"
            value={props.form.server_port}
            aria-label="Minecraft server port"
            onChange={(event) => patch({ server_port: Number(event.target.value) })}
          />
        </span>
      </label>
      <label>
        <span>RCON</span>
        <span className="inline-fields">
          <input
            value={props.form.rcon_host}
            aria-label="RCON host"
            onChange={(event) => patch({ rcon_host: event.target.value })}
          />
          <input
            type="number"
            value={props.form.rcon_port}
            aria-label="RCON port"
            onChange={(event) => patch({ rcon_port: Number(event.target.value) })}
          />
        </span>
      </label>
      <label>
        <span>Step limit</span>
        <input
          type="number"
          min="1"
          max="300"
          value={props.form.max_steps}
          onChange={(event) => patch({ max_steps: Number(event.target.value) })}
        />
      </label>
      <label>
        <span>Runtime (sec)</span>
        <input
          type="number"
          min="30"
          max="7200"
          value={props.form.max_runtime_sec}
          onChange={(event) => patch({ max_runtime_sec: Number(event.target.value) })}
        />
      </label>
      <div className="launch-toggle-list">
        <ToggleControl label="Threat pause" checked={props.form.threat_pause} onChange={(value) => patch({ threat_pause: value })} />
        <ToggleControl label="Random spawn" checked={props.form.random_spawn} onChange={(value) => patch({ random_spawn: value })} />
        <ToggleControl label="Auto-promote" checked={props.form.auto_promote} onChange={(value) => patch({ auto_promote: value })} />
      </div>
    </div>
  );
}

/** Accessible compact toggle used for binary launch settings. */
function ToggleControl(props: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="switch-control">
      <input type="checkbox" checked={props.checked} onChange={(event) => props.onChange(event.target.checked)} />
      <span aria-hidden="true" />
      <strong>{props.label}</strong>
    </label>
  );
}

/** Selected task manifest summary with expandable reset and action details. */
function TaskConfigurationPanel(props: { task: LaunchTaskDetail | null }) {
  if (!props.task) {
    return (
      <aside className="task-configuration empty">
        <Search size={20} aria-hidden="true" />
        <strong>Select a task</strong>
        <span>Its reset plan, verifier, and action contract will appear here.</span>
      </aside>
    );
  }
  const task = props.task;
  return (
    <aside className="task-configuration">
      <header>
        <span className={`task-kind-badge ${task.kind}`}>{task.kind}</span>
        <strong>{task.goal}</strong>
        <small>{task.task_id}</small>
      </header>
      <dl>
        <div><dt>Category</dt><dd>{task.category}</dd></div>
        <div><dt>Verifier</dt><dd>{task.verifier_type}</dd></div>
        <div><dt>Biome</dt><dd>{task.biome_hint ?? "not specified"}</dd></div>
        <div><dt>Runtime</dt><dd>{task.runtime_profile || "default"}</dd></div>
        <div><dt>Initial items</dt><dd>{task.initial_inventory_count}</dd></div>
        <div><dt>Spawn mobs</dt><dd>{task.spawn_mob_count}</dd></div>
      </dl>
      {task.description ? <p>{task.description}</p> : null}
      <details>
        <summary>Reset & verifier configuration</summary>
        <pre>{JSON.stringify({ reset_plan: task.reset_plan, verifier: task.verifier }, null, 2)}</pre>
      </details>
      <details>
        <summary>Available action primitives ({task.allowed_actions.length})</summary>
        <div className="primitive-list">
          {task.allowed_actions.map((action) => <span key={action}>{action}</span>)}
        </div>
      </details>
    </aside>
  );
}

/** Readiness checks shown before and after a launch request. */
function PreflightEvidence(props: { preflight: LaunchPreflight }) {
  const phase = !props.preflight.launchable
    ? "blocked"
    : props.preflight.runtime_ready
      ? "ready"
      : "pending";
  const heading =
    phase === "blocked"
      ? "Environment blocked"
      : phase === "ready"
        ? "Environment ready"
        : "Ready to prepare environment";
  return (
    <section className={`preflight-evidence ${phase}`}>
      <header>
        {phase === "ready" ? (
          <CheckCircle2 size={17} />
        ) : phase === "pending" ? (
          <LoaderCircle size={17} />
        ) : (
          <XCircle size={17} />
        )}
        <strong>{heading}</strong>
      </header>
      <div>
        {props.preflight.checks.map((check) => (
          <article className={check.state} key={check.name}>
            {check.state === "ready" ? (
              <CheckCircle2 size={15} />
            ) : check.state === "pending" ? (
              <LoaderCircle size={15} />
            ) : (
              <XCircle size={15} />
            )}
            <span>
              <strong>{check.name.replaceAll("_", " ")}</strong>
              <small>{check.detail}</small>
            </span>
          </article>
        ))}
      </div>
    </section>
  );
}

/** Active or completed child-process status and bounded live launcher logs. */
function LaunchJobPanel(props: {
  job: LaunchJob;
  logText: string;
  busy: boolean;
  onCancel: () => void;
  onOpenRuntime: () => void;
}) {
  const active = isActiveLaunchJobStatus(props.job.status);
  const cancelling = props.job.status === "cancelling";
  const joinAddress = formatServerAddress(
    props.job.server_host,
    props.job.server_port
  );
  return (
    <section className="launch-job-panel">
      <header>
        <div>
          <span className={`status-dot ${launchStatusClass(props.job.status)}`} />
          <span>
            <strong>{props.job.task_goal}</strong>
            <small>{props.job.job_id} · {formatLaunchStatus(props.job.status)}</small>
          </span>
        </div>
        <div className="launch-commands">
          <button className="secondary-command" type="button" onClick={props.onOpenRuntime}>
            <TerminalSquare size={16} />
            Runtime Audit
          </button>
          {active ? (
            <button
              className="danger-command"
              type="button"
              disabled={props.busy || cancelling}
              onClick={props.onCancel}
            >
              {cancelling ? <LoaderCircle className="spin" size={16} /> : <CircleStop size={16} />}
              {cancelling ? "Cancelling" : "Cancel"}
            </button>
          ) : null}
        </div>
      </header>
      {props.job.status === "waiting_for_client" ? (
        <div className="client-join-prompt" role="status" aria-live="polite">
          <UserRound size={20} aria-hidden="true" />
          <span>
            <strong>Join the Minecraft server</strong>
            <span>
              Connect to <code>{joinAddress}</code> as <code>{props.job.client_player}</code>.
            </span>
            <small>
              {props.job.status_detail ??
                "The launcher is checking the server automatically for this player."}
            </small>
            <small>
              Once the client is detected, the workflow resets the task and attaches the
              spectator view. Recording starts only after the camera is ready.
            </small>
          </span>
        </div>
      ) : props.job.status_detail ? (
        <div className="launch-status-detail" role="status" aria-live="polite">
          {props.job.status_detail}
        </div>
      ) : null}
      <div className="job-facts">
        <span><small>PID</small><strong>{props.job.pid ?? "-"}</strong></span>
        <span><small>View</small><strong>{props.job.view_mode}</strong></span>
        <span><small>Server</small><strong>{joinAddress}</strong></span>
        <span><small>Artifacts</small><strong>{props.job.artifact_dir}</strong></span>
        <span><small>Exit</small><strong>{props.job.return_code ?? "-"}</strong></span>
      </div>
      <pre className="launch-log">{props.logText || "Waiting for launcher output..."}</pre>
    </section>
  );
}

/** Minimal skeleton line used while backend defaults are loading. */
function LoadingLine() {
  return (
    <div className="launcher-loading">
      <LoaderCircle className="spin" size={17} />
      Loading local defaults
    </div>
  );
}

/** Convert a long generated task id to a stable lottery display label. */
function compactTaskName(taskId: string) {
  return taskId.length > 52 ? `${taskId.slice(0, 49)}...` : taskId;
}

/** Resolve caught values to concise UI-safe error messages. */
function errorMessage(caught: unknown) {
  return caught instanceof Error ? caught.message : String(caught);
}

/** Promise-based visual delay for the random task reel. */
function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

/** Map launcher lifecycle states to the dashboard's shared status colors. */
function launchStatusClass(status: LaunchJob["status"]) {
  if (status === "succeeded") {
    return "ok";
  }
  if (status === "failed" || status === "cancelled") {
    return "bad";
  }
  return "warn";
}

/** Format a host and port for copyable Minecraft connection instructions. */
function formatServerAddress(host: string, port: number) {
  const formattedHost = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `${formattedHost}:${port}`;
}

/** Convert machine lifecycle values into concise status labels. */
function formatLaunchStatus(status: LaunchJob["status"]) {
  return status.replaceAll("_", " ");
}
