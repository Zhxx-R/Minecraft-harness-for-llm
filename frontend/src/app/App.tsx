import {
  Activity,
  AlertTriangle,
  Bot,
  Boxes,
  CheckCircle2,
  ClipboardCheck,
  CircleHelp,
  ChevronRight,
  Clock,
  Database,
  FileSearch,
  GitCompare,
  Home,
  LayoutDashboard,
  Library,
  ListChecks,
  MessageSquareText,
  Pencil,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  Route,
  ScrollText,
  Settings2,
  ShieldCheck,
  TerminalSquare,
  Trash2,
  XCircle
} from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AgentDetail,
  AgentSummary,
  AgentTaskSummary,
  BenchmarkComparison,
  BenchmarkMode,
  CreativeEvaluation,
  EvaluationReportBreakdown,
  EvaluationReportRun,
  EvaluationReports,
  HumanReview,
  HumanReviewDecision,
  LaunchJob,
  ModelCall,
  RuntimeErrorRecord,
  RunDetail,
  RunReplay,
  SkillDetail,
  SkillSummary,
  TrajectoryEvent,
  decideHumanReview,
  deleteSkill,
  deprecateSkill,
  getAgentDetail,
  getAgents,
  getBenchmarkComparison,
  getCreativeEvaluations,
  getEvaluationReports,
  getHumanReviews,
  getLaunchJobs,
  getModelCalls,
  getRunDetail,
  getRunEvents,
  getRunReplay,
  getRuntimeErrors,
  getSkillDetail,
  getSkills,
  getApiAssetUrl,
  openRunEventStream,
  promoteSkill,
  updateSkill
} from "../api/client";
import { PromptConfigurationWorkspace } from "../features/configuration/PromptConfigurationWorkspace";
import { KnowledgeWorkspace } from "../features/knowledge/KnowledgeWorkspace";
import { QuickStartWorkspace } from "../features/quick-start/QuickStartWorkspace";
import { PortalHome } from "../features/portal/PortalHome";
import { SkillSpecEditor } from "../features/skills/SkillSpecEditor";
import { MainSection, sectionFromHash } from "./sections";

/** Agent detail category selected in the secondary audit sidebar. */
type AgentPanel = "overview" | "tasks" | "run" | "timeline";

/** Run detail evidence mode selected inside the run audit page. */
type RunEvidenceMode = "core" | "metrics" | "runtime" | "raw";

/** Optional evidence fields that are hidden by default in the core trace. */
interface EvidenceOptions {
  showTokens: boolean;
  showRequestMeta: boolean;
  showRawJson: boolean;
}

/** One public MineCLIP trend sample rendered in the creative review page. */
interface CreativeTrendPoint {
  windowIndex: number;
  score: number;
}

/** One path-free MineCLIP key frame served through the dashboard API. */
interface CreativeKeyFrame {
  imageUrl: string;
  score: number | null;
  sequence: number | null;
  windowIndex: number | null;
}

/** Top-level React application for the harness audit workspace. */
export function App() {
  const [mainSection, setMainSection] = useState<MainSection>(() =>
    sectionFromHash(window.location.hash)
  );
  const [agentPanel, setAgentPanel] = useState<AgentPanel>("overview");
  const [runEvidenceMode, setRunEvidenceMode] = useState<RunEvidenceMode>("core");
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [selectedAgentKey, setSelectedAgentKey] = useState<string | null>(null);
  const [agentDetail, setAgentDetail] = useState<AgentDetail | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<TrajectoryEvent[]>([]);
  const [modelCalls, setModelCalls] = useState<ModelCall[]>([]);
  const [runtimeErrors, setRuntimeErrors] = useState<RuntimeErrorRecord[]>([]);
  const [replay, setReplay] = useState<RunReplay | null>(null);
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [comparison, setComparison] = useState<BenchmarkComparison | null>(null);
  const [evaluationReports, setEvaluationReports] = useState<EvaluationReports | null>(null);
  const [creativeEvaluations, setCreativeEvaluations] = useState<CreativeEvaluation[]>([]);
  const [humanReviews, setHumanReviews] = useState<HumanReview[]>([]);
  const [launchJobs, setLaunchJobs] = useState<LaunchJob[]>([]);
  const [evidenceOptions, setEvidenceOptions] = useState<EvidenceOptions>({
    showTokens: false,
    showRequestMeta: false,
    showRawJson: false
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);
  const [streamStatus, setStreamStatus] = useState("idle");
  const [workspaceRefreshToken, setWorkspaceRefreshToken] = useState(0);
  const latestEventIdByRunRef = useRef<Record<string, number>>({});
  const syncTimerRef = useRef<number | null>(null);
  const refreshIndexRef = useRef<(() => Promise<void>) | null>(null);

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.key === selectedAgentKey) ?? null,
    [agents, selectedAgentKey]
  );

  const selectedTask = useMemo(
    () => agentDetail?.runs.find((run) => run.run_id === selectedRunId) ?? null,
    [agentDetail, selectedRunId]
  );

  const clearSelectedRunEvidence = useCallback(() => {
    setSelectedRunId(null);
    setRunDetail(null);
    setEvents([]);
    setModelCalls([]);
    setRuntimeErrors([]);
    setReplay(null);
  }, []);

  const loadRunEvidence = useCallback(async (runId: string) => {
    const [detail, timeline, calls, errors, nextReplay] = await Promise.all([
      getRunDetail(runId),
      getRunEvents(runId),
      getModelCalls(runId),
      getRuntimeErrors(runId),
      getRunReplay(runId)
    ]);
    latestEventIdByRunRef.current[runId] = maxEventId(timeline);
    setRunDetail(detail);
    setEvents(timeline);
    setModelCalls(calls);
    setRuntimeErrors(errors);
    setReplay(nextReplay);
    setLastUpdatedAt(new Date());
  }, []);

  const loadAgent = useCallback(
    async (agentKey: string, preferredRunId?: string | null) => {
      const detail = await getAgentDetail(agentKey);
      const nextRunId =
        preferredRunId && detail.runs.some((run) => run.run_id === preferredRunId)
          ? preferredRunId
          : detail.agent.latest_run_id ?? detail.runs[0]?.run_id ?? null;
      setAgentDetail(detail);
      setSelectedRunId(nextRunId);
      if (nextRunId) {
        await loadRunEvidence(nextRunId);
      } else {
        clearSelectedRunEvidence();
      }
    },
    [clearSelectedRunEvidence, loadRunEvidence]
  );

  const refreshIndex = useCallback(async () => {
    setLoading(true);
    const loadErrors: string[] = [];
    try {
      const [
        agentsResult,
        skillsResult,
        comparisonResult,
        creativeResult,
        reviewsResult,
        jobsResult
      ] = await Promise.allSettled([
        getAgents(),
        getSkills(),
        getBenchmarkComparison(),
        getCreativeEvaluations(),
        getHumanReviews(),
        getLaunchJobs()
      ]);

      void getEvaluationReports()
        .then((reports) => {
          setEvaluationReports(reports);
        })
        .catch((caught: unknown) => {
          setError((current) =>
            [current, errorMessage("evaluation reports", caught)].filter(Boolean).join(" · ")
          );
        });

      if (skillsResult.status === "fulfilled") {
        setSkills(skillsResult.value);
      } else {
        loadErrors.push(errorMessage("skills", skillsResult.reason));
      }
      if (comparisonResult.status === "fulfilled") {
        setComparison(comparisonResult.value);
      } else {
        loadErrors.push(errorMessage("legacy benchmark", comparisonResult.reason));
      }
      if (creativeResult.status === "fulfilled") {
        setCreativeEvaluations(creativeResult.value);
      } else {
        loadErrors.push(errorMessage("creative evaluations", creativeResult.reason));
      }
      if (reviewsResult.status === "fulfilled") {
        setHumanReviews(reviewsResult.value);
      } else {
        loadErrors.push(errorMessage("human reviews", reviewsResult.reason));
      }
      if (jobsResult.status === "fulfilled") {
        setLaunchJobs(jobsResult.value);
      } else {
        loadErrors.push(errorMessage("launch jobs", jobsResult.reason));
      }

      if (agentsResult.status === "fulfilled") {
        const nextAgents = agentsResult.value;
        const nextAgentKey =
          selectedAgentKey && nextAgents.some((agent) => agent.key === selectedAgentKey)
            ? selectedAgentKey
            : nextAgents[0]?.key ?? null;
        setAgents(nextAgents);
        setSelectedAgentKey(nextAgentKey);
        if (nextAgentKey) {
          try {
            await loadAgent(nextAgentKey, selectedRunId);
          } catch (caught) {
            loadErrors.push(errorMessage("selected agent", caught));
          }
        } else {
          setAgentDetail(null);
          clearSelectedRunEvidence();
        }
      } else {
        loadErrors.push(errorMessage("agents", agentsResult.reason));
      }

      setLastUpdatedAt(new Date());
      setError(loadErrors.length ? loadErrors.join(" · ") : null);
    } finally {
      setLoading(false);
    }
  }, [clearSelectedRunEvidence, loadAgent, selectedAgentKey, selectedRunId]);

  const refreshLaunchJobs = useCallback(async () => {
    try {
      setLaunchJobs(await getLaunchJobs());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  const handleLaunchJobsChanged = useCallback(() => {
    void refreshLaunchJobs();
  }, [refreshLaunchJobs]);

  const navigate = useCallback((section: MainSection) => {
    const nextHash = section === "home" ? "#/" : `#/${section}`;
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash;
    }
    setMainSection(section);
  }, []);

  const refreshSelectedRunArtifacts = useCallback(
    async (runId: string) => {
      const [nextAgents, detail, calls, errors, nextReplay] = await Promise.all([
        getAgents(),
        getRunDetail(runId),
        getModelCalls(runId),
        getRuntimeErrors(runId),
        getRunReplay(runId)
      ]);
      setAgents(nextAgents);
      setRunDetail(detail);
      setModelCalls(calls);
      setRuntimeErrors(errors);
      setReplay(nextReplay);
      if (selectedAgentKey) {
        const nextAgentDetail = await getAgentDetail(selectedAgentKey);
        setAgentDetail(nextAgentDetail);
      }
      setLastUpdatedAt(new Date());
      setError(null);
    },
    [selectedAgentKey]
  );

  const scheduleSelectedRunSync = useCallback(
    (runId: string) => {
      if (syncTimerRef.current !== null) {
        window.clearTimeout(syncTimerRef.current);
      }
      syncTimerRef.current = window.setTimeout(() => {
        syncTimerRef.current = null;
        void refreshSelectedRunArtifacts(runId).catch((caught) => {
          setError(caught instanceof Error ? caught.message : String(caught));
        });
      }, 350);
    },
    [refreshSelectedRunArtifacts]
  );

  const selectAgent = useCallback(
    (agentKey: string) => {
      setSelectedAgentKey(agentKey);
      setAgentPanel("overview");
      void loadAgent(agentKey).catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
    },
    [loadAgent]
  );

  const selectRun = useCallback(
    (runId: string) => {
      setSelectedRunId(runId);
      setAgentPanel("run");
      setRunEvidenceMode("core");
      void loadRunEvidence(runId).catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
    },
    [loadRunEvidence]
  );

  useEffect(() => {
    refreshIndexRef.current = refreshIndex;
  }, [refreshIndex]);

  useEffect(() => {
    void refreshIndexRef.current?.();
  }, []);

  useEffect(() => {
    const handleHashChange = () => setMainSection(sectionFromHash(window.location.hash));
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      void refreshIndexRef.current?.();
    }, 15000);
    return () => window.clearInterval(intervalId);
  }, []);

  useEffect(() => {
    if (!selectedRunId) {
      setStreamStatus("idle");
      return;
    }
    const eventSource = openRunEventStream(selectedRunId, latestEventIdByRunRef.current[selectedRunId] ?? 0);
    setStreamStatus("connecting");
    eventSource.onopen = () => setStreamStatus("live");
    eventSource.onerror = () => setStreamStatus("reconnecting");
    const handleTrajectory = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as TrajectoryEvent;
        const latestEventId = latestEventIdByRunRef.current[event.run_id] ?? 0;
        if (event.id <= latestEventId) {
          return;
        }
        latestEventIdByRunRef.current[event.run_id] = event.id;
        setEvents((current) => mergeTrajectoryEvents(current, [event]));
        setLastUpdatedAt(new Date());
        scheduleSelectedRunSync(event.run_id);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    };
    const handleHeartbeat = () => setStreamStatus("live");
    eventSource.addEventListener("trajectory", handleTrajectory as EventListener);
    eventSource.addEventListener("heartbeat", handleHeartbeat as EventListener);
    return () => {
      eventSource.close();
      setStreamStatus("idle");
    };
  }, [scheduleSelectedRunSync, selectedRunId]);

  useEffect(() => {
    return () => {
      if (syncTimerRef.current !== null) {
        window.clearTimeout(syncTimerRef.current);
      }
    };
  }, []);

  const summary = useMemo(
    () => buildSummary(agents, skills, humanReviews),
    [agents, humanReviews, skills]
  );

  const refreshWorkspace = () => {
    if (mainSection === "knowledge" || mainSection === "configuration") {
      setWorkspaceRefreshToken((current) => current + 1);
      return;
    }
    void refreshIndex();
  };

  return (
    <main className={`audit-shell ${mainSection === "creative" ? "review-shell" : ""}`}>
      <PrimarySidebar
        active={mainSection}
        agents={agents}
        summary={summary}
        streamStatus={streamStatus}
        lastUpdatedAt={lastUpdatedAt}
        onChange={navigate}
      />
      <section className={`workspace ${mainSection === "creative" ? "review-mode" : ""} ${mainSection === "home" ? "portal-mode" : ""}`}>
        {!["home", "creative"].includes(mainSection) ? (
          <WorkspaceHeader
            section={mainSection}
            loading={loading}
            onRefresh={refreshWorkspace}
          />
        ) : null}
        {error ? <Alert message={error} /> : null}
        {mainSection === "home" ? (
          <PortalHome
            agents={agents}
            skills={skills}
            reviews={humanReviews}
            launchJobs={launchJobs}
            onNavigate={navigate}
          />
        ) : null}
        {mainSection === "quick-start" ? (
          <QuickStartWorkspace
            onOpenRuntime={() => navigate("runtime")}
            onJobsChanged={handleLaunchJobsChanged}
          />
        ) : null}
        {mainSection === "runtime" ? (
          <AgentsWorkspace
            agents={agents}
            selectedAgent={selectedAgent}
            selectedAgentKey={selectedAgentKey}
            selectedTask={selectedTask}
            agentDetail={agentDetail}
            agentPanel={agentPanel}
            runEvidenceMode={runEvidenceMode}
            replay={replay}
            events={events}
            modelCalls={modelCalls}
            runtimeErrors={runtimeErrors}
            runDetail={runDetail}
            evidenceOptions={evidenceOptions}
            onSelectAgent={selectAgent}
            onSelectPanel={setAgentPanel}
            onSelectRun={selectRun}
            onSelectRunEvidenceMode={setRunEvidenceMode}
            onToggleEvidenceOption={(key) =>
              setEvidenceOptions((current) => ({ ...current, [key]: !current[key] }))
            }
          />
        ) : null}
        {mainSection === "skills" ? <SkillsWorkspace skills={skills} onChanged={() => void refreshIndex()} /> : null}
        {mainSection === "knowledge" ? (
          <KnowledgeWorkspace refreshToken={workspaceRefreshToken} />
        ) : null}
        {mainSection === "configuration" ? (
          <PromptConfigurationWorkspace refreshToken={workspaceRefreshToken} />
        ) : null}
        {mainSection === "creative" ? (
          <HumanReviewWorkspace
            reviews={humanReviews}
            evaluations={creativeEvaluations}
            onChanged={() => void refreshIndex()}
          />
        ) : null}
        {mainSection === "reports" ? (
          <ReportsWorkspace reports={evaluationReports} comparison={comparison} />
        ) : null}
      </section>
    </main>
  );
}

