import { existsSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

export type ReadOnlyShellPolicy = {
  schema_version: number;
  max_timeout_ms: number;
  allowed_commands: string[];
  git_subcommands: string[];
  blocked_options: Record<string, string[]>;
};

export type ShellDecision = {
  allowed: boolean;
  reason?: string;
  command?: string;
  timeout_ms?: number;
};

function inside(root: string, target: string): boolean {
  const rel = relative(resolve(root), resolve(target));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function fail(reason: string): ShellDecision {
  return { allowed: false, reason };
}

function tokenizeSimple(command: string): string[] | null {
  const out: string[] = [];
  let token = "";
  let quote: "'" | '"' | null = null;
  let tokenStarted = false;

  for (let i = 0; i < command.length; i += 1) {
    const ch = command[i];
    if (quote) {
      if (ch === quote) {
        quote = null;
        tokenStarted = true;
      } else {
        token += ch;
        tokenStarted = true;
      }
      continue;
    }

    if (ch === "'" || ch === '"') {
      quote = ch;
      tokenStarted = true;
      continue;
    }

    if (/\s/.test(ch)) {
      if (tokenStarted) {
        out.push(token);
        token = "";
        tokenStarted = false;
      }
      continue;
    }

    token += ch;
    tokenStarted = true;
  }

  if (quote) return null;
  if (tokenStarted) out.push(token);
  return out;
}

function splitAllowedChain(command: string): { left?: string; right: string } | ShellDecision {
  let quote: "'" | '"' | null = null;
  let andIndex = -1;

  for (let i = 0; i < command.length; i += 1) {
    const ch = command[i];
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }

    if (ch === "\n" || ch === "\r") return fail("multiline shell commands are not allowed");
    if (ch === "$" || ch === "`" || ch === "\\") return fail("shell expansion/escaping is not allowed");
    if ([";", "|", ">", "<", "(", ")", "{", "}", "*", "?", "[", "]"].includes(ch)) {
      return fail(`unquoted shell operator/glob '${ch}' is not allowed`);
    }
    if (ch === "&") {
      if (command[i + 1] !== "&") return fail("background execution is not allowed");
      if (andIndex >= 0) return fail("only one leading 'cd ... && <read command>' chain is allowed");
      andIndex = i;
      i += 1;
    }
  }

  if (quote) return fail("unterminated shell quote");
  if (andIndex < 0) return { right: command.trim() };
  return {
    left: command.slice(0, andIndex).trim(),
    right: command.slice(andIndex + 2).trim(),
  };
}

function optionName(token: string): string {
  const eq = token.indexOf("=");
  return eq >= 0 ? token.slice(0, eq) : token;
}

function optionValue(token: string): string | null {
  const eq = token.indexOf("=");
  return eq >= 0 ? token.slice(eq + 1) : null;
}

function looksLikeExplicitOutsidePath(token: string, root: string, cwd: string): boolean {
  if (!token || token === ".") return false;
  if (token.startsWith("~")) return true;

  const candidate = isAbsolute(token) ? resolve(token) : resolve(cwd, token);
  if (!inside(root, candidate) && (isAbsolute(token) || token === ".." || token.startsWith("../") || token.includes("/../"))) {
    return true;
  }
  // If the token names an existing path, resolve symlinks as well. Tokens that are
  // patterns/format strings normally do not exist and therefore fall through.
  try {
    if (existsSync(candidate) && !inside(root, realpathSync(candidate))) return true;
  } catch {
    return true;
  }
  return false;
}

function validatePathScope(tokens: string[], root: string, cwd: string): ShellDecision | null {
  for (const token of tokens) {
    if (!token) continue;
    if (token.startsWith("-")) {
      const value = optionValue(token);
      if (value && looksLikeExplicitOutsidePath(value, root, cwd)) {
        return fail(`option path escapes research_root: ${value}`);
      }
      continue;
    }
    if (looksLikeExplicitOutsidePath(token, root, cwd)) {
      return fail(`path escapes research_root: ${token}`);
    }
  }
  return null;
}

function hasBlockedOption(command: string, args: string[], policy: ReadOnlyShellPolicy): string | null {
  const blocked = new Set(policy.blocked_options[command] ?? []);
  for (const arg of args) {
    if (blocked.has(optionName(arg))) return optionName(arg);
  }
  return null;
}

function validateFind(args: string[]): ShellDecision | null {
  const forbidden = new Set([
    "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf",
  ]);
  for (const arg of args) {
    if (forbidden.has(arg)) return fail(`find action '${arg}' is not allowed`);
  }
  return null;
}

function validateUniq(args: string[]): ShellDecision | null {
  const positional = args.filter((x) => !x.startsWith("-"));
  if (positional.length > 1) {
    return fail("uniq with an OUTPUT operand is not allowed; use at most one input file");
  }
  return null;
}

function validateGit(args: string[], policy: ReadOnlyShellPolicy): ShellDecision | null {
  if (args.length === 0) return fail("git requires an allowlisted read-only subcommand");

  let i = 0;
  const allowedGlobal = new Set(["--no-pager", "--no-optional-locks"]);
  while (i < args.length && args[i].startsWith("-")) {
    const opt = optionName(args[i]);
    if (!allowedGlobal.has(opt)) {
      return fail(`git global option '${opt}' is not allowed`);
    }
    i += 1;
  }

  const subcommand = args[i];
  if (!subcommand || !policy.git_subcommands.includes(subcommand)) {
    return fail(`git subcommand '${subcommand ?? ""}' is not allowlisted`);
  }
  const rest = args.slice(i + 1);

  if (["diff", "show", "log"].includes(subcommand)) {
    for (const arg of rest) {
      const opt = optionName(arg);
      if (["--ext-diff", "--textconv", "--show-signature"].includes(opt)) {
        return fail(`git ${subcommand} option '${opt}' may execute external helpers and is blocked`);
      }
    }
  }

  if (subcommand === "branch") {
    const safeBranchOptions = new Set([
      "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "--show-current",
    ]);
    for (const arg of rest) {
      if (!arg.startsWith("-") || !safeBranchOptions.has(optionName(arg))) {
        return fail("git branch is inspection-only; branch creation/deletion/rename is blocked");
      }
    }
  }

  return null;
}

function validateSimple(
  tokens: string[],
  root: string,
  cwd: string,
  policy: ReadOnlyShellPolicy,
): ShellDecision {
  if (tokens.length === 0) return fail("empty shell command");
  const command = tokens[0];
  const args = tokens.slice(1);
  if (!policy.allowed_commands.includes(command)) {
    return fail(`command '${command}' is not in the read-only inspection allowlist`);
  }

  const blocked = hasBlockedOption(command, args, policy);
  if (blocked) return fail(`${command} option '${blocked}' is blocked because it can modify files or execute helpers`);

  if (command === "find") {
    const decision = validateFind(args);
    if (decision) return decision;
  }
  if (command === "uniq") {
    const decision = validateUniq(args);
    if (decision) return decision;
  }
  if (command === "git") {
    const decision = validateGit(args, policy);
    if (decision) return decision;
  }

  const scopeDecision = validatePathScope(args, root, cwd);
  if (scopeDecision) return scopeDecision;

  let normalized = tokens.map((t) => (/\s/.test(t) ? JSON.stringify(t) : t)).join(" ");
  if (command === "git") {
    // Avoid pagers and optional index refresh locks/writes during inspection.
    const withoutInjected = args.filter((x) => x !== "--no-pager" && x !== "--no-optional-locks");
    normalized = ["git", "--no-pager", "--no-optional-locks", ...withoutInjected]
      .map((t) => (/\s/.test(t) ? JSON.stringify(t) : t))
      .join(" ");
  }
  return { allowed: true, command: normalized };
}

export function evaluateReadOnlyShell(
  command: string,
  researchRoot: string,
  cwd: string,
  policy: ReadOnlyShellPolicy,
  requestedTimeoutMs?: number,
): ShellDecision {
  if (!inside(researchRoot, cwd)) {
    return fail(`current working directory is outside research_root: ${cwd}`);
  }

  const split = splitAllowedChain(command);
  if ("allowed" in split) return split;
  const rightTokens = tokenizeSimple(split.right);
  if (!rightTokens) return fail("could not parse shell command safely");

  let effectiveCwd = cwd;
  let prefix = "";
  if (split.left !== undefined) {
    const leftTokens = tokenizeSimple(split.left);
    if (!leftTokens || leftTokens.length !== 2 || leftTokens[0] !== "cd") {
      return fail("the only allowed command chain is 'cd <directory> && <read-only command>'");
    }
    const target = resolve(cwd, leftTokens[1]);
    if (!inside(researchRoot, target)) return fail(`cd target escapes research_root: ${leftTokens[1]}`);
    effectiveCwd = target;
    prefix = `cd ${/\s/.test(leftTokens[1]) ? JSON.stringify(leftTokens[1]) : leftTokens[1]} && `;
  } else if (rightTokens[0] === "cd") {
    if (rightTokens.length !== 2) return fail("standalone cd requires exactly one directory argument");
    const target = resolve(cwd, rightTokens[1]);
    if (!inside(researchRoot, target)) return fail(`cd target escapes research_root: ${rightTokens[1]}`);
    return {
      allowed: true,
      command: `cd ${/\s/.test(rightTokens[1]) ? JSON.stringify(rightTokens[1]) : rightTokens[1]}`,
      timeout_ms: Math.min(requestedTimeoutMs ?? policy.max_timeout_ms, policy.max_timeout_ms),
    };
  }

  const decision = validateSimple(rightTokens, researchRoot, effectiveCwd, policy);
  if (!decision.allowed) return decision;
  return {
    allowed: true,
    command: `${prefix}${decision.command}`,
    timeout_ms: Math.min(requestedTimeoutMs ?? policy.max_timeout_ms, policy.max_timeout_ms),
  };
}
