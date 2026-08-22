/** Base URL used by the dashboard to reach the FastAPI backend. */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/** Compact run row returned by the dashboard run-list endpoint. */
export interface RunSummary {
  id: string;
  trace_id: string;
  root_span_id: string;
  task_id: string;
  status: string;
  lifecycle_status: string;
  task_result: string;
  verifier_success: boolean | null;
  started_at: string | null;
  finished_at: string | null;
  step_count: number;
  event_count: number;
  model_call_count: number;
  runtime_error_count: number;
}

/** Detailed persisted run metadata for the selected run. */
export interface RunDetail {
  id: string;
  trace_id: string;
  root_span_id: string;
  task_id: string;
  status: string;
  lifecycle_status: string;
  task_result: string;
  verifier_success: boolean | null;
  task_spec: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  resumed_from_checkpoint_id: number | null;
}

/** One trajectory event shown in the run timeline. */
export interface TrajectoryEvent {
  id: number;
  run_id: string;
  step_index: number | null;
  trace_id: string;
  span_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  task_id: string | null;
  agent_id: string | null;
  created_at: string | null;
}

/** One persisted model call including parsed action and usage metadata. */
export interface ModelCall {
  id: number;
  run_id: string;
  step_index: number;
  trace_id: string;
  span_id: string;
  raw_content: string;
  action: Record<string, unknown> | null;
  usage: Record<string, unknown>;
  raw_response: Record<string, unknown>;
  source: string;
  created_at: string | null;
}

/** One worker or runtime error row persisted for a run. */
export interface RuntimeErrorRecord {
  id: number;
  run_id: string;
  step_index: number | null;
  trace_id: string;
  span_id: string;
  error_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}

/** One step-centric replay record assembled from multiple audit tables. */
export interface ReplayStep {
  step_index: number;
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  span_status: string;
  span_started_at: string | null;
  span_finished_at: string | null;
  status: string;
  observation: Record<string, unknown> | null;
  context: Record<string, unknown> | null;
  resolved_terms: string[];
  retrieved_docs: string[];
  retrieved_skills: Record<string, unknown>[];
  model_events: TrajectoryEvent[];
  model_calls: ModelCall[];
  parsed_action: Record<string, unknown> | null;
  action_result: Record<string, unknown> | null;
  runtime_errors: RuntimeErrorRecord[];
  highlights: string[];
  raw_events: TrajectoryEvent[];
}

/** Full run replay payload used by the dashboard evidence-chain tab. */
export interface RunReplay {
  run: RunDetail;
  run_events: TrajectoryEvent[];
  steps: ReplayStep[];
  summary: Record<string, number>;
}

/** Run-level agent audit snapshot assembled by the backend inspector endpoint. */
export interface AgentAuditSnapshot {
  run_id: string;
  task_id: string;
  run_status: string;
  lifecycle_status: string;
  task_result: string;
  verifier_success: boolean | null;
  presence: string;
  identity: Record<string, unknown>;
  current_task: Record<string, unknown>;
  latest_observation: Record<string, unknown> | null;
  latest_action: Record<string, unknown> | null;
  latest_action_result: Record<string, unknown> | null;
  latest_model_output: string | null;
  latest_model_usage: Record<string, unknown> | null;
  token_totals: Record<string, number>;
  reset: Record<string, unknown> | null;
  verifier: Record<string, unknown> | null;
  runtime_error_count: number;
  latest_runtime_error: RuntimeErrorRecord | null;
  event_counts: Record<string, number>;
  latest_event_at: string | null;
}

/** Agent overview row grouped from persisted run audit records. */
export interface AgentSummary {
  key: string;
  display_name: string;
  username: string | null;
  worker_id: string | null;
  agent_id: string | null;
  presence: string;
  run_count: number;
  active_run_count: number;
  completed_run_count: number;
  failed_run_count: number;
  task_success_count: number;
  task_failure_count: number;
  skill_count: number;
  promoted_skill_count: number;
  latest_run_id: string | null;
  latest_task_id: string | null;
  latest_task_result: string;
  latest_verifier_success: boolean | null;
  latest_event_at: string | null;
  token_totals: Record<string, number>;
  runtime_error_count: number;
}

