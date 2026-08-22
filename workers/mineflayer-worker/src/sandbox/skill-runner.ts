/** Result returned by future sandboxed code-skill validation runs. */
export interface SkillSandboxResult {
  ok: boolean;
  logs: string[];
  error?: string;
}

/** Placeholder boundary for executing an untrusted skill candidate in isolation. */
export async function runSkillCandidate(): Promise<SkillSandboxResult> {
  return {
    ok: false,
    logs: [],
    error: "Skill sandbox is not implemented yet."
  };
}