/** Primary navigation and high-level system summary for the audit workspace. */
function PrimarySidebar(props: {
  active: MainSection;
  agents: AgentSummary[];
  summary: ReturnType<typeof buildSummary>;
  streamStatus: string;
  lastUpdatedAt: Date | null;
  onChange: (section: MainSection) => void;
}) {
  return (
    <aside className="primary-sidebar" aria-label="Primary navigation">
      <div className="brand-block">
        <Bot size={22} aria-hidden="true" />
        <div>
          <strong>Minecraft Agent Harness</strong>
          <span>Audit Console</span>
        </div>
      </div>
      <nav className="primary-nav">
        <NavButton active={props.active === "home"} icon={<Home size={16} />} label="Overview" onClick={() => props.onChange("home")} />
        <NavButton active={props.active === "quick-start"} icon={<Play size={16} />} label="Quick Start" onClick={() => props.onChange("quick-start")} />
        <NavButton active={props.active === "runtime"} icon={<LayoutDashboard size={16} />} label="Runtime" onClick={() => props.onChange("runtime")} />
        <NavButton active={props.active === "skills"} icon={<Library size={16} />} label="Skills" onClick={() => props.onChange("skills")} />
        <NavButton active={props.active === "knowledge"} icon={<Database size={16} />} label="Knowledge" onClick={() => props.onChange("knowledge")} />
        <NavButton active={props.active === "configuration"} icon={<Settings2 size={16} />} label="Configuration" onClick={() => props.onChange("configuration")} />
        <NavButton active={props.active === "creative"} icon={<ClipboardCheck size={16} />} label="Creative" onClick={() => props.onChange("creative")} />
        <NavButton active={props.active === "reports"} icon={<GitCompare size={16} />} label="Reports" onClick={() => props.onChange("reports")} />
      </nav>
      <section className="sidebar-summary" aria-label="System summary">
        <SmallMetric label="Agents" value={props.summary.agentCount} />
        <SmallMetric label="Runs" value={props.summary.runCount} />
        <SmallMetric label="Skills" value={props.summary.skillCount} />
        <SmallMetric label="Pending Review" value={props.summary.pendingReviewCount} />
        <SmallMetric label="Stream" value={props.streamStatus} />
        <SmallMetric label="Updated" value={formatClock(props.lastUpdatedAt)} />
      </section>
      <section className="sidebar-list" aria-label="Recent agents">
        <span>Recent Agents</span>
        {props.agents.slice(0, 5).map((agent) => (
          <div className="mini-agent-row" key={agent.key}>
            <span className={`status-dot ${statusClass(agent.presence)}`} aria-hidden="true" />
            <strong>{agent.display_name}</strong>
          </div>
        ))}
      </section>
    </aside>
  );
}

/** Clickable primary navigation row. */
function NavButton(props: { active: boolean; icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button className={`nav-button ${props.active ? "active" : ""}`} type="button" onClick={props.onClick}>
      {props.icon}
      <span>{props.label}</span>
    </button>
  );
}

/** Top header for the current workspace pane. */
function WorkspaceHeader(props: {
  section: MainSection;
  loading: boolean;
  onRefresh: () => void;
}) {
  const copy = {
    home: {
      title: "Minecraft Agent Harness",
      description: "Launch, inspect, and govern Minecraft agent workflows."
    },
    "quick-start": {
      title: "Quick Start",
      description: "Choose an executable task, validate the local Minecraft environment, and start one audited run."
    },
    runtime: {
      title: "Agent Runtime",
      description: "Inspect agent state, task history, and step-by-step prompt / observation / action evidence."
    },
    skills: {
      title: "Skill Review",
      description: "Review reusable strategy memories, source evidence, lifecycle state, and promotion decisions."
    },
    knowledge: {
      title: "Knowledge Base",
      description: "Manage the versioned local document corpus available to the agent through retrieve_docs."
    },
    configuration: {
      title: "Prompt Configuration",
      description: "Hot-update the system prompt and prompt-facing registry for implemented harness actions."
    },
    creative: {
      title: "Creative Task Review",
      description: "Review final visual evidence and the underlying agent trajectory."
    },
    reports: {
      title: "Evaluation Reports",
      description: "Compare task success, runtime stability, token use, and cost across harness modes."
    }
  }[props.section];
  return (
    <header className="workspace-header">
      <div>
        <h1>{copy.title}</h1>
        <p>{copy.description}</p>
      </div>
      <button className="icon-button labeled" type="button" onClick={props.onRefresh} disabled={props.loading}>
        <RefreshCw size={16} aria-hidden="true" />
        <span>{props.loading ? "Refreshing" : "Refresh"}</span>
      </button>
    </header>
  );
}

/** Alert row for backend or stream errors. */
function Alert(props: { message: string }) {
  return (
    <section className="alert-row" role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <span>{props.message}</span>
    </section>
  );
}

