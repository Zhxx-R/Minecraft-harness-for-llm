import {
  ArrowRight,
  Bot,
  ClipboardCheck,
  Database,
  FileBarChart,
  Library,
  Play,
  Radio,
  Settings2
} from "lucide-react";
import type { ReactNode } from "react";
import {
  isActiveLaunchJobStatus,
  type AgentSummary,
  type HumanReview,
  type LaunchJob,
  type SkillSummary
} from "../../api/client";
import type { MainSection } from "../../app/sections";

/** Data required to summarize the harness from the portal home. */
interface PortalHomeProps {
  agents: AgentSummary[];
  skills: SkillSummary[];
  reviews: HumanReview[];
  launchJobs: LaunchJob[];
  onNavigate: (section: MainSection) => void;
}

/** One clickable operational workspace advertised by the portal. */
interface PortalEntry {
  section: MainSection;
  title: string;
  description: string;
  metric: string;
  icon: ReactNode;
  accent: "green" | "blue" | "amber" | "red" | "gray";
  primary?: boolean;
}

/** Entry portal that routes users into launch, review, audit, and report workflows. */
export function PortalHome(props: PortalHomeProps) {
  const activeJobs = props.launchJobs.filter((job) =>
    isActiveLaunchJobStatus(job.status)
  ).length;
  const onlineAgents = props.agents.filter((agent) => agent.presence === "online").length;
  const pendingSkills = props.skills.filter((skill) =>
    ["draft", "validated", "staged"].includes(skill.status)
  ).length;
  const pendingReviews = props.reviews.filter((review) =>
    ["awaiting_review", "awaiting_human_review"].includes(review.status)
  ).length;
  const runCount = props.agents.reduce((total, agent) => total + agent.run_count, 0);
  const entries: PortalEntry[] = [
    {
      section: "quick-start",
      title: "Quick Start",
      description: "Select an executable MineDojo task, verify the local game environment, and launch one audited run.",
      metric: activeJobs ? `${activeJobs} active launch` : "3,141 executable tasks",
      icon: <Play size={21} aria-hidden="true" />,
      accent: "green",
      primary: true
    },
    {
      section: "runtime",
      title: "Agent Runtime",
      description: "Inspect every ReAct round, prompt, observation, action, model call, and runtime error.",
      metric: `${onlineAgents}/${props.agents.length} online`,
      icon: <Radio size={21} aria-hidden="true" />,
      accent: "blue"
    },
    {
      section: "skills",
      title: "Skill Review",
      description: "Review strategy memories, evidence, versions, promotion state, and deprecation history.",
      metric: `${pendingSkills} awaiting review`,
      icon: <Library size={21} aria-hidden="true" />,
      accent: "amber"
    },
    {
      section: "knowledge",
      title: "Knowledge Base",
      description: "Manage versioned local document chunks used by the agent's retrieve_docs action.",
      metric: "Live retrieval corpus",
      icon: <Database size={21} aria-hidden="true" />,
      accent: "blue"
    },
    {
      section: "configuration",
      title: "Prompt Configuration",
      description: "Edit the system prompt and action descriptions with next-decision hot reload.",
      metric: "System + action registry",
      icon: <Settings2 size={21} aria-hidden="true" />,
      accent: "gray"
    },
    {
      section: "creative",
      title: "Creative Task Review",
      description: "Judge final video or screenshot evidence, then expand the full agent trajectory when needed.",
      metric: `${pendingReviews} pending`,
      icon: <ClipboardCheck size={21} aria-hidden="true" />,
      accent: "red"
    },
    {
      section: "reports",
      title: "Evaluation Reports",
      description: "Compare harness modes across success rate, runtime stability, token use, and cost.",
      metric: `${runCount} audited runs`,
      icon: <FileBarChart size={21} aria-hidden="true" />,
      accent: "gray"
    }
  ];

  return (
    <section className="portal-home">
      <header className="portal-heading">
        <div className="portal-mark" aria-hidden="true">
          <Bot size={24} />
        </div>
        <div>
          <span>Operations Portal</span>
          <h1>Minecraft Agent Harness</h1>
          <p>Launch tasks, inspect runtime evidence, and govern learned capabilities.</p>
        </div>
      </header>
      <div className="portal-entry-grid">
        {entries.map((entry) => (
          <button
            className={`portal-entry ${entry.primary ? "primary" : ""} accent-${entry.accent}`}
            key={entry.section}
            type="button"
            onClick={() => props.onNavigate(entry.section)}
          >
            <span className="portal-entry-icon">{entry.icon}</span>
            <span className="portal-entry-copy">
              <strong>{entry.title}</strong>
              <small>{entry.description}</small>
            </span>
            <span className="portal-entry-footer">
              <span>{entry.metric}</span>
              <ArrowRight size={17} aria-hidden="true" />
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
