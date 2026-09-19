#!/usr/bin/env node
// Tier 2 probe: ask an installed harness's own resource loader which skills it
// discovers, without calling a model, making network requests, or starting a daemon.
//
// Usage: node harness_probe.mjs pi|prime CWD [--trust headless|approve|deny] [--expand TEXT]
//
// The agent directory and home come from the environment exactly as the harness
// reads them (PI_CODING_AGENT_DIR / PRIME_AGENT_CODING_AGENT_DIR, HOME). For pi,
// `headless` resolves project trust the way `pi -p` / `--mode json` does without -a
// (main.js createRuntime + core/project-trust.js resolveProjectTrusted, no UI),
// `approve` is -a and `deny` is -na. prime has no project trust.
// Prints one JSON document on stdout.

import { existsSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const PACKAGES = {
  pi: process.env.MOL_PI_PACKAGE ?? "/usr/lib/node_modules/@earendil-works/pi-coding-agent",
  prime: process.env.MOL_PRIME_PACKAGE ?? "/usr/lib/node_modules/prime-agent",
};

function fail(message) {
  process.stderr.write(`harness_probe: ${message}\n`);
  process.exit(2);
}

const [harness, cwdArgument, ...rest] = process.argv.slice(2);
if (!(harness in PACKAGES) || !cwdArgument) {
  fail("usage: harness_probe.mjs pi|prime CWD [--trust headless|approve|deny] [--expand TEXT]");
}
let trustMode = "headless";
let expandText;
for (let index = 0; index < rest.length; index++) {
  if (rest[index] === "--trust") trustMode = rest[++index];
  else if (rest[index] === "--expand") expandText = rest[++index];
  else fail(`unknown argument: ${rest[index]}`);
}
if (!["headless", "approve", "deny"].includes(trustMode)) fail(`bad --trust: ${trustMode}`);

const packageDir = PACKAGES[harness];
const packageJson = JSON.parse(readFileSync(join(packageDir, "package.json"), "utf8"));
const cwd = resolve(cwdArgument);
const importFile = (path) => import(pathToFileURL(path).href);

// prime's CLI runs dist/bundle/cli.js; its library classes live in content-hashed
// chunks, so find the chunk whose export list names every wanted symbol.
function bundleChunkExporting(names) {
  const bundle = join(packageDir, "dist", "bundle");
  for (const file of readdirSync(bundle).filter((name) => name.endsWith(".js")).sort()) {
    const text = readFileSync(join(bundle, file), "utf8");
    const exportBlock = text.slice(text.lastIndexOf("\nexport {"));
    if (names.every((name) => new RegExp(`\\b${name}\\b`).test(exportBlock))) return join(bundle, file);
  }
  fail(`no bundle chunk exports ${names.join(", ")}`);
}

let DefaultResourceLoader, SettingsManager, AgentSession, loaderModule, sessionModule;
let projectTrusted = null;
let trustSource = "prime has no project trust";
let agentDir;

if (harness === "pi") {
  loaderModule = join(packageDir, "dist", "index.js");
  sessionModule = loaderModule;
  ({ DefaultResourceLoader, SettingsManager, AgentSession } = await importFile(loaderModule));
  const { getAgentDir } = await importFile(join(packageDir, "dist", "config.js"));
  const { hasTrustRequiringProjectResources, ProjectTrustStore } = await importFile(
    join(packageDir, "dist", "core", "trust-manager.js"));
  const { resolveProjectTrusted } = await importFile(join(packageDir, "dist", "core", "project-trust.js"));
  agentDir = getAgentDir();
  if (trustMode === "approve" || trustMode === "deny") {
    projectTrusted = trustMode === "approve";
    trustSource = trustMode === "approve" ? "-a/--approve" : "-na/--no-approve";
  } else if (!hasTrustRequiringProjectResources(cwd)) {
    projectTrusted = true;
    trustSource = "no trust-requiring project resources";
  } else {
    const startup = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
    projectTrusted = await resolveProjectTrusted({
      cwd,
      trustStore: new ProjectTrustStore(agentDir),
      trustOverride: undefined,
      defaultProjectTrust: startup.getDefaultProjectTrust(),
      extensionsResult: undefined,
      projectTrustContext: { hasUI: false },
    });
    trustSource = "headless resolution (no UI)";
  }
} else {
  loaderModule = bundleChunkExporting(["DefaultResourceLoader", "SettingsManager"]);
  sessionModule = bundleChunkExporting(["AgentSession"]);
  ({ DefaultResourceLoader, SettingsManager } = await importFile(loaderModule));
  ({ AgentSession } = await importFile(sessionModule));
  const envDir = process.env.PRIME_AGENT_CODING_AGENT_DIR;
  agentDir = envDir ? envDir.replace(/^~(?=$|\/)/, homedir()) : join(homedir(), ".prime", "agent");
}

const settingsManager = harness === "pi"
  ? SettingsManager.create(cwd, agentDir, { projectTrusted })
  : SettingsManager.create(cwd, agentDir);
const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager, noExtensions: true });
await loader.reload();
const { skills, diagnostics } = loader.getSkills();

const realPath = (path) => { try { return realpathSync(path); } catch { return null; } };
const result = {
  harness,
  version: packageJson.version,
  loaderModule,
  cwd,
  home: process.env.HOME ?? homedir(),
  agentDir,
  projectTrusted,
  trustSource,
  skills: skills.map((skill) => ({
    name: skill.name,
    filePath: skill.filePath,
    baseDir: skill.baseDir,
    scope: skill.sourceInfo?.scope ?? null,
    source: skill.sourceInfo?.source ?? null,
    realPath: realPath(skill.filePath),
  })),
  diagnostics,
};

if (expandText !== undefined) {
  // The harness's own /skill: expansion, bound to this loader; no session or model.
  const errors = [];
  const host = { resourceLoader: loader, _extensionRunner: { emitError: (error) => errors.push(error) } };
  result.expansion = AgentSession.prototype._expandSkillCommand.call(host, expandText);
  result.expansionModule = sessionModule;
  result.expansionErrors = errors;
}

result.agentDirExists = existsSync(agentDir);
process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