/** Agent audit workspace with agent index, category sidebar, and detail content. */
function AgentsWorkspace(props: {
  agents: AgentSummary[];
  selectedAgent: AgentSummary | null;
  selectedAgentKey: string | null;
  selectedTask: AgentTaskSummary | null;
  agentDetail: AgentDetail | null;
  agentPanel: AgentPanel;
  runEvidenceMode: RunEvidenceMode;
  replay: RunReplay | null;
  events: TrajectoryEvent[];
  modelCalls: ModelCall[];
  runtimeErrors: RuntimeErrorRecord[];
  runDetail: RunDetail | null;
  evidenceOptions: EvidenceOptions;
  onSelectAgent: (agentKey: string) => void;
  onSelectPanel: (panel: AgentPanel) => void;
  onSelectRun: (runId: string) => void;
  onSelectRunEvidenceMode: (mode: RunEvidenceMode) => void;
  onToggleEvidenceOption: (key: keyof EvidenceOptions) => void;
}) {
  return (
    <section className="agent-workspace">
      <aside className="agent-index" aria-label="Agent list">
        <div className="section-heading">
          <h2>Agents</h2>
          <span>{props.agents.length}</span>
        </div>
        <div className="stack-list">
          {props.agents.length ? (
            props.agents.map((agent) => (
              <AgentRow
                key={agent.key}
                agent={agent}
                selected={agent.key === props.selectedAgentKey}
                onClick={() => props.onSelectAgent(agent.key)}
              />
            ))
          ) : (
            <EmptyState label="No agents" />
          )}
        </div>
      </aside>
      <section className="agent-page">
        {props.agentDetail && props.selectedAgent ? (
          <>
            <AgentHero agent={props.agentDetail.agent} />
            <div className="agent-detail-grid">
              <AuditCategorySidebar
                active={props.agentPanel}
                selectedTask={props.selectedTask}
                onSelect={props.onSelectPanel}
              />
              <section className="agent-content">
                {props.agentPanel === "overview" ? (
                  <AgentOverview detail={props.agentDetail} onSelectRun={props.onSelectRun} />
                ) : null}
                {props.agentPanel === "tasks" ? (
                  <TaskHistory runs={props.agentDetail.runs} selectedRunId={props.selectedTask?.run_id ?? null} onSelectRun={props.onSelectRun} />
                ) : null}
                {props.agentPanel === "run" ? (
                  <RunAuditDetail
                    task={props.selectedTask}
                    runDetail={props.runDetail}
                    replay={props.replay}
                    events={props.events}
                    modelCalls={props.modelCalls}
                    runtimeErrors={props.runtimeErrors}
                    evidenceMode={props.runEvidenceMode}
                    options={props.evidenceOptions}
                    onSelectMode={props.onSelectRunEvidenceMode}
                    onToggleOption={props.onToggleEvidenceOption}
                  />
                ) : null}
                {props.agentPanel === "timeline" ? <Timeline events={props.events} /> : null}
              </section>
            </div>
          </>
        ) : (
          <EmptyState label="Select an agent" />
        )}
      </section>
    </section>
  );
}

/** Compact row inside the agent index. */
function AgentRow(props: { agent: AgentSummary; selected: boolean; onClick: () => void }) {
  return (
    <button className={`agent-row ${props.selected ? "selected" : ""}`} type="button" onClick={props.onClick}>
      <span className={`status-dot ${statusClass(props.agent.presence)}`} aria-hidden="true" />
      <span>
        <strong>{props.agent.display_name}</strong>
        <small>{props.agent.latest_task_id ?? "no task"} · {props.agent.latest_task_result}</small>
      </span>
      <ChevronRight size={15} aria-hidden="true" />
    </button>
  );
}

/** Header summary for one selected agent. */
function AgentHero(props: { agent: AgentSummary }) {
  return (
    <header className="agent-hero">
      <div>
        <span className={`status-badge ${statusClass(props.agent.presence)}`}>
          <Radio size={13} aria-hidden="true" />
          {props.agent.presence}
        </span>
        <h2>{props.agent.display_name}</h2>
        <p>{props.agent.key}</p>
      </div>
      <div className="fact-grid">
        <Fact label="Runs" value={props.agent.run_count} />
        <Fact label="Task Result" value={`${props.agent.task_success_count}/${props.agent.task_success_count + props.agent.task_failure_count}`} />
        <Fact label="Skills" value={`${props.agent.promoted_skill_count}/${props.agent.skill_count}`} />
        <Fact label="Errors" value={props.agent.runtime_error_count} />
        <Fact label="Tokens" value={formatUsageNumber(props.agent.token_totals.total_tokens ?? 0)} />
      </div>
    </header>
  );
}

/** Secondary category sidebar for the selected agent. */
function AuditCategorySidebar(props: {
  active: AgentPanel;
  selectedTask: AgentTaskSummary | null;
  onSelect: (panel: AgentPanel) => void;
}) {
  return (
    <aside className="audit-category-sidebar" aria-label="Agent audit categories">
      <CategoryButton active={props.active === "overview"} icon={<Activity size={15} />} label="Overview" onClick={() => props.onSelect("overview")} />
      <CategoryButton active={props.active === "tasks"} icon={<ListChecks size={15} />} label="Task History" onClick={() => props.onSelect("tasks")} />
      <CategoryButton active={props.active === "run"} icon={<MessageSquareText size={15} />} label="Run Detail" onClick={() => props.onSelect("run")} />
      <CategoryButton active={props.active === "timeline"} icon={<ScrollText size={15} />} label="Raw Timeline" onClick={() => props.onSelect("timeline")} />
      {props.selectedTask ? (
        <div className="selected-task-note">
          <span>Selected Run</span>
          <strong>{props.selectedTask.task_id}</strong>
        </div>
      ) : null}
    </aside>
  );
}

/** Button inside the secondary audit category sidebar. */
function CategoryButton(props: { active: boolean; icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button className={`category-button ${props.active ? "active" : ""}`} type="button" onClick={props.onClick}>
      {props.icon}
      <span>{props.label}</span>
    </button>
  );
}

/** Agent overview section with status metrics, task summary, and skill inventory. */
function AgentOverview(props: { detail: AgentDetail; onSelectRun: (runId: string) => void }) {
  return (
    <div className="content-stack">
      <section className="overview-grid">
        <AuditMetric icon={<Route size={17} />} label="Tasks" value={props.detail.agent.run_count} detail={`${props.detail.agent.active_run_count} active`} />
        <AuditMetric icon={<CheckCircle2 size={17} />} label="Task Results" value={props.detail.agent.task_success_count} detail={`${props.detail.agent.task_failure_count} verifier failed`} />
        <AuditMetric icon={<Activity size={17} />} label="Lifecycle" value={props.detail.agent.completed_run_count} detail={`${props.detail.agent.failed_run_count} run failed`} />
        <AuditMetric icon={<Library size={17} />} label="Skill Library" value={props.detail.agent.skill_count} detail={`${props.detail.agent.promoted_skill_count} promoted`} />
        <AuditMetric icon={<TerminalSquare size={17} />} label="Tokens" value={formatUsageNumber(props.detail.agent.token_totals.total_tokens ?? 0)} detail="all runs" />
      </section>
      <section className="panel-section">
        <SectionTitle icon={<ListChecks size={16} />} title="Recent Tasks" meta={`${props.detail.runs.length} runs`} />
        <TaskHistory runs={props.detail.runs.slice(0, 5)} selectedRunId={null} onSelectRun={props.onSelectRun} />
      </section>
      <section className="panel-section">
        <SectionTitle icon={<Library size={16} />} title="Skill Inventory" meta={`${props.detail.skills.length} skills`} />
        <SkillCompactList skills={props.detail.skills} />
      </section>
    </div>
  );
}

/** Reusable metric card for overview sections. */
function AuditMetric(props: { icon: ReactNode; label: string; value: string | number; detail: string }) {
  return (
    <article className="audit-metric">
      <span>{props.icon}</span>
      <div>
        <small>{props.label}</small>
        <strong>{props.value}</strong>
        <p>{props.detail}</p>
      </div>
    </article>
  );
}

/** Section title with optional metadata on the right. */
function SectionTitle(props: { icon: ReactNode; title: string; meta?: string }) {
  return (
    <header className="section-title">
      <div>
        {props.icon}
        <strong>{props.title}</strong>
      </div>
      {props.meta ? <span>{props.meta}</span> : null}
    </header>
  );
}

/** Task history list for the selected agent. */
function TaskHistory(props: {
  runs: AgentTaskSummary[];
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}) {
  if (!props.runs.length) {
    return <EmptyState label="No task history" />;
  }
  return (
    <div className="task-list">
      {props.runs.map((run) => (
        <button
          className={`task-row ${run.run_id === props.selectedRunId ? "selected" : ""}`}
          key={run.run_id}
          type="button"
          onClick={() => props.onSelectRun(run.run_id)}
        >
          <span className={`status-dot ${statusClass(run.task_result)}`} aria-hidden="true" />
          <span className="task-main">
            <strong>{run.task_id}</strong>
            <small>{run.run_id}</small>
          </span>
          <span className={`status-badge ${statusClass(run.task_result)}`}>{run.task_result}</span>
          <span className="task-meta">{run.step_count} steps</span>
          <span className="task-meta">{formatUsageNumber(run.token_totals.total_tokens ?? 0)} tokens</span>
        </button>
      ))}
    </div>
  );
}

/** Run detail page with categorized evidence modes. */
function RunAuditDetail(props: {
  task: AgentTaskSummary | null;
  runDetail: RunDetail | null;
  replay: RunReplay | null;
  events: TrajectoryEvent[];
  modelCalls: ModelCall[];
  runtimeErrors: RuntimeErrorRecord[];
  evidenceMode: RunEvidenceMode;
  options: EvidenceOptions;
  onSelectMode: (mode: RunEvidenceMode) => void;
  onToggleOption: (key: keyof EvidenceOptions) => void;
}) {
  if (!props.task) {
    return <EmptyState label="Select a task run" />;
  }
  return (
    <div className="content-stack">
      <RunTitle task={props.task} detail={props.runDetail} />
      <EvidenceModeNav active={props.evidenceMode} onSelect={props.onSelectMode} />
      {props.evidenceMode === "core" ? (
        <CoreTrace replay={props.replay} options={props.options} onToggleOption={props.onToggleOption} />
      ) : null}
      {props.evidenceMode === "metrics" ? <RunMetrics task={props.task} modelCalls={props.modelCalls} /> : null}
      {props.evidenceMode === "runtime" ? <RuntimeEvidence task={props.task} errors={props.runtimeErrors} events={props.events} /> : null}
      {props.evidenceMode === "raw" ? <RawEvidence replay={props.replay} events={props.events} /> : null}
    </div>
  );
}

/** Header for a selected run detail view. */
function RunTitle(props: { task: AgentTaskSummary; detail: RunDetail | null }) {
  return (
    <header className="run-title">
      <div>
        <span className={`status-badge ${statusClass(props.task.task_result)}`}>task {props.task.task_result}</span>
        <h3>{props.task.task_id}</h3>
        <p>{props.task.run_id}</p>
        {props.detail ? (
          <div className="trace-identity">
            <span>Trace</span>
            <code title={props.detail.trace_id}>{props.detail.trace_id}</code>
            <span>Root span</span>
            <code title={props.detail.root_span_id}>{props.detail.root_span_id}</code>
          </div>
        ) : null}
      </div>
      <div className="fact-grid compact">
        <Fact label="Lifecycle" value={props.task.lifecycle_status} />
        <Fact label="Steps" value={props.task.step_count} />
        <Fact label="Events" value={props.task.event_count} />
        <Fact label="Errors" value={props.task.runtime_error_count} />
        <Fact label="Checkpoint" value={props.detail?.resumed_from_checkpoint_id ?? "none"} />
      </div>
    </header>
  );
}

/** Horizontal mode navigation inside a run detail page. */
function EvidenceModeNav(props: { active: RunEvidenceMode; onSelect: (mode: RunEvidenceMode) => void }) {
  return (
    <div className="evidence-mode-nav">
      <SegmentButton active={props.active === "core"} icon={<MessageSquareText size={15} />} label="Core Trace" onClick={() => props.onSelect("core")} />
      <SegmentButton active={props.active === "metrics"} icon={<TerminalSquare size={15} />} label="Model Metrics" onClick={() => props.onSelect("metrics")} />
      <SegmentButton active={props.active === "runtime"} icon={<ShieldCheck size={15} />} label="Runtime" onClick={() => props.onSelect("runtime")} />
      <SegmentButton active={props.active === "raw"} icon={<FileSearch size={15} />} label="Raw Evidence" onClick={() => props.onSelect("raw")} />
    </div>
  );
}

