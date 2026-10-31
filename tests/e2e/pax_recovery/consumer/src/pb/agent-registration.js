import { readFileSync } from 'node:fs';
import { basename, isAbsolute, resolve } from 'node:path';
import { validateNorthStar } from './north-star.js';

// The installed OOS registration generation; server-infra keeps `current` on the deployed document.
export const DEFAULT_AGENT_CONNECTIONS_FILE = '/var/lib/oos-agent-config/current/agent-connections.json';

export const DEFAULT_OWNER_PROFILES = Object.freeze({
  magnus: 'magnus', pippa: 'pippa', noor: 'noor', otto: 'otto', pax: 'pax',
});

const TOKEN = /^[a-z0-9][a-z0-9_-]*$/u;
const SERVER = /^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$/u;
const PERSONAL = /^david-os(?:-|$)/u;
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Validate only consumed source bindings; OOS owns full-document validation and grants. */
export function loadAgentRegistration({ filename, readFile = readFileSync } = {}) {
  if (typeof filename !== 'string' || !isAbsolute(filename) || /[\0\r\n]/u.test(filename)) {
    throw new TypeError('pb_agent_registration_filename_invalid');
  }
  const document = JSON.parse(readFile(filename, 'utf8'));
  if (!record(document) || document.schema_version !== 'oos.agent_connections.v1' || !Array.isArray(document.connections)) {
    throw new TypeError('pb_agent_registration_schema_invalid');
  }
  const ids = new Set();
  const bindings = new Map();
  const workspaceProjects = new Map();
  const repositories = new Set();
  const agentProfiles = new Map();
  const profileAgents = new Map();
  const disabled = new Set();
  const withoutConnector = new Set();
  const activeAgents = new Set();
  const northStars = new Map();
  const nativePaths = new Map();
  const connectors = new Map();
  for (const connection of document.connections) {
    if (!record(connection) || ![connection.id, connection.agent_id, connection.repo].every(value => typeof value === 'string' && TOKEN.test(value)) ||
        !record(connection.hermes) || typeof connection.hermes.profile !== 'string' || !TOKEN.test(connection.hermes.profile) ||
        typeof connection.workspace !== 'string' || !isAbsolute(connection.workspace) || /[\0\r\n]/u.test(connection.workspace) ||
        resolve(connection.workspace) !== connection.workspace || (connection.enabled !== undefined && typeof connection.enabled !== 'boolean')) {
      throw new TypeError('pb_agent_registration_binding_invalid');
    }
    const { id, agent_id: agentId, repo, workspace, pb_connector_ref: connector } = connection;
    const profile = connection.hermes.profile;
    if (PERSONAL.test(repo) || workspace.split('/').some(part => PERSONAL.test(part))) {
      throw new TypeError('pb_agent_registration_personal_project_excluded');
    }
    const prefix = `hermes:${profile}:mcp:`;
    const hasConnector = connector !== null && connector !== undefined;
    if (hasConnector && (typeof connector !== 'string' || !connector.startsWith(prefix) || !SERVER.test(connector.slice(prefix.length)))) {
      throw new TypeError('pb_agent_registration_connector_invalid');
    }
    const key = `${agentId}:${repo}`;
    if (connection.north_star !== undefined) northStars.set(key, validateNorthStar(connection.north_star, connection));
    const cronWorkspaces = connection.hermes.cron_workspaces;
    const routes = Array.isArray(connection.hermes.task_route) ? connection.hermes.task_route : [connection.hermes.task_route];
    if (cronWorkspaces !== undefined && (!record(cronWorkspaces) || Object.entries(cronWorkspaces).some(([job, path]) =>
      !/^[A-Za-z0-9_.-]+$/u.test(job) || !routes.includes(`hermes:${profile}:cron:${job}`) ||
      typeof path !== 'string' || !isAbsolute(path) || /[\0\r\n]/u.test(path) || resolve(path) !== path ||
      path.split('/').some(part => PERSONAL.test(part))))) throw new TypeError('pb_agent_registration_cron_workspace_invalid');
    nativePaths.set(key, Object.freeze([...new Set([workspace, ...Object.values(cronWorkspaces ?? {})])]));
    if (!hasConnector) withoutConnector.add(key);
    if (connection.enabled === false) disabled.add(key);
    else if (hasConnector) {
      activeAgents.add(agentId);
      const entry = connectors.get(agentId) ?? { agent_id: agentId, profile, server: connector.slice(prefix.length), cron_jobs: new Set() };
      if (entry.server !== connector.slice(prefix.length)) throw new TypeError('pb_agent_registration_identity_conflict');
      for (const route of routes) if (typeof route === 'string' && route.startsWith(`hermes:${profile}:cron:`)) entry.cron_jobs.add(route.slice(`hermes:${profile}:cron:`.length));
      connectors.set(agentId, entry);
    }
    if (ids.has(id) || bindings.has(key)) throw new TypeError('pb_agent_registration_duplicate_binding');
    if ((workspaceProjects.has(workspace) && workspaceProjects.get(workspace) !== repo) ||
        (agentProfiles.has(agentId) && agentProfiles.get(agentId) !== profile) ||
        (profileAgents.has(profile) && profileAgents.get(profile) !== agentId)) {
      throw new TypeError('pb_agent_registration_identity_conflict');
    }
    ids.add(id);
    workspaceProjects.set(workspace, repo);
    repositories.add(repo);
    agentProfiles.set(agentId, profile);
    profileAgents.set(profile, agentId);
    bindings.set(key, Object.freeze({ agent_id: agentId, repo, workspace, profile }));
  }
  const ownerProfiles = Object.freeze(Object.fromEntries([...agentProfiles].filter(([agent]) => activeAgents.has(agent))));
  return Object.freeze({
    ownerProfiles,
    /** Enabled agents with a PB connector: Hermes profile, ACE MCP server and the cron jobs on their registered task route. */
    activeAgents() {
      return [...connectors.values()].map(entry => Object.freeze({ ...entry, cron_jobs: Object.freeze([...entry.cron_jobs].sort()) }));
    },
    /** Company repository of a canonical Git root: a registered workspace, or a clone named after a registered repo. */
    businessRepository(root) {
      if (typeof root !== 'string' || !isAbsolute(root) || resolve(root) !== root || root.split('/').some(part => PERSONAL.test(part))) return null;
      return workspaceProjects.get(root) ?? (repositories.has(basename(root)) ? basename(root) : null);
    },
    northStar(agentId, canonicalWorkspace) {
      const binding = this.resolve(agentId, canonicalWorkspace);
      return binding ? structuredClone(northStars.get(`${agentId}:${binding.repo}`) ?? null) : null;
    },
    nativeWorkspaces(agentId, canonicalWorkspace) {
      const binding = this.resolve(agentId, canonicalWorkspace);
      return binding ? [...nativePaths.get(`${agentId}:${binding.repo}`)] : [];
    },
    resolve(agentId, canonicalWorkspace) {
      if (typeof agentId !== 'string' || typeof canonicalWorkspace !== 'string') return null;
      const repo = workspaceProjects.get(canonicalWorkspace);
      if (repo === undefined || disabled.has(`${agentId}:${repo}`) || withoutConnector.has(`${agentId}:${repo}`)) return null;
      const binding = bindings.get(`${agentId}:${repo}`);
      return binding?.workspace === canonicalWorkspace ? binding : null;
    },
  });
}