/** One task/run row shown in the agent detail page. */
export interface AgentTaskSummary {
  run_id: string;
  task_id: string;
  status: string;
  lifecycle_status: string;
  task_result: string;
  verifier_success: boolean | null;
  started_at: string | null;
  finished_at: string | null;
  step_count: number;
  event_count: number;
  model_call_count: number;
  runtime_error_count: number;
  token_totals: Record<string, number>;
  verifier: Record<string, unknown> | null;
}

/** Agent detail payload with overview, task history, and owned skills. */
export interface AgentDetail {
  agent: AgentSummary;
  runs: AgentTaskSummary[];
  skills: SkillSummary[];
}

/** Compact skill row used by the dashboard review queue. */
export interface SkillSummary {
  id: number;
  name: string;
  version: string;
  status: string;
  description: string;
  triggers: string[];
  action_count: number;
  source_run_id: string | null;
  updated_at: string | null;
}

/** Full skill payload returned after a review state transition. */
export interface SkillDetail {
  id: number;
  name: string;
  version: string;
  status: string;
  spec: Record<string, unknown>;
  source_run_id: string | null;
  updated_at: string | null;
}

/** Tombstone response returned after removing one skill from the active library. */
export interface DeletedSkill {
  id: number;
  name: string;
  version: string;
  deleted: true;
}

/** One execution-mode row in the Week 8 comparison table. */
export interface BenchmarkMode {
  mode: string;
  label: string;
  status: string;
  task_count: number | null;
  success_count: number | null;
  success_rate: number | null;
  invalid_action_rate: number | null;
  runtime_crash_rate: number | null;
  total_steps: number | null;
  total_tokens: number | null;
  estimated_cost: number | null;
  source: string;
  notes: string[];
  raw_baseline_results: Record<string, unknown>[];
}

/** Week 8 comparison payload assembled by the backend. */
export interface BenchmarkComparison {
  comparison_id: string;
  generated_at: string;
  modes: BenchmarkMode[];
}

/**
 * Aggregated metrics from imported and live evaluation runs.
 *
 * The optional aliases keep the dashboard compatible with both the persisted
 * audit vocabulary and the public report vocabulary while older artifacts are
 * being imported.
 */
export interface EvaluationReportSummary {
  total_runs?: number | null;
  runs?: number | null;
  unique_tasks?: number | null;
  succeeded_runs?: number | null;
  succeeded?: number | null;
  failed_runs?: number | null;
  failed?: number | null;
  cancelled_runs?: number | null;
  cancelled?: number | null;
  running_runs?: number | null;
  running?: number | null;
  unverified_runs?: number | null;
  unverified?: number | null;
  success_rate?: number | null;
  total_steps?: number | null;
  total_model_calls?: number | null;
  model_calls?: number | null;
  total_runtime_errors?: number | null;
  runtime_errors?: number | null;
  total_input_tokens?: number | null;
  input_tokens?: number | null;
  total_output_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  total_duration_sec?: number | null;
  duration_seconds?: number | null;
  avg_duration_sec?: number | null;
  avg_steps_per_run?: number | null;
  estimated_cost?: number | null;
}

/** One category or skill-usage aggregate in the evaluation report. */
export interface EvaluationReportBreakdown {
  key?: string | null;
  label?: string | null;
  category?: string | null;
  mode?: string | null;
  run_count?: number | null;
  runs?: number | null;
  succeeded_runs?: number | null;
  succeeded?: number | null;
  failed_runs?: number | null;
  failed?: number | null;
  success_rate?: number | null;
  total_steps?: number | null;
  total_model_calls?: number | null;
  model_calls?: number | null;
  total_runtime_errors?: number | null;
  runtime_errors?: number | null;
  total_input_tokens?: number | null;
  input_tokens?: number | null;
  total_output_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  estimated_cost?: number | null;
  total_duration_sec?: number | null;
  duration_seconds?: number | null;
}