/** Compact segmented control button. */
function SegmentButton(props: { active: boolean; icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button className={`segment-button ${props.active ? "active" : ""}`} type="button" onClick={props.onClick}>
      {props.icon}
      <span>{props.label}</span>
    </button>
  );
}

/** Step-by-step core trace centered on prompt, observation, and action. */
function CoreTrace(props: {
  replay: RunReplay | null;
  options: EvidenceOptions;
  onToggleOption: (key: keyof EvidenceOptions) => void;
}) {
  if (!props.replay || !props.replay.steps.length) {
    return <EmptyState label="No step evidence" />;
  }
  return (
    <section className="core-trace">
      <div className="trace-toolbar">
        <strong>Step Evidence</strong>
        <div className="toggle-row">
          <Toggle checked={props.options.showTokens} label="Tokens" onClick={() => props.onToggleOption("showTokens")} />
          <Toggle checked={props.options.showRequestMeta} label="Request Meta" onClick={() => props.onToggleOption("showRequestMeta")} />
          <Toggle checked={props.options.showRawJson} label="Raw JSON" onClick={() => props.onToggleOption("showRawJson")} />
        </div>
      </div>
      <div className="round-list">
        {props.replay.steps.map((step) => (
          <RoundCard key={step.step_index} step={step} options={props.options} />
        ))}
      </div>
    </section>
  );
}

/** Small toggle used for optional audit evidence fields. */
function Toggle(props: { checked: boolean; label: string; onClick: () => void }) {
  return (
    <button className={`toggle-button ${props.checked ? "checked" : ""}`} type="button" onClick={props.onClick}>
      <span aria-hidden="true" />
      {props.label}
    </button>
  );
}

/** One execution round showing prompt, observation, and action as the primary evidence. */
function RoundCard(props: { step: RunReplay["steps"][number]; options: EvidenceOptions }) {
  const modelCall = props.step.model_calls[0] ?? null;
  const decision = decisionEnvelope(props.step);
  return (
    <details className="round-card">
      <summary className="round-summary">
        <div className="round-summary-title">
          <ChevronRight className="round-toggle-icon" size={17} aria-hidden="true" />
          <span className={`status-badge ${statusClass(props.step.status)}`}>{props.step.status}</span>
          <h4>Round {props.step.step_index}</h4>
          <code
            className="round-span-id"
            title={`trace ${props.step.trace_id} · parent ${props.step.parent_span_id}`}
          >
            span {props.step.span_id}
          </code>
        </div>
        <div className="tag-row">
          {props.step.highlights.slice(0, 4).map((highlight) => (
            <span key={highlight}>{highlight}</span>
          ))}
        </div>
      </summary>
      <div className="round-card-body">
        <div className="round-evidence-grid">
          <EvidenceColumn collapsible title="Prompt" value={formatPrompt(props.step.context)} summary={summarizePrompt(props.step.context)} />
          <EvidenceColumn collapsible title="Observation" value={formatJson(props.step.observation ?? {})} summary={summarizeObservation(props.step.observation)} />
          <EvidenceColumn collapsible title="Decision" value={formatJson(decision)} summary={summarizeDecision(decision)} />
          <EvidenceColumn collapsible title="Action" value={formatJson(props.step.parsed_action ?? {})} summary={summarizeAction(props.step.parsed_action)} />
        </div>
        {props.options.showTokens || props.options.showRequestMeta || props.options.showRawJson ? (
          <div className="optional-evidence">
            {props.options.showTokens ? <EvidenceColumn collapsible title="Token Usage" value={formatJson(modelCall?.usage ?? {})} summary="model usage for this round" /> : null}
            {props.options.showRequestMeta ? <EvidenceColumn collapsible title="Request Metadata" value={formatJson(requestMetadata(modelCall))} summary="provider and request metadata" /> : null}
            {props.options.showRawJson ? <EvidenceColumn collapsible title="Raw Step Events" value={formatJson(props.step.raw_events)} summary={`${props.step.raw_events.length} persisted events`} /> : null}
          </div>
        ) : null}
      </div>
    </details>
  );
}

/** One prompt, observation, action, or optional evidence column. */
function EvidenceColumn(props: { title: string; value: string; summary?: string; collapsible?: boolean }) {
  if (props.collapsible) {
    return (
      <details className="evidence-column collapsible">
        <summary>
          <span>
            <strong>{props.title}</strong>
            <small>{props.summary ?? "Click to inspect full evidence"}</small>
          </span>
          <ChevronRight className="evidence-toggle-icon" size={15} aria-hidden="true" />
        </summary>
        <pre>{props.value}</pre>
      </details>
    );
  }
  return (
    <section className="evidence-column">
      <header>
        <strong>{props.title}</strong>
        {props.summary ? <span>{props.summary}</span> : null}
      </header>
      <pre>{props.value}</pre>
    </section>
  );
}

/** Model metrics view with token usage and request metadata. */
function RunMetrics(props: { task: AgentTaskSummary; modelCalls: ModelCall[] }) {
  return (
    <section className="panel-section">
      <SectionTitle icon={<TerminalSquare size={16} />} title="Model Calls" meta={`${props.modelCalls.length} calls`} />
      <div className="metrics-grid">
        <AuditMetric icon={<Boxes size={17} />} label="Total Tokens" value={formatUsageNumber(props.task.token_totals.total_tokens ?? 0)} detail="task total" />
        <AuditMetric icon={<MessageSquareText size={17} />} label="Input Tokens" value={formatUsageNumber(props.task.token_totals.input_tokens ?? 0)} detail="prompt side" />
        <AuditMetric icon={<TerminalSquare size={17} />} label="Output Tokens" value={formatUsageNumber(props.task.token_totals.output_tokens ?? 0)} detail="model output" />
      </div>
      <div className="audit-table">
        {props.modelCalls.map((call) => (
          <article className="audit-row" key={call.id}>
            <header>
              <strong>Step {call.step_index}</strong>
              <span>{call.source}</span>
            </header>
            <p>{call.raw_content || "empty output"}</p>
            <pre>{formatJson({ usage: call.usage, action: call.action, raw_response: call.raw_response })}</pre>
          </article>
        ))}
      </div>
    </section>
  );
}

/** Runtime and verifier evidence view for one selected run. */
function RuntimeEvidence(props: { task: AgentTaskSummary; errors: RuntimeErrorRecord[]; events: TrajectoryEvent[] }) {
  const resetEvents = props.events.filter((event) => event.event_type === "environment_reset");
  const reachabilityEvents = props.events.filter((event) => event.event_type === "reachability_analysis");
  const planEvents = props.events.filter((event) =>
    event.event_type === "agent_plan_created" || event.event_type === "agent_plan_revised"
  );
  return (
    <div className="content-stack">
      <section className="panel-section">
        <SectionTitle icon={<ShieldCheck size={16} />} title="Verifier" meta={verifierLabel(props.task.verifier)} />
        <pre className="block-json">{formatJson(props.task.verifier ?? {})}</pre>
      </section>
      <section className="panel-section">
        <SectionTitle icon={<ScrollText size={16} />} title="Agent Plan" meta={`${planEvents.length} events`} />
        {planEvents.length ? <PlanEvents events={planEvents} /> : <EmptyState label="No agent plan events" />}
      </section>
      <section className="panel-section">
        <SectionTitle icon={<Route size={16} />} title="Reachability Analysis" meta={`${reachabilityEvents.length} events`} />
        {reachabilityEvents.length ? <ReachabilityEvents events={reachabilityEvents} /> : <EmptyState label="No reachability analysis" />}
      </section>
      <section className="panel-section">
        <SectionTitle icon={<Settings2 size={16} />} title="Environment Reset" meta={`${resetEvents.length} events`} />
        <pre className="block-json">{formatJson(resetEvents.map((event) => event.payload))}</pre>
      </section>
      <section className="panel-section">
        <SectionTitle icon={<AlertTriangle size={16} />} title="Runtime Errors" meta={`${props.errors.length} errors`} />
        {props.errors.length ? <RuntimeErrors errors={props.errors} /> : <EmptyState label="No runtime errors" />}
      </section>
    </div>
  );
}

/** Focused list of agent-created task plans and revisions. */
function PlanEvents(props: { events: TrajectoryEvent[] }) {
  return (
    <div className="audit-table">
      {props.events.map((event) => {
        const plan = recordValue(event.payload.plan);
        return (
          <article className="audit-row" key={event.id}>
            <header>
              <strong>{event.event_type === "agent_plan_created" ? "Initial Plan" : "Plan Revision"}</strong>
              <span>step {stringValue(event.payload.step_index)}</span>
            </header>
            <div className="tag-row">
              <span>phase {stringValue(plan.current_phase)}</span>
              <span>revision {stringValue(plan.revision)}</span>
              <span>source {stringValue(plan.source)}</span>
            </div>
            {typeof plan.high_level_strategy === "string" ? <p>{plan.high_level_strategy}</p> : null}
            <pre>{formatJson(event.payload)}</pre>
          </article>
        );
      })}
    </div>
  );
}

/** Focused list of pathfinder reachability diagnostics emitted by move_to. */
function ReachabilityEvents(props: { events: TrajectoryEvent[] }) {
  return (
    <div className="audit-table">
      {props.events.map((event) => {
        const payload = event.payload;
        const pathSummary = recordValue(payload.path_summary);
        return (
          <article className="audit-row" key={event.id}>
            <header>
              <strong>Step {stringValue(payload.step_index)}</strong>
              <span>{stringValue(payload.error_code ?? (payload.ok === true ? "reachable" : "unknown"))}</span>
            </header>
            <div className="tag-row">
              <span>target {compactPosition(payload.target)}</span>
              <span>nearest {compactPosition(payload.nearest_reachable_position)}</span>
              <span>height {stringValue(payload.target_height_delta)}</span>
              <span>path {stringValue(pathSummary.status)}</span>
              <span>visited {stringValue(pathSummary.visited_nodes)}</span>
            </div>
            {typeof payload.state_summary === "string" ? <p>{payload.state_summary}</p> : null}
            <pre>{formatJson(payload)}</pre>
          </article>
        );
      })}
    </div>
  );
}

/** Raw evidence view for users who need full trajectory records. */
function RawEvidence(props: { replay: RunReplay | null; events: TrajectoryEvent[] }) {
  return (
    <div className="content-stack">
      <section className="panel-section">
        <SectionTitle icon={<FileSearch size={16} />} title="Run Replay JSON" />
        <pre className="block-json">{formatJson(props.replay ?? {})}</pre>
      </section>
      <section className="panel-section">
        <SectionTitle icon={<ScrollText size={16} />} title="Trajectory Events" meta={`${props.events.length} events`} />
        <Timeline events={props.events} />
      </section>
    </div>
  );
}

/** Timeline list for persisted trajectory events. */
function Timeline(props: { events: TrajectoryEvent[] }) {
  if (!props.events.length) {
    return <EmptyState label="No trajectory events" />;
  }
  return (
    <div className="audit-table">
      {props.events.map((event) => (
        <article className="audit-row" key={event.id}>
          <header>
            <strong>{event.event_type}</strong>
            <span>#{event.id}</span>
          </header>
          <small>{formatDate(event.created_at)}</small>
          <pre>{formatJson(event.payload)}</pre>
        </article>
      ))}
    </div>
  );
}

/** Runtime error list for worker failures and recoverable errors. */
function RuntimeErrors(props: { errors: RuntimeErrorRecord[] }) {
  return (
    <div className="audit-table">
      {props.errors.map((error) => (
        <article className="audit-row error" key={error.id}>
          <header>
            <strong>{error.error_type}</strong>
            <span>{error.step_index === null ? "run" : `step ${error.step_index}`}</span>
          </header>
          <p>{error.message}</p>
          <pre>{formatJson(error.payload)}</pre>
        </article>
      ))}
    </div>
  );
}

/** Compact skill inventory inside the agent overview. */
function SkillCompactList(props: { skills: SkillSummary[] }) {
  if (!props.skills.length) {
    return <EmptyState label="No skills produced by this agent" />;
  }
  return (
    <div className="skill-compact-list">
      {props.skills.map((skill) => (
        <article className="skill-compact-row" key={`${skill.id}-${skill.status}`}>
          <div>
            <strong>{skill.name}</strong>
            <small>v{skill.version} / {skill.action_count} actions</small>
          </div>
          <span className={`status-badge ${statusClass(skill.status)}`}>{skill.status}</span>
        </article>
      ))}
    </div>
  );
}

/** Full skill review workspace. */
function SkillsWorkspace(props: { skills: SkillSummary[]; onChanged: () => void }) {
  return (
    <section className="single-workspace">
      <SectionTitle icon={<Library size={16} />} title="Skill Review" meta={`${props.skills.length} skills`} />
      <div className="skill-review-grid">
        {props.skills.length ? (
          props.skills.map((skill) => <SkillRow key={`${skill.id}-${skill.status}`} skill={skill} onChanged={props.onChanged} />)
        ) : (
          <EmptyState label="No skills yet" />
        )}
      </div>
    </section>
  );
}