/** One recent historical run included for report-level drill-down. */
export interface EvaluationReportRun {
  run_id?: string | null;
  task_id?: string | null;
  category?: string | null;
  status?: string | null;
  lifecycle_status?: string | null;
  task_result?: string | null;
  verifier_success?: boolean | null;
  skill_usage?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_sec?: number | null;
  duration_seconds?: number | null;
  step_count?: number | null;
  model_call_count?: number | null;
  runtime_error_count?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  estimated_cost?: number | null;
}

/** Historical evaluation report aggregated across imported audit stores. */
export interface EvaluationReports {
  generated_at?: string | null;
  summary?: EvaluationReportSummary | null;
  by_category?: EvaluationReportBreakdown[] | null;
  by_skill_usage?: EvaluationReportBreakdown[] | null;
  recent_runs?: EvaluationReportRun[] | null;
}

/** Persisted MineCLIP result and external creative-task evidence. */
export interface CreativeEvaluation {
  id: number;
  run_id: string;
  task_id: string;
  status: string;
  prompt: string;
  score: number | null;
  score_threshold: number | null;
  success: boolean | null;
  scorer: string;
  variant: string | null;
  calibration_status: string;
  frame_count: number;
  window_count: number;
  result: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

/** Guarded media endpoints attached to one creative-task human review. */
export interface HumanReviewMedia {
  video_url: string | null;
  image_url: string | null;
  video_available: boolean;
  image_available: boolean;
}

/** Authoritative creative-task review entry and its sanitized evidence metadata. */
export interface HumanReview {
  id: number;
  run_id: string;
  task_id: string;
  task_name: string;
  status: string;
  submission_summary: string;
  reviewer_id: string | null;
  decision: string | null;
  reason_codes: string[];
  notes: string;
  submitted_at: string | null;
  decided_at: string | null;
  version: number;
  media: HumanReviewMedia;
  mineclip: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Allowed authoritative decisions in the creative-task human review workflow. */
export type HumanReviewDecision = "approved" | "rejected" | "revision_requested" | "inconclusive";

/** Compact executable MineDojo task shown in the quick-start catalog. */
export interface LaunchTaskSummary {
  task_id: string;
  kind: "programmatic" | "creative";
  category: string;
  family: string;
  goal: string;
  description: string;
  runtime_profile: string;
  verifier_type: string;
  biome_hint: string | null;
  initial_inventory_count: number;
  spawn_mob_count: number;
  collection: string | null;
  allowed_action_count: number;
}

/** Full trusted task metadata shown before a quick-start launch. */
export interface LaunchTaskDetail extends LaunchTaskSummary {
  reset_plan: Record<string, unknown>;
  verifier: Record<string, unknown>;
  success_criteria: Record<string, unknown>;
  allowed_actions: string[];
  knowledge_tags: string[];
  source_metadata: Record<string, unknown>;
}

/** Paginated executable task response and stable filter counts. */
export interface LaunchTaskPage {
  items: LaunchTaskSummary[];
  total: number;
  offset: number;
  limit: number;
  categories: Record<string, number>;
  kinds: Record<string, number>;
}

/** Safe backend-owned defaults for the local quick-start control surface. */
export interface LauncherConfig {
  server_host: string;
  server_port: number;
  rcon_host: string;
  rcon_port: number;
  default_client_player: string | null;
  recording_window_title: string;
  rcon_password_configured: boolean;
  model_configured: boolean;
  active_job_id: string | null;
}

/** One concrete readiness assertion returned by quick-start preflight. */
export interface LaunchPreflightCheck {
  name: string;
  ok: boolean;
  state: "ready" | "pending" | "blocked";
  detail: string;
}

/** Full launch readiness evidence collected from Minecraft, RCON, and model config. */
export interface LaunchPreflight {
  launchable: boolean;
  runtime_ready: boolean;
  minecraft_reachable: boolean;
  rcon_configured: boolean;
  rcon_reachable: boolean;
  model_configured: boolean;
  client_online: boolean;
  online_players: string[];
  checks: LaunchPreflightCheck[];
}

/** User-editable, bounded settings for one allowlisted task launch. */
export interface LaunchRequest {
  task_id: string;
  view_mode: "agent" | "player";
  client_player: string;
  server_host: string;
  server_port: number;
  rcon_host: string;
  rcon_port: number;
  max_steps: number;
  max_runtime_sec: number;
  threat_pause: boolean;
  random_spawn: boolean;
  auto_promote: boolean;
}

/** Lifecycle states that still own the local launch workflow. */
export type LaunchJobStatus =
  | "starting"
  | "starting_server"
  | "waiting_for_client"
  | "running"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled";

const ACTIVE_LAUNCH_JOB_STATUSES: readonly LaunchJobStatus[] = [
  "starting",
  "starting_server",
  "waiting_for_client",
  "running",
  "cancelling"
];

/** Return whether a launcher job still owns server or task resources. */
export function isActiveLaunchJobStatus(status: LaunchJobStatus): boolean {
  return ACTIVE_LAUNCH_JOB_STATUSES.includes(status);
}

/** Dashboard-visible lifecycle state for one launcher-owned task process. */
export interface LaunchJob {
  job_id: string;
  task_id: string;
  task_kind: string;
  task_goal: string;
  view_mode: "agent" | "player";
  client_player: string;
  server_host: string;
  server_port: number;
  status: LaunchJobStatus;
  status_detail: string | null;
  server_started_by_job: boolean;
  artifact_dir: string;
  pid: number | null;
  return_code: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

/** Incremental child-process log chunk returned from a byte offset. */
export interface LaunchJobLog {
  content: string;
  next_offset: number;
  complete: boolean;
}

/** One versioned SQL-backed chunk exposed by the knowledge-management API. */
export interface KnowledgeChunk {
  id: string;
  source: string;
  title: string;
  content: string;
  tags: string[];
  metadata: Record<string, unknown>;
  enabled: boolean;
  version: number;
  has_embedding: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** Filtered page of knowledge chunks plus stable filter facets. */
export interface KnowledgeChunkPage {
  items: KnowledgeChunk[];
  total: number;
  offset: number;
  limit: number;
  sources: Record<string, number>;
  kinds: Record<string, number>;
}

/** Editable fields accepted when creating a knowledge chunk. */
export interface KnowledgeChunkCreate {
  id: string;
  source: string;
  title: string;
  content: string;
  tags: string[];
  metadata: Record<string, unknown>;
  enabled: boolean;
}

/** Editable fields accepted when updating an existing knowledge chunk. */
export interface KnowledgeChunkUpdate {
  source: string;
  title: string;
  content: string;
  tags: string[];
  metadata: Record<string, unknown>;
  enabled: boolean;
  expected_version: number;
}

/** Effective system prompt plus its persisted override state. */
export interface SystemPromptConfiguration {
  content: string;
  enabled: boolean;
  version: number;
  persisted: boolean;
  effective_source: string;
  updated_at: string | null;
}

/** One prompt-facing description for an implemented harness action. */
export interface ActionPromptConfiguration {
  action_type: string;
  display_name: string;
  category: string;
  runtime_supported: boolean;
  hard_hidden: boolean;
  prompt_visible: boolean;
  purpose: string;
  args: Record<string, unknown>;
  returns: string;
  when_to_use: string;
  recommended_next_actions: string[];
  version: number;
  persisted: boolean;
  effective_source: string;
  updated_at: string | null;
}

/** Versioned policy that controls whether active runs re-read prompt changes. */
export interface PromptHotReloadConfiguration {
  enabled: boolean;
  version: number;
  persisted: boolean;
  effective_source: string;
  updated_at: string | null;
}

/** Atomic prompt-configuration snapshot read before each agent decision. */
export interface PromptConfigurations {
  hot_reload: PromptHotReloadConfiguration;
  snapshot_revision: string;
  system_prompt: SystemPromptConfiguration;
  actions: ActionPromptConfiguration[];
}

/** Hot-reload policy update guarded by optimistic concurrency. */
export interface PromptHotReloadConfigurationUpdate {
  enabled: boolean;
  expected_version: number;
}

/** Editable system-prompt override guarded by optimistic concurrency. */
export interface SystemPromptConfigurationUpdate {
  content: string;
  enabled: boolean;
  expected_version: number;
}

/** Editable action prompt fields guarded by optimistic concurrency. */
export interface ActionPromptConfigurationUpdate {
  prompt_visible: boolean;
  purpose: string;
  args: Record<string, unknown>;
  returns: string;
  when_to_use: string;
  recommended_next_actions: string[];
  expected_version: number;
}

/** Fetch the backend health endpoint for dashboard connectivity checks. */
export async function getHealth(): Promise<{ status: string }> {
  return apiGet<{ status: string }>("/api/health");
}

/** Fetch recent persisted runs for the dashboard sidebar. */
export async function getRuns(): Promise<RunSummary[]> {
  return apiGet<RunSummary[]>("/api/runs");
}

/** Fetch detailed metadata for one selected run. */
export async function getRunDetail(runId: string): Promise<RunDetail> {
  return apiGet<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`);
}

/** Fetch trajectory events for one run timeline. */
export async function getRunEvents(runId: string): Promise<TrajectoryEvent[]> {
  return apiGet<TrajectoryEvent[]>(`/api/runs/${encodeURIComponent(runId)}/events`);
}

/** Fetch persisted model calls for one run. */
export async function getModelCalls(runId: string): Promise<ModelCall[]> {
  return apiGet<ModelCall[]>(`/api/runs/${encodeURIComponent(runId)}/model-calls`);
}

/** Fetch persisted worker/runtime errors for one run. */
export async function getRuntimeErrors(runId: string): Promise<RuntimeErrorRecord[]> {
  return apiGet<RuntimeErrorRecord[]>(`/api/runs/${encodeURIComponent(runId)}/runtime-errors`);
}

/** Fetch the step-centric replay view for one run. */
export async function getRunReplay(runId: string): Promise<RunReplay> {
  return apiGet<RunReplay>(`/api/runs/${encodeURIComponent(runId)}/replay`);
}

/** Fetch the run-level agent audit snapshot for the selected run. */
export async function getAgentAudit(runId: string): Promise<AgentAuditSnapshot> {
  return apiGet<AgentAuditSnapshot>(`/api/runs/${encodeURIComponent(runId)}/agent-audit`);
}

/** Fetch agent overview rows for the dashboard agent index. */
export async function getAgents(): Promise<AgentSummary[]> {
  return apiGet<AgentSummary[]>("/api/agents");
}

/** Fetch one agent detail view by its stable dashboard key. */
export async function getAgentDetail(agentKey: string): Promise<AgentDetail> {
  return apiGet<AgentDetail>(`/api/agents/${encodeURIComponent(agentKey)}`);
}

/** Open a Server-Sent Events stream for incremental trajectory updates. */
export function openRunEventStream(runId: string, afterId = 0): EventSource {
  const params = new URLSearchParams({ after_id: String(afterId) });
  return new EventSource(`${API_BASE_URL}/api/runs/${encodeURIComponent(runId)}/stream?${params.toString()}`);
}

/** Fetch skill review rows across all lifecycle states. */
export async function getSkills(): Promise<SkillSummary[]> {
  return apiGet<SkillSummary[]>("/api/skills");
}

/** Fetch the canonical specification and source evidence for one skill. */
export async function getSkillDetail(skillId: number): Promise<SkillDetail> {
  return apiGet<SkillDetail>(`/api/skills/${skillId}`);
}

/** Replace the editable canonical SkillSpec with optimistic concurrency. */
export async function updateSkill(
  skillId: number,
  spec: Record<string, unknown>,
  expectedUpdatedAt: string
): Promise<SkillDetail> {
  return apiPatch<SkillDetail>(
    `/api/skills/${skillId}`,
    { spec, expected_updated_at: expectedUpdatedAt },
    harnessControlHeaders()
  );
}

/** Remove a skill from the active library while retaining its audit tombstone. */
export async function deleteSkill(
  skillId: number,
  expectedUpdatedAt: string,
  reason: string
): Promise<DeletedSkill> {
  return apiDelete<DeletedSkill>(
    `/api/skills/${skillId}`,
    { expected_updated_at: expectedUpdatedAt, reason },
    harnessControlHeaders()
  );
}

/** Promote one reviewed skill candidate. */
export async function promoteSkill(
  skillId: number,
  expectedUpdatedAt: string
): Promise<SkillDetail> {
  return apiPost<SkillDetail>(
    `/api/skills/${skillId}/promote`,
    { expected_updated_at: expectedUpdatedAt },
    harnessControlHeaders()
  );
}

/** Deprecate one skill and preserve a review reason. */
export async function deprecateSkill(
  skillId: number,
  reason: string,
  expectedUpdatedAt: string
): Promise<SkillDetail> {
  return apiPost<SkillDetail>(
    `/api/skills/${skillId}/deprecate`,
    { reason, expected_updated_at: expectedUpdatedAt },
    harnessControlHeaders()
  );
}

/** Fetch the current Week 8 benchmark comparison view. */
export async function getBenchmarkComparison(): Promise<BenchmarkComparison> {
  return apiGet<BenchmarkComparison>("/api/benchmark-comparison");
}

/** Fetch aggregated historical evaluation metrics and recent run rows. */
export async function getEvaluationReports(): Promise<EvaluationReports> {
  return apiGet<EvaluationReports>("/api/evaluation-reports");
}

/** Fetch recent external MineCLIP evaluations for creative-task auditing. */
export async function getCreativeEvaluations(): Promise<CreativeEvaluation[]> {
  return apiGet<CreativeEvaluation[]>("/api/creative-evaluations");
}

/** Fetch the creative-task human review queue. */
export async function getHumanReviews(): Promise<HumanReview[]> {
  return apiGet<HumanReview[]>("/api/human-reviews");
}

/** Submit an optimistically locked authoritative creative-task review decision. */
export async function decideHumanReview(
  runId: string,
  decision: HumanReviewDecision,
  expectedVersion: number,
  notes: string
): Promise<HumanReview> {
  return apiPost<HumanReview>(`/api/human-reviews/${encodeURIComponent(runId)}/decision`, {
    decision,
    expected_version: expectedVersion,
    reviewer_id: "local-reviewer",
    notes,
    reason_codes: []
  });
}

/** Resolve a backend artifact path across same-origin and split dev-server deployments. */
export function getApiAssetUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return `${API_BASE_URL}${path}`;
}

/** Fetch safe quick-start defaults and credential-presence flags. */
export async function getLauncherConfig(): Promise<LauncherConfig> {
  return apiGet<LauncherConfig>("/api/launcher/config");
}

/** Search one page of trusted executable MineDojo tasks. */
export async function getLaunchTasks(filters: {
  query?: string;
  kind?: "all" | "programmatic" | "creative";
  category?: string;
  offset?: number;
  limit?: number;
}): Promise<LaunchTaskPage> {
  const params = launcherFilterParams(filters);
  params.set("offset", String(filters.offset ?? 0));
  params.set("limit", String(filters.limit ?? 40));
  return apiGet<LaunchTaskPage>(`/api/launcher/tasks?${params.toString()}`);
}

/** Fetch complete safe metadata for one selected executable task. */
export async function getLaunchTask(taskId: string): Promise<LaunchTaskDetail> {
  return apiGet<LaunchTaskDetail>(`/api/launcher/tasks/${encodeURIComponent(taskId)}`);
}

/** Draw one task from the same server-side filters used by the visible catalog. */
export async function getRandomLaunchTask(filters: {
  query?: string;
  kind?: "all" | "programmatic" | "creative";
  category?: string;
}): Promise<LaunchTaskSummary> {
  const params = launcherFilterParams(filters);
  return apiGet<LaunchTaskSummary>(`/api/launcher/tasks/random?${params.toString()}`);
}

/** Check Minecraft, RCON, client, model, and task/view readiness without launching. */
export async function preflightLaunch(request: LaunchRequest): Promise<LaunchPreflight> {
  return apiPost<LaunchPreflight>("/api/launcher/preflight", request);
}

/** Start one allowlisted task after the backend repeats all readiness checks. */
export async function startLaunchJob(
  request: LaunchRequest
): Promise<{ job: LaunchJob; preflight: LaunchPreflight }> {
  return apiPost<{ job: LaunchJob; preflight: LaunchPreflight }>(
    "/api/launcher/jobs",
    request,
    { "X-Harness-Control": "local-dashboard-v1" }
  );
}

/** Fetch launcher jobs known by the current backend process. */
export async function getLaunchJobs(): Promise<LaunchJob[]> {
  return apiGet<LaunchJob[]>("/api/launcher/jobs");
}

/** Poll one launcher-owned child process. */
export async function getLaunchJob(jobId: string): Promise<LaunchJob> {
  return apiGet<LaunchJob>(`/api/launcher/jobs/${encodeURIComponent(jobId)}`);
}

/** Read one incremental launch log segment. */
export async function getLaunchJobLogs(jobId: string, offset: number): Promise<LaunchJobLog> {
  return apiGet<LaunchJobLog>(
    `/api/launcher/jobs/${encodeURIComponent(jobId)}/logs?offset=${Math.max(0, offset)}`
  );
}

/** Cancel one active launcher-owned process group. */
export async function cancelLaunchJob(jobId: string): Promise<LaunchJob> {
  return apiPost<LaunchJob>(
    `/api/launcher/jobs/${encodeURIComponent(jobId)}/cancel`,
    undefined,
    { "X-Harness-Control": "local-dashboard-v1" }
  );
}

/** Search one page of live SQL-backed knowledge chunks. */
export async function getKnowledgeChunks(filters: {
  q?: string;
  source?: string;
  kind?: string;
  enabled?: boolean;
  offset?: number;
  limit?: number;
}): Promise<KnowledgeChunkPage> {
  const params = new URLSearchParams();
  if (filters.q?.trim()) {
    params.set("q", filters.q.trim());
  }
  if (filters.source) {
    params.set("source", filters.source);
  }
  if (filters.kind) {
    params.set("kind", filters.kind);
  }
  if (typeof filters.enabled === "boolean") {
    params.set("enabled", String(filters.enabled));
  }
  params.set("offset", String(filters.offset ?? 0));
  params.set("limit", String(filters.limit ?? 30));
  return apiGet<KnowledgeChunkPage>(`/api/knowledge-chunks?${params.toString()}`);
}

/** Create one knowledge chunk that becomes available to retrieve_docs. */
export async function createKnowledgeChunk(
  input: KnowledgeChunkCreate
): Promise<KnowledgeChunk> {
  return apiPost<KnowledgeChunk>("/api/knowledge-chunks", input, harnessControlHeaders());
}

/** Update one knowledge chunk using its current optimistic-lock version. */
export async function updateKnowledgeChunk(
  chunkId: string,
  input: KnowledgeChunkUpdate
): Promise<KnowledgeChunk> {
  return apiPatch<KnowledgeChunk>(
    `/api/knowledge-chunks/${encodeURIComponent(chunkId)}`,
    input,
    harnessControlHeaders()
  );
}

/** Archive one knowledge chunk without destructively deleting its audit history. */
export async function archiveKnowledgeChunk(
  chunkId: string,
  expectedVersion: number
): Promise<KnowledgeChunk> {
  return apiPost<KnowledgeChunk>(
    `/api/knowledge-chunks/${encodeURIComponent(chunkId)}/archive`,
    { expected_version: expectedVersion },
    harnessControlHeaders()
  );
}

/** Read the effective system prompt and action prompt registry snapshot. */
export async function getPromptConfigurations(): Promise<PromptConfigurations> {
  return apiGet<PromptConfigurations>("/api/prompt-configurations");
}

/** Choose whether running agents re-read saved prompt changes. */
export async function updatePromptHotReload(
  input: PromptHotReloadConfigurationUpdate
): Promise<PromptHotReloadConfiguration> {
  return apiPut<PromptHotReloadConfiguration>(
    "/api/prompt-configurations/hot-reload",
    input,
    harnessControlHeaders()
  );
}

/** Remove the hot-reload override and return to its code default. */
export async function resetPromptHotReload(
  expectedVersion: number
): Promise<PromptHotReloadConfiguration> {
  return apiDelete<PromptHotReloadConfiguration>(
    "/api/prompt-configurations/hot-reload",
    { expected_version: expectedVersion },
    harnessControlHeaders()
  );
}

/** Save a versioned system-prompt override. */
export async function updateSystemPromptConfiguration(
  input: SystemPromptConfigurationUpdate
): Promise<unknown> {
  return apiPut<unknown>(
    "/api/prompt-configurations/system",
    input,
    harnessControlHeaders()
  );
}

/** Remove the system-prompt override and return to the code default. */
export async function resetSystemPromptConfiguration(
  expectedVersion: number
): Promise<unknown> {
  return apiDelete<unknown>(
    "/api/prompt-configurations/system",
    { expected_version: expectedVersion },
    harnessControlHeaders()
  );
}

/** Save a prompt-facing override for one implemented action. */
export async function updateActionPromptConfiguration(
  actionType: string,
  input: ActionPromptConfigurationUpdate
): Promise<unknown> {
  return apiPut<unknown>(
    `/api/prompt-configurations/actions/${encodeURIComponent(actionType)}`,
    input,
    harnessControlHeaders()
  );
}

/** Remove one action prompt override and return to its code default. */
export async function resetActionPromptConfiguration(
  actionType: string,
  expectedVersion: number
): Promise<unknown> {
  return apiDelete<unknown>(
    `/api/prompt-configurations/actions/${encodeURIComponent(actionType)}`,
    { expected_version: expectedVersion },
    harnessControlHeaders()
  );
}

/** Execute a GET request and parse the JSON response body. */
async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  return parseJsonResponse<T>(response);
}

/** Execute a POST request with an optional JSON body. */
async function apiPost<T>(
  path: string,
  body?: object,
  headers?: Record<string, string>
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined
  });
  return parseJsonResponse<T>(response);
}

/** Execute a PATCH request with a JSON body. */
async function apiPatch<T>(
  path: string,
  body: object,
  headers?: Record<string, string>
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body)
  });
  return parseJsonResponse<T>(response);
}

/** Execute a PUT request with a JSON body. */
async function apiPut<T>(
  path: string,
  body: object,
  headers?: Record<string, string>
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body)
  });
  return parseJsonResponse<T>(response);
}

/** Execute a DELETE request with a JSON body for optimistic concurrency. */
async function apiDelete<T>(
  path: string,
  body: object,
  headers?: Record<string, string>
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body)
  });
  return parseJsonResponse<T>(response);
}

/** Required local-control header for every dashboard mutation. */
function harnessControlHeaders(): Record<string, string> {
  return { "X-Harness-Control": "local-dashboard-v1" };
}

/** Encode optional launch filters without sending empty query parameters. */
function launcherFilterParams(filters: {
  query?: string;
  kind?: "all" | "programmatic" | "creative";
  category?: string;
}): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.query?.trim()) {
    params.set("q", filters.query.trim());
  }
  if (filters.kind) {
    params.set("kind", filters.kind);
  }
  if (filters.category) {
    params.set("category", filters.category);
  }
  return params;
}

/** Decode a fetch response and surface backend error details. */
async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Request failed ${response.status}: ${detail}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}