/** One skill review row with lifecycle, edit, and permanent-delete controls. */
function SkillRow(props: { skill: SkillSummary; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editorSpec, setEditorSpec] = useState<Record<string, unknown> | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setEditing(false);
    setEditorSpec(null);
    setConfirmingDelete(false);
    setLocalError(null);
  }, [props.skill.id]);

  const loadDetail = async () => {
    if (detail || detailLoading) {
      return;
    }
    setDetailLoading(true);
    setLocalError(null);
    try {
      setDetail(await getSkillDetail(props.skill.id));
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setDetailLoading(false);
    }
  };

  const review = async (kind: "promote" | "deprecate") => {
    const expectedUpdatedAt = detail?.updated_at ?? props.skill.updated_at;
    if (!expectedUpdatedAt) {
      setLocalError("This skill has no update timestamp. Refresh Skill Review before changing its status.");
      return;
    }
    setBusy(true);
    setLocalError(null);
    try {
      let changed: SkillDetail;
      if (kind === "promote") {
        changed = await promoteSkill(props.skill.id, expectedUpdatedAt);
      } else {
        changed = await deprecateSkill(props.skill.id, "dashboard review", expectedUpdatedAt);
      }
      setDetail(changed);
      props.onChanged();
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const startEditing = async () => {
    setBusy(true);
    setLocalError(null);
    try {
      const current = await getSkillDetail(props.skill.id);
      setDetail(current);
      setEditorSpec(current.spec);
      setConfirmingDelete(false);
      setEditing(true);
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async (spec: Record<string, unknown>) => {
    const expectedUpdatedAt = detail?.updated_at ?? props.skill.updated_at;
    if (!expectedUpdatedAt) {
      setLocalError("This skill has no update timestamp. Refresh Skill Review before saving.");
      return;
    }
    setBusy(true);
    setLocalError(null);
    try {
      const changed = await updateSkill(
        props.skill.id,
        spec,
        expectedUpdatedAt
      );
      setDetail(changed);
      setEditorSpec(changed.spec);
      setEditing(false);
      props.onChanged();
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const deleteFromActiveLibrary = async () => {
    const expectedUpdatedAt = detail?.updated_at ?? props.skill.updated_at;
    if (!expectedUpdatedAt) {
      setLocalError("This skill has no update timestamp. Refresh Skill Review before deleting it.");
      return;
    }
    setBusy(true);
    setLocalError(null);
    try {
      await deleteSkill(
        props.skill.id,
        expectedUpdatedAt,
        "removed from the active Skill Library by a dashboard operator"
      );
      setConfirmingDelete(false);
      props.onChanged();
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="skill-row">
      <header>
        <div>
          <strong>{props.skill.name}</strong>
          <small>v{props.skill.version}</small>
        </div>
        <span className={`status-badge ${statusClass(props.skill.status)}`}>{props.skill.status}</span>
      </header>
      <p>{props.skill.description}</p>
      <div className="tag-row">
        {props.skill.triggers.slice(0, 4).map((trigger) => (
          <span key={trigger}>{trigger}</span>
        ))}
      </div>
      <div className="skill-source-summary">
        <span>Source Run</span>
        <strong>{props.skill.source_run_id ?? "No source trajectory"}</strong>
      </div>
      {editing ? (
        <SkillSpecEditor
          busy={busy}
          initialSpec={editorSpec ?? detail?.spec ?? {}}
          skillId={props.skill.id}
          onSave={(spec) => void saveEdit(spec)}
          onCancel={() => {
            setEditing(false);
            setLocalError(null);
          }}
        />
      ) : null}
      <details
        className="skill-detail-disclosure"
        onToggle={(event) => {
          if (event.currentTarget.open) {
            void loadDetail();
          }
        }}
      >
        <summary>
          <FileSearch size={15} aria-hidden="true" />
          Full Spec & Source Evidence
        </summary>
        {detailLoading ? <EmptyState label="Loading skill specification" /> : null}
        {detail ? <SkillDetailPanel detail={detail} /> : null}
      </details>
      {localError ? <Alert message={localError} /> : null}
      {confirmingDelete ? (
        <section
          className="skill-delete-confirmation"
          role="alertdialog"
          aria-labelledby={`skill-delete-title-${props.skill.id}`}
          aria-describedby={`skill-delete-description-${props.skill.id}`}
        >
          <AlertTriangle size={18} aria-hidden="true" />
          <div>
            <strong id={`skill-delete-title-${props.skill.id}`}>Remove this skill from the active library?</strong>
            <p id={`skill-delete-description-${props.skill.id}`}>
              This removes the Skill from the active Skill Library and Skill Review. Its source run and a minimal
              deletion tombstone remain for audit; Deprecate keeps the full Skill visible. Deletion applies to the
              next Skill snapshot, while running batches keep their pinned copy.
            </p>
          </div>
          <div className="skill-delete-actions">
            <button
              className="icon-button labeled danger"
              type="button"
              disabled={busy}
              onClick={() => void deleteFromActiveLibrary()}
            >
              <Trash2 size={15} aria-hidden="true" />
              Delete permanently
            </button>
            <button
              className="icon-button labeled"
              type="button"
              disabled={busy}
              onClick={() => setConfirmingDelete(false)}
            >
              Cancel
            </button>
          </div>
        </section>
      ) : null}
      <footer>
        <span>{props.skill.action_count} actions</span>
        <div className="button-row">
          <button
            className="icon-button"
            type="button"
            disabled={busy}
            onClick={() => void startEditing()}
            title="Edit canonical SkillSpec"
            aria-label={`Edit ${props.skill.name}`}
          >
            <Pencil size={15} aria-hidden="true" />
          </button>
          <button className="icon-button" type="button" disabled={busy} onClick={() => void review("promote")} title="Promote skill">
            <CheckCircle2 size={15} aria-hidden="true" />
          </button>
          <button className="icon-button danger" type="button" disabled={busy} onClick={() => void review("deprecate")} title="Deprecate skill">
            <XCircle size={15} aria-hidden="true" />
          </button>
          <button
            className="icon-button danger"
            type="button"
            disabled={busy}
            onClick={() => {
              setEditing(false);
              setConfirmingDelete(true);
              setLocalError(null);
            }}
            title="Remove skill from active library"
            aria-label={`Remove ${props.skill.name} from the active library`}
          >
            <Trash2 size={15} aria-hidden="true" />
          </button>
        </div>
      </footer>
    </article>
  );
}

/** Structured inspection of one canonical skill specification and its provenance. */
function SkillDetailPanel(props: { detail: SkillDetail }) {
  const spec = props.detail.spec;
  const strategySummary = stringField(spec, "strategy_summary");
  const preconditions = stringListField(spec, "preconditions");
  const taskScope = stringListField(spec, "task_scope");
  const dependencies = stringListField(spec, "dependencies");
  const recoveryPolicy = stringListField(spec, "recovery_policy");
  const parameterizedPlan = listField(spec, "parameterized_plan");
  const actionPlan = listField(spec, "action_plan");
  const sourceStepRange = recordField(spec, "source_step_range");
  const sourceEvidence = recordField(spec, "source_evidence");
  const verifierStats = recordField(spec, "verifier_stats");
  const validation = recordField(spec, "validation");
  const metrics = recordField(spec, "metrics");
  const sourceRunId =
    stringField(spec, "source_run_id") ?? props.detail.source_run_id ?? "not recorded";
  const stepStart = sourceStepRange?.start;
  const stepEnd = sourceStepRange?.end;

  return (
    <div className="skill-detail-panel">
      <section className="skill-spec-section skill-strategy-section">
        <span className="eyebrow">Strategy</span>
        <p>{strategySummary ?? "No strategy summary recorded."}</p>
      </section>
      <div className="skill-spec-grid">
        <SkillTextList title="Preconditions" values={preconditions} />
        <SkillTextList title="Task Scope" values={taskScope} />
        <SkillTextList title="Dependencies" values={dependencies} />
        <SkillTextList title="Recovery Policy" values={recoveryPolicy} />
      </div>
      <section className="skill-spec-section">
        <header>
          <strong>Parameterized Plan</strong>
          <span>{parameterizedPlan.length} steps</span>
        </header>
        <pre>{formatJson(parameterizedPlan)}</pre>
      </section>
      <section className="skill-source-evidence">
        <header>
          <strong>Source Evidence</strong>
          <span>Auditable provenance</span>
        </header>
        <div className="skill-source-facts">
          <Fact label="Source Run" value={sourceRunId} />
          <Fact
            label="Source Steps"
            value={
              typeof stepStart === "number" && typeof stepEnd === "number"
                ? `${stepStart}-${stepEnd}`
                : "not recorded"
            }
          />
          <Fact label="Replay Actions" value={actionPlan.length} />
          <Fact label="Updated" value={formatDate(props.detail.updated_at)} />
        </div>
        <div className="skill-evidence-grid">
          <EvidenceColumn title="Trajectory Evidence" value={formatJson(sourceEvidence ?? {})} />
          <EvidenceColumn title="Verifier Evidence" value={formatJson(verifierStats ?? {})} />
          <EvidenceColumn title="Validation" value={formatJson(validation ?? {})} />
          <EvidenceColumn title="Metrics" value={formatJson(metrics ?? {})} />
        </div>
      </section>
      <details className="canonical-spec">
        <summary>Canonical SkillSpec JSON</summary>
        <pre>{formatJson(spec)}</pre>
      </details>
    </div>
  );
}

/** Compact string-list field inside the skill specification inspector. */
function SkillTextList(props: { title: string; values: string[] }) {
  return (
    <section className="skill-text-list">
      <strong>{props.title}</strong>
      {props.values.length ? (
        <ul>
          {props.values.map((value) => <li key={value}>{value}</li>)}
        </ul>
      ) : (
        <span>None recorded</span>
      )}
    </section>
  );
}

/** Minimal review queue plus media-first evidence page for creative-task adjudication. */
function HumanReviewWorkspace(props: {
  reviews: HumanReview[];
  evaluations: CreativeEvaluation[];
  onChanged: () => void;
}) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(props.reviews[0]?.run_id ?? null);
  useEffect(() => {
    if (!props.reviews.some((review) => review.run_id === selectedRunId)) {
      setSelectedRunId(props.reviews[0]?.run_id ?? null);
    }
  }, [props.reviews, selectedRunId]);
  const selected =
    props.reviews.find((review) => review.run_id === selectedRunId) ?? props.reviews[0] ?? null;
  const selectedEvaluation = selected
    ? props.evaluations.find((evaluation) => evaluation.run_id === selected.run_id) ?? null
    : null;

  return (
    <section className="review-workspace">
      <aside className="review-index" aria-label="Human review queue">
        <SectionTitle
          icon={<ClipboardCheck size={16} />}
          title="Human Review"
          meta={`${props.reviews.filter((review) => review.status === "awaiting_review").length} pending`}
        />
        <div className="review-run-list">
          {props.reviews.length ? (
            props.reviews.map((review) => (
              <button
                className={`review-run-row ${review.run_id === selected?.run_id ? "selected" : ""}`}
                key={review.run_id}
                type="button"
                onClick={() => setSelectedRunId(review.run_id)}
              >
                <span className={`status-dot ${statusClass(review.status)}`} aria-hidden="true" />
                <span>
                  <strong>{review.task_name}</strong>
                  <small>{review.status}</small>
                </span>
                <ChevronRight size={15} aria-hidden="true" />
              </button>
            ))
          ) : (
            <EmptyState label="No reviews" />
          )}
        </div>
      </aside>
      <section className="review-page">
        {selected ? (
          <HumanReviewDetail
            review={selected}
            evaluation={selectedEvaluation}
            onChanged={props.onChanged}
          />
        ) : (
          <EmptyState label="Select a review" />
        )}
      </section>
    </section>
  );
}

/** Media-first review detail with an on-demand full ReAct trajectory. */
function HumanReviewDetail(props: {
  review: HumanReview;
  evaluation: CreativeEvaluation | null;
  onChanged: () => void;
}) {
  const [notes, setNotes] = useState(props.review.notes);
  const [busy, setBusy] = useState(false);
  const [trajectory, setTrajectory] = useState<RunReplay | null>(null);
  const [trajectoryLoading, setTrajectoryLoading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    setNotes(props.review.notes);
    setTrajectory(null);
    setLocalError(null);
  }, [props.review.run_id, props.review.notes]);

  const loadTrajectory = async () => {
    if (trajectory || trajectoryLoading) {
      return;
    }
    setTrajectoryLoading(true);
    try {
      setTrajectory(await getRunReplay(props.review.run_id));
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setTrajectoryLoading(false);
    }
  };

  const submitDecision = async (decision: HumanReviewDecision) => {
    setBusy(true);
    setLocalError(null);
    try {
      await decideHumanReview(props.review.run_id, decision, props.review.version, notes);
      props.onChanged();
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const poster = props.review.media.image_url
    ? getApiAssetUrl(props.review.media.image_url)
    : undefined;

  return (
    <article className="review-detail">
      <header className="review-title">
        <h2>{props.review.task_name}</h2>
        <span className={`status-badge ${statusClass(props.review.status)}`}>{props.review.status}</span>
      </header>
      <CreativeEvaluationPanel
        evaluation={props.evaluation}
        fallback={props.review.mineclip}
        humanStatus={props.review.status}
      />
      <section className="review-media" aria-label="Final creative task evidence">
        {props.review.media.video_url ? (
          <video controls preload="metadata" poster={poster} key={`${props.review.run_id}-${props.review.version}`}>
            <source src={getApiAssetUrl(props.review.media.video_url)} />
          </video>
        ) : props.review.media.image_url ? (
          <img src={getApiAssetUrl(props.review.media.image_url)} alt={props.review.task_name} />
        ) : (
          <EmptyState label="Final media unavailable" />
        )}
      </section>
      {props.review.status === "awaiting_review" ? (
        <section className="review-decision">
          <textarea
            aria-label="Review notes"
            placeholder="Review notes"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            maxLength={4000}
          />
          <div className="review-actions">
            <button type="button" className="review-command approve" disabled={busy} onClick={() => void submitDecision("approved")}>
              <CheckCircle2 size={16} aria-hidden="true" />
              Approve
            </button>
            <button type="button" className="review-command reject" disabled={busy} onClick={() => void submitDecision("rejected")}>
              <XCircle size={16} aria-hidden="true" />
              Reject
            </button>
            <button type="button" className="review-command" disabled={busy} onClick={() => void submitDecision("revision_requested")}>
              <RotateCcw size={16} aria-hidden="true" />
              Request Revision
            </button>
            <button type="button" className="review-command" disabled={busy} onClick={() => void submitDecision("inconclusive")}>
              <CircleHelp size={16} aria-hidden="true" />
              Inconclusive
            </button>
          </div>
        </section>
      ) : (
        <section className="review-decision-summary">
          <strong>{props.review.decision ?? props.review.status}</strong>
          {props.review.notes ? <p>{props.review.notes}</p> : null}
        </section>
      )}
      {localError ? <Alert message={localError} /> : null}
      <details
        className="review-trajectory"
        onToggle={(event) => {
          if (event.currentTarget.open) {
            void loadTrajectory();
          }
        }}
      >
        <summary>
          <ScrollText size={16} aria-hidden="true" />
          Complete Agent Trajectory
        </summary>
        {trajectoryLoading ? <EmptyState label="Loading trajectory" /> : null}
        {trajectory ? <ReviewTrajectory replay={trajectory} /> : null}
      </details>
    </article>
  );
}

/** MineCLIP score feedback, key frames, and trajectory trend shown before human adjudication. */
function CreativeEvaluationPanel(props: {
  evaluation: CreativeEvaluation | null;
  fallback: Record<string, unknown> | null;
  humanStatus: string;
}) {
  const fallbackScore = props.fallback ? numberField(props.fallback, "score") : null;
  const score = props.evaluation?.score ?? fallbackScore;
  const fallbackFrameCount = props.fallback ? numberField(props.fallback, "frame_count") : null;
  const fallbackWindowCount = props.fallback ? numberField(props.fallback, "window_count") : null;
  const frameCount = props.evaluation?.frame_count ?? fallbackFrameCount ?? 0;
  const windowCount = props.evaluation?.window_count ?? fallbackWindowCount ?? 0;
  const trend = props.evaluation ? creativeTrendPoints(props.evaluation.result) : [];
  const keyFrames = props.evaluation ? creativeKeyFrames(props.evaluation.result) : [];
  const scorer = props.evaluation
    ? `${props.evaluation.scorer}${props.evaluation.variant ? ` / ${props.evaluation.variant}` : ""}`
    : "MineCLIP";
  const feedbackStatus = score === null ? "pending" : "feedback ready";

  return (
    <section className="creative-feedback" aria-label="MineCLIP feedback">
      <div className="creative-feedback-header">
        <div>
          <span className="eyebrow">External evaluator</span>
          <h3>MineCLIP Feedback</h3>
          {props.evaluation?.prompt ? <p>{props.evaluation.prompt}</p> : null}
        </div>
        <span className={`status-badge ${score === null ? "warn" : "ok"}`}>{feedbackStatus}</span>
      </div>
      <div className="creative-metrics">
        <Fact label="MineCLIP Score" value={score === null ? "pending" : formatScore(score)} />
        <Fact label="Scorer" value={scorer} />
        <Fact label="Frames" value={frameCount} />
        <Fact label="Windows" value={windowCount} />
        <Fact label="Human Review" value={props.humanStatus} />
      </div>
      <p className="creative-feedback-note">
        MineCLIP provides visual-semantic feedback. The human reviewer remains the authoritative decision maker.
      </p>
      {trend.length ? <CreativeScoreTrend points={trend} /> : null}
      {keyFrames.length ? <CreativeKeyFrames frames={keyFrames} /> : null}
    </section>
  );
}

/** Compact bar trend for MineCLIP target probability across sampled video windows. */
function CreativeScoreTrend(props: { points: CreativeTrendPoint[] }) {
  const peak = Math.max(...props.points.map((point) => point.score));
  const average =
    props.points.reduce((total, point) => total + point.score, 0) / props.points.length;
  return (
    <section className="creative-evidence-section">
      <SectionTitle
        icon={<Activity size={16} />}
        title="Score Trend"
        meta={`${props.points.length} windows · avg ${formatScore(average)} · peak ${formatScore(peak)}`}
      />
      <div className="creative-trend-wrap">
        <div className="creative-trend" role="img" aria-label="MineCLIP score trend by sampled window">
          {props.points.map((point) => (
            <div
              className="creative-trend-column"
              key={`${point.windowIndex}-${point.score}`}
              title={`Window ${point.windowIndex}: ${formatScore(point.score)}`}
            >
              <span
                className="creative-trend-bar"
                style={{ height: `${Math.max(2, Math.min(100, point.score * 100))}%` }}
              />
            </div>
          ))}
        </div>
        <div className="creative-trend-legend">
          <span>Window {props.points[0]?.windowIndex ?? 0}</span>
          <span>Target probability, 0–1</span>
          <span>Window {props.points.at(-1)?.windowIndex ?? props.points.length - 1}</span>
        </div>
      </div>
    </section>
  );
}

/** Highest-scoring MineCLIP frames with public audited image endpoints. */
function CreativeKeyFrames(props: { frames: CreativeKeyFrame[] }) {
  return (
    <section className="creative-evidence-section">
      <SectionTitle
        icon={<FileSearch size={16} />}
        title="Key Frames"
        meta={`${props.frames.length} visual checkpoints`}
      />
      <div className="creative-key-frames">
        {props.frames.map((frame) => (
          <figure key={`${frame.imageUrl}-${frame.sequence ?? frame.windowIndex ?? "frame"}`}>
            <img src={getApiAssetUrl(frame.imageUrl)} alt={`MineCLIP key frame ${frame.sequence ?? ""}`} />
            <figcaption>
              <span>
                {typeof frame.sequence === "number" ? `Frame ${frame.sequence}` : "Key frame"}
                {typeof frame.windowIndex === "number" ? ` · Window ${frame.windowIndex}` : ""}
              </span>
              <strong>{frame.score === null ? "score n/a" : formatScore(frame.score)}</strong>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}

/** Full prompt, observation, decision, and action evidence shown only when expanded. */
function ReviewTrajectory(props: { replay: RunReplay }) {
  if (!props.replay.steps.length) {
    return <EmptyState label="No step evidence" />;
  }
  const options: EvidenceOptions = {
    showTokens: false,
    showRequestMeta: false,
    showRawJson: false
  };
  return (
    <div className="review-round-list">
      {props.replay.steps.map((step) => (
        <RoundCard key={step.step_index} step={step} options={options} />
      ))}
    </div>
  );
}

/** Historical evaluation report followed by the original Week 8 benchmark. */
function ReportsWorkspace(props: {
  reports: EvaluationReports | null;
  comparison: BenchmarkComparison | null;
}) {
  const summary = props.reports?.summary;
  const totalRuns = reportNumber(summary, "total_runs", "runs");
  const uniqueTasks = reportNumber(summary, "unique_tasks");
  const succeeded = reportNumber(summary, "succeeded_runs", "succeeded");
  const failed = reportNumber(summary, "failed_runs", "failed");
  const cancelled = reportNumber(summary, "cancelled_runs", "cancelled");
  const running = reportNumber(summary, "running_runs", "running");
  const unverified = reportNumber(summary, "unverified_runs", "unverified");
  const successRate = reportNumber(summary, "success_rate");
  const totalSteps = reportNumber(summary, "total_steps");
  const modelCalls = reportNumber(summary, "total_model_calls", "model_calls");
  const runtimeErrors = reportNumber(summary, "total_runtime_errors", "runtime_errors");
  const inputTokens = reportNumber(summary, "total_input_tokens", "input_tokens");
  const outputTokens = reportNumber(summary, "total_output_tokens", "output_tokens");
  const totalTokens = reportNumber(summary, "total_tokens");
  const duration = reportNumber(summary, "total_duration_sec", "duration_seconds");
  const averageDuration = reportNumber(summary, "avg_duration_sec");
  const averageSteps = reportNumber(summary, "avg_steps_per_run");
  const estimatedCost = reportNumber(summary, "estimated_cost");
  const categories = props.reports?.by_category ?? [];
  const skillUsage = props.reports?.by_skill_usage ?? [];
  const recentRuns = props.reports?.recent_runs ?? [];

  return (
    <div className="reports-workspace">
      <section className="report-section">
        <SectionTitle
          icon={<Activity size={16} />}
          title="Evaluation Reports"
          meta={props.reports?.generated_at ? `Generated ${formatDate(props.reports.generated_at)}` : "No report loaded"}
        />
        {props.reports ? (
          <>
            <div className="report-kpi-grid">
              <AuditMetric
                icon={<Database size={17} />}
                label="Historical Runs"
                value={formatReportNumber(totalRuns)}
                detail={`${formatReportNumber(uniqueTasks)} unique tasks`}
              />
              <AuditMetric
                icon={<CheckCircle2 size={17} />}
                label="Verified Success"
                value={formatPercent(successRate)}
                detail={`${formatReportNumber(succeeded)} succeeded · ${formatReportNumber(failed)} failed`}
              />
              <AuditMetric
                icon={<Route size={17} />}
                label="Agent Steps"
                value={formatReportNumber(totalSteps)}
                detail={`${formatReportNumber(averageSteps, 1)} average per run`}
              />
              <AuditMetric
                icon={<MessageSquareText size={17} />}
                label="Model Calls"
                value={formatReportNumber(modelCalls)}
                detail={`${formatReportNumber(runtimeErrors)} runtime errors`}
              />
              <AuditMetric
                icon={<TerminalSquare size={17} />}
                label="Tokens"
                value={formatReportNumber(totalTokens)}
                detail={`${formatReportNumber(inputTokens)} input · ${formatReportNumber(outputTokens)} output`}
              />
              <AuditMetric
                icon={<Clock size={17} />}
                label="Run Time"
                value={formatDurationSeconds(duration)}
                detail={`${formatDurationSeconds(averageDuration)} average per run`}
              />
              <AuditMetric
                icon={<Boxes size={17} />}
                label="Estimated Cost"
                value={formatEstimatedCost(estimatedCost)}
                detail="Only recorded API usage"
              />
              <AuditMetric
                icon={<ListChecks size={17} />}
                label="Other States"
                value={formatReportNumber(unverified)}
                detail={`${formatReportNumber(running)} running · ${formatReportNumber(cancelled)} cancelled`}
              />
            </div>

            <div className="report-breakdown-grid">
              <ReportBreakdown
                title="By Task Category"
                rows={categories}
                emptyLabel="No category breakdown recorded"
              />
              <ReportBreakdown
                title="By Skill Usage"
                rows={skillUsage}
                emptyLabel="No skill-usage breakdown recorded"
              />
            </div>

            <section className="report-subsection">
              <SectionTitle
                icon={<ScrollText size={16} />}
                title="Recent Runs"
                meta={`${recentRuns.length} shown`}
              />
              <RecentEvaluationRuns runs={recentRuns} />
            </section>
          </>
        ) : (
          <EmptyState label="Historical evaluation report is not available" />
        )}
      </section>

      <section className="report-section legacy-report">
        <SectionTitle
          icon={<GitCompare size={16} />}
          title="Legacy Benchmark"
          meta={props.comparison?.comparison_id ?? "Not available"}
        />
        {props.comparison?.modes.length ? (
          <div className="comparison-table">
            {props.comparison.modes.map((mode) => (
              <ComparisonRow key={mode.mode} mode={mode} />
            ))}
          </div>
        ) : (
          <EmptyState label="Legacy Week 8 benchmark is not available" />
        )}
      </section>
    </div>
  );
}

/** Category or skill-usage report with compact execution totals. */
function ReportBreakdown(props: {
  title: string;
  rows: EvaluationReportBreakdown[];
  emptyLabel: string;
}) {
  return (
    <section className="report-subsection">
      <SectionTitle icon={<GitCompare size={16} />} title={props.title} meta={`${props.rows.length} groups`} />
      {props.rows.length ? (
        <div className="report-breakdown-list">
          {props.rows.map((row, index) => {
            const key =
              reportText(row, "key", "category", "mode", "label") ?? `${props.title}-${index}`;
            const label = reportText(row, "label") ?? humanizeReportKey(key);
            const runs = reportNumber(row, "run_count", "runs");
            const succeeded = reportNumber(row, "succeeded_runs", "succeeded");
            const failed = reportNumber(row, "failed_runs", "failed");
            const successRate = reportNumber(row, "success_rate");
            return (
              <article className="report-breakdown-row" key={`${key}-${index}`}>
                <div className="report-breakdown-name">
                  <strong>{label}</strong>
                  <span>{formatReportNumber(runs)} runs</span>
                </div>
                <Fact
                  label="Verified Success"
                  value={formatEvaluatedSuccess(succeeded, failed, successRate)}
                />
                <Fact label="Failed" value={formatReportNumber(failed)} />
                <Fact label="Steps" value={formatReportNumber(reportNumber(row, "total_steps"))} />
                <Fact label="Tokens" value={formatReportNumber(reportNumber(row, "total_tokens"))} />
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyState label={props.emptyLabel} />
      )}
    </section>
  );
}

/** Recent historical runs rendered independently from the runtime inspector. */
function RecentEvaluationRuns(props: { runs: EvaluationReportRun[] }) {
  if (!props.runs.length) {
    return <EmptyState label="No historical runs recorded" />;
  }
  return (
    <div className="report-run-table-wrap">
      <table className="report-run-table">
        <thead>
          <tr>
            <th>Run</th>
            <th>Task</th>
            <th>Category</th>
            <th>Result</th>
            <th>Verifier</th>
            <th>Skills</th>
            <th>Steps</th>
            <th>Calls</th>
            <th>Tokens</th>
            <th>Duration</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {props.runs.map((run, index) => {
            const runId = reportText(run, "run_id") ?? `run-${index + 1}`;
            const result =
              reportText(run, "task_result", "lifecycle_status", "status") ?? "unverified";
            return (
              <tr key={`${runId}-${index}`}>
                <td><code title={runId}>{compactReportId(runId)}</code></td>
                <td>{reportText(run, "task_id") ?? "Unknown task"}</td>
                <td>{humanizeReportKey(reportText(run, "category") ?? "uncategorized")}</td>
                <td><span className={`status-badge ${statusClass(result)}`}>{result}</span></td>
                <td>{formatVerifierResult(run.verifier_success)}</td>
                <td>{humanizeReportKey(reportText(run, "skill_usage") ?? "not recorded")}</td>
                <td>{formatReportNumber(run.step_count)}</td>
                <td>{formatReportNumber(run.model_call_count)}</td>
                <td>{formatReportNumber(run.total_tokens)}</td>
                <td>{formatDurationSeconds(reportNumber(run, "duration_sec", "duration_seconds"))}</td>
                <td>{run.started_at ? formatDate(run.started_at) : "Not recorded"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** One benchmark mode row in the comparison report. */
function ComparisonRow(props: { mode: BenchmarkMode }) {
  return (
    <article className="comparison-row">
      <div>
        <GitCompare size={16} aria-hidden="true" />
        <strong>{props.mode.label}</strong>
        <span className="status-badge neutral">{props.mode.status}</span>
      </div>
      <Fact label="Success" value={formatRatio(props.mode.success_count, props.mode.task_count, props.mode.success_rate)} />
      <Fact label="Invalid" value={formatPercent(props.mode.invalid_action_rate)} />
      <Fact label="Crashes" value={formatPercent(props.mode.runtime_crash_rate)} />
      <Fact label="Steps" value={props.mode.total_steps ?? "pending"} />
      <Fact label="Tokens" value={props.mode.total_tokens ?? "pending"} />
      <Fact label="Cost" value={formatEstimatedCost(props.mode.estimated_cost)} />
      <small>{props.mode.source}</small>
    </article>
  );
}

/** Fixed-size fact cell for compact metadata. */
function Fact(props: { label: string; value: string | number }) {
  return (
    <div className="fact">
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

/** Small metric cell in the primary sidebar. */
function SmallMetric(props: { label: string; value: string | number }) {
  return (
    <div className="small-metric">
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

/** Minimal placeholder row for empty audit panes. */
function EmptyState(props: { label: string }) {
  return (
    <div className="empty-state">
      <Database size={18} aria-hidden="true" />
      <span>{props.label}</span>
    </div>
  );
}

/** Aggregate compact dashboard metrics from agent and skill rows. */
function buildSummary(
  agents: AgentSummary[],
  skills: SkillSummary[],
  humanReviews: HumanReview[]
) {
  return {
    agentCount: agents.length,
    runCount: agents.reduce((total, agent) => total + agent.run_count, 0),
    skillCount: skills.length,
    pendingReviewCount: humanReviews.filter((review) => review.status === "awaiting_review").length
  };
}

/** Return the largest persisted trajectory event id in a run event list. */
function maxEventId(events: TrajectoryEvent[]) {
  return events.reduce((maxId, event) => Math.max(maxId, event.id), 0);
}

/** Merge streamed trajectory events into the existing timeline without duplicates. */
function mergeTrajectoryEvents(current: TrajectoryEvent[], incoming: TrajectoryEvent[]) {
  const eventsById = new Map<number, TrajectoryEvent>();
  for (const event of current) {
    eventsById.set(event.id, event);
  }
  for (const event of incoming) {
    eventsById.set(event.id, event);
  }
  return Array.from(eventsById.values()).sort((left, right) => left.id - right.id);
}

/** Format a nullable timestamp for compact display. */
function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "pending";
}

/** Format the dashboard refresh clock. */
function formatClock(value: Date | null) {
  return value ? value.toLocaleTimeString() : "pending";
}

/** Build a short partial-load error without discarding successful dashboard data. */
function errorMessage(section: string, reason: unknown) {
  const detail = reason instanceof Error ? reason.message : String(reason);
  return `${section}: ${detail}`;
}

/** Pretty-print a JSON value without risking render-time exceptions. */
function formatJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Format a prompt payload into readable audit text. */
function formatPrompt(context: Record<string, unknown> | null) {
  if (!context) {
    return "prompt not recorded";
  }
  const promptSections = asRecord(context.prompt_sections);
  if (promptSections) {
    return Object.entries(promptSections)
      .map(([name, value]) => `# ${name}\n${typeof value === "string" ? value : formatJson(value)}`)
      .join("\n\n");
  }
  const messages = Array.isArray(context.messages) ? context.messages : null;
  if (messages) {
    return messages
      .map((message, index) => {
        const record = asRecord(message);
        return record ? `# ${index + 1} ${textValue(record.role, "message")}\n${textValue(record.content, formatJson(record))}` : formatJson(message);
      })
      .join("\n\n");
  }
  return formatJson(context);
}

/** Summarize the persisted prompt shape without rendering the full request. */
function summarizePrompt(context: Record<string, unknown> | null) {
  if (!context) {
    return "not recorded";
  }
  const promptSections = asRecord(context.prompt_sections);
  if (promptSections) {
    const count = Object.keys(promptSections).length;
    return `${count} prompt section${count === 1 ? "" : "s"}`;
  }
  const messages = Array.isArray(context.messages) ? context.messages : null;
  if (messages) {
    return `${messages.length} message${messages.length === 1 ? "" : "s"}`;
  }
  return `${Object.keys(context).length} context field${Object.keys(context).length === 1 ? "" : "s"}`;
}

/** Summarize a Minecraft observation for quick scanning. */
function summarizeObservation(observation: Record<string, unknown> | null) {
  if (!observation) {
    return "not recorded";
  }
  return `${arrayField(observation, "inventory").length} inventory, ${arrayField(observation, "nearby_blocks").length} blocks, ${arrayField(observation, "nearby_entities").length} entities`;
}

/** Summarize the latest parsed action. */
function summarizeAction(action: Record<string, unknown> | null) {
  if (!action) {
    return "not parsed";
  }
  const type = textValue(action.type, "unknown action");
  const args = asRecord(action.args);
  const argKeys = args ? Object.keys(args) : [];
  return argKeys.length ? `${type}: ${argKeys.join(", ")}` : type;
}

/** Extract the auditable model decision envelope for one replay step. */
function decisionEnvelope(step: RunReplay["steps"][number]) {
  const callDecision = asRecord(step.model_calls[0]?.raw_response?.decision);
  if (callDecision) {
    return callDecision;
  }
  for (const event of step.model_events) {
    const decision = asRecord(event.payload.decision);
    if (decision) {
      return decision;
    }
  }
  return {};
}

/** Summarize a decision envelope for compact replay scanning. */
function summarizeDecision(decision: Record<string, unknown>) {
  const summary = textValue(decision.reasoning_summary, "");
  if (summary) {
    return summary.length > 90 ? `${summary.slice(0, 87)}...` : summary;
  }
  const knowledgeNeed = asRecord(decision.knowledge_need);
  if (knowledgeNeed?.needed === true) {
    return `needs knowledge: ${textValue(knowledgeNeed.query, "query unspecified")}`;
  }
  return "not recorded";
}

/** Extract request metadata from a model call without duplicating raw response by default. */
function requestMetadata(call: ModelCall | null) {
  if (!call) {
    return {};
  }
  return {
    id: call.id,
    source: call.source,
    created_at: call.created_at,
    raw_response_keys: Object.keys(call.raw_response ?? {})
  };
}

/** Convert unknown JSON values into compact UI text. */
function textValue(value: unknown, fallback = "unknown") {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

/** Convert unknown values into short labels for dense audit rows. */
function stringValue(value: unknown, fallback = "unknown") {
  return textValue(value, fallback);
}

/** Return a JSON object or an empty record for optional nested payloads. */
function recordValue(value: unknown): Record<string, unknown> {
  return asRecord(value) ?? {};
}

/** Format a Minecraft position object as x,y,z compact text. */
function compactPosition(value: unknown) {
  const record = asRecord(value);
  if (!record) {
    return "unknown";
  }
  return `(${stringValue(record.x)},${stringValue(record.y)},${stringValue(record.z)})`;
}

/** Return a JSON object when an unknown value is a plain record. */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

/** Return one optional string field from a JSON record. */
function stringField(record: Record<string, unknown>, field: string) {
  const value = record[field];
  return typeof value === "string" && value.trim() ? value : null;
}

/** Return one optional finite numeric field from a JSON record. */
function numberField(record: Record<string, unknown>, field: string) {
  const value = record[field];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Return one optional nested object field from a JSON record. */
function recordField(record: Record<string, unknown>, field: string) {
  return asRecord(record[field]);
}

/** Return one list field while preserving its unknown JSON item type. */
function listField(record: Record<string, unknown>, field: string): unknown[] {
  const value = record[field];
  return Array.isArray(value) ? value : [];
}

/** Return only non-empty strings from one JSON list field. */
function stringListField(record: Record<string, unknown>, field: string) {
  return listField(record, field).filter(
    (value): value is string => typeof value === "string" && Boolean(value.trim())
  );
}

/** Return an array field from a nullable JSON record. */
function arrayField(record: Record<string, unknown> | null | undefined, field: string) {
  const value = record?.[field];
  return Array.isArray(value) ? value : [];
}

/** Parse MineCLIP score samples from a creative-evaluation result. */
function creativeTrendPoints(result: Record<string, unknown>): CreativeTrendPoint[] {
  return listField(result, "score_trend")
    .map((value, index) => {
      const point = asRecord(value);
      if (!point) {
        return null;
      }
      const score =
        numberField(point, "target_probability") ??
        numberField(point, "score");
      if (score === null) {
        return null;
      }
      return {
        windowIndex: numberField(point, "window_index") ?? index,
        score: Math.max(0, Math.min(1, score))
      };
    })
    .filter((point): point is CreativeTrendPoint => point !== null);
}

/** Parse public key-frame descriptors from a creative-evaluation result. */
function creativeKeyFrames(result: Record<string, unknown>): CreativeKeyFrame[] {
  return listField(result, "key_frames")
    .map((value) => {
      const frame = asRecord(value);
      if (!frame) {
        return null;
      }
      const imageUrl = stringField(frame, "image_url");
      if (!imageUrl) {
        return null;
      }
      return {
        imageUrl,
        score: numberField(frame, "score"),
        sequence: numberField(frame, "sequence"),
        windowIndex: numberField(frame, "window_index")
      };
    })
    .filter((frame): frame is CreativeKeyFrame => frame !== null);
}

/** Format numeric usage values as compact integers when possible. */
function formatUsageNumber(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/** Read the first finite numeric metric across compatible report field names. */
function reportNumber(value: object | null | undefined, ...keys: string[]) {
  if (!value) {
    return null;
  }
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "number" && Number.isFinite(candidate)) {
      return candidate;
    }
  }
  return null;
}

/** Read the first non-empty label across compatible report field names. */
function reportText(value: object | null | undefined, ...keys: string[]) {
  if (!value) {
    return null;
  }
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const candidate = record[key];
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate;
    }
  }
  return null;
}

/** Format nullable report counts without inventing absent history. */
function formatReportNumber(value: number | null | undefined, fractionDigits = 0) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "Not recorded";
  }
  return value.toLocaleString(undefined, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits
  });
}

/** Format elapsed seconds at a useful dashboard scale. */
function formatDurationSeconds(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "Not recorded";
  }
  if (value < 60) {
    return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  }
  if (value < 3600) {
    return `${(value / 60).toFixed(1)}m`;
  }
  return `${(value / 3600).toFixed(1)}h`;
}

/** Display persisted API cost only; a missing value is deliberately not inferred. */
function formatEstimatedCost(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "Not recorded";
  }
  return `$${value.toFixed(2)}`;
}

/** Turn API grouping keys into audience-facing labels. */
function humanizeReportKey(value: string) {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

/** Preserve recognizable run ids while keeping the report table compact. */
function compactReportId(value: string) {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

/** Format the authoritative verifier state independently from run lifecycle. */
function formatVerifierResult(value: boolean | null | undefined) {
  if (value === true) {
    return "Passed";
  }
  if (value === false) {
    return "Failed";
  }
  return "Unverified";
}

/** Format MineCLIP feedback on its native zero-to-one scale. */
function formatScore(value: number) {
  return value.toFixed(3);
}

/** Format a ratio as count and percent text. */
function formatRatio(success: number | null, total: number | null, rate: number | null) {
  if (success === null || total === null) {
    return "pending";
  }
  return `${success}/${total} (${formatPercent(rate)})`;
}

/** Format success against verifier-evaluated runs rather than every lifecycle row. */
function formatEvaluatedSuccess(
  succeeded: number | null,
  failed: number | null,
  rate: number | null
) {
  if (succeeded === null || failed === null) {
    return formatPercent(rate);
  }
  return `${formatPercent(rate)} · ${succeeded}/${succeeded + failed} evaluated`;
}

/** Format nullable numeric ratios as percentages. */
function formatPercent(value: number | null) {
  return value === null ? "pending" : `${(value * 100).toFixed(1)}%`;
}

/** Summarize a verifier payload for labels. */
function verifierLabel(verifier: Record<string, unknown> | null) {
  if (!verifier) {
    return "missing";
  }
  const nested = asRecord(verifier.verifier);
  const success = typeof verifier.success === "boolean" ? verifier.success : nested?.success;
  if (success === true) {
    return "success";
  }
  if (success === false) {
    return "failed";
  }
  return "recorded";
}

/** Map lifecycle status strings onto stable CSS classes. */
function statusClass(status: string) {
  if (["completed", "promoted", "validated", "online", "ok", "verified", "success", "succeeded", "approved"].includes(status)) {
    return "ok";
  }
  if (["failed", "deprecated", "error", "blocked", "task_timeout", "model_timeout", "runtime_error", "rejected"].includes(status)) {
    return "bad";
  }
  if (["running", "draft", "staged", "pending", "unverified", "reconnecting", "connecting", "awaiting_review", "awaiting_human_review", "revision_requested"].includes(status)) {
    return "warn";
  }
  return "neutral";
}
