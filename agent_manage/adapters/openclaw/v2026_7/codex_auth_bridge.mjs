// Deliberately use the server SDK: it owns SQLite, inheritance and refresh.
// Secret input/output stays on private process pipes, never LocalRunner logs.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const GLOBAL = 'openai:agent-manage-global';
const sdk = await import(pathToFileURL(path.join(input.package, 'dist/plugin-sdk/provider-auth.js')));
const oauth = c => c?.type === 'oauth' && ['openai', 'openai-codex'].includes(c.provider);
const emit = data => process.stdout.write(JSON.stringify(data) + '\n');
const options = {filterExternalAuthProfiles: false, syncExternalCli: false, preserveOrderProfileIds: [GLOBAL]};

async function local(dir, updater = () => false) {
  const store = await sdk.updateAuthProfileStoreWithLock({agentDir: dir, updater, saveOptions: options});
  if (!store) throw new Error('auth_store_update_failed');
  return store;
}

async function fetchCodexModels() {
  const failure = code => ({catalog_error: {code}});
  let stage = 'sdk_unavailable';
  let signal;
  try {
    const runtime = await import(pathToFileURL(path.join(input.package, 'dist/plugin-sdk/provider-auth-runtime.js')));
    const cfg = JSON.parse(fs.readFileSync(process.env.OPENCLAW_CONFIG_PATH, 'utf8'));
    stage = 'oauth_unavailable';
    const profile = {provider: 'openai', cfg, agentDir: input.main, profileId: GLOBAL};
    const auth = await runtime.resolveApiKeyForProvider({...profile, lockedProfile: true, modelApi: 'openai-chatgpt-responses'});
    if (auth.mode !== 'oauth' || auth.profileId !== GLOBAL || !auth.apiKey) return failure(stage);
    const {accountId} = runtime.resolveProviderAuthProfileMetadata(profile);
    stage = 'network_error';
    signal = AbortSignal.timeout(10000);
    // Match the model discovery endpoint used by the supported July server series.
    const response = await fetch('https://chatgpt.com/backend-api/codex/models?client_version=1.0.0', {
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${auth.apiKey}`,
        ...(accountId ? {'ChatGPT-Account-ID': accountId} : {}),
      },
      redirect: 'error',
      signal,
    });
    if (!response.ok) return {catalog_error: {code: 'http_error', http_status: response.status}};
    if (!response.body) return failure('invalid_response');
    const chunks = [];
    let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > 4 * 1024 * 1024) return failure('response_too_large');
      chunks.push(chunk);
    }
    stage = 'invalid_response';
    const {models} = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!Array.isArray(models)) return failure(stage);
    const visibleModels = models
      .filter(row => row && (!row.visibility || row.visibility === 'list')
        && (row.show_in_picker ?? row.showInPicker) !== false)
      .map(row => ({
        id: row.slug ?? row.id,
        name: row.display_name ?? row.displayName,
        input: row.input_modalities ?? row.inputModalities,
      }));
    return {models: visibleModels};
  } catch {
    return failure(signal?.aborted ? 'timeout' : stage);
  }
}

function health(c) {
  if (!oauth(c) || !c.access?.trim()) return 'missing';
  if (sdk.hasUsableOAuthCredential(c, {refreshMarginMs: 0})) return 'valid';
  return c.refresh?.trim() ? 'refreshable' : 'expired';
}
function summarize(c) {
  if (!c) return null;
  const identity = sdk.resolveOpenAICodexAuthIdentity({access: c.access, accountId: c.accountId});
  return {email: c.email || identity.email || null, account_id: c.accountId || identity.accountId || null,
    plan: c.chatgptPlanType || identity.chatgptPlanType || null, expires_at: c.expires || null};
}
function clear(store) {
  const removed = Object.entries(store.profiles).filter(([, c]) => oauth(c)).map(([id]) => id);
  for (const id of removed) {
    delete store.profiles[id];
    if (store.usageStats) delete store.usageStats[id];
  }
  for (const provider of ['openai', 'openai-codex']) {
    if (store.order?.[provider]) {
      store.order[provider] = store.order[provider].filter(id => !removed.includes(id) && id !== GLOBAL);
      if (!store.order[provider].length) delete store.order[provider];
    }
    if (removed.includes(store.lastGood?.[provider]) || store.lastGood?.[provider] === GLOBAL)
      delete store.lastGood[provider];
  }
  return removed.length;
}
function restoreOAuth(store, saved) {
  clear(store);
  for (const [id, c] of Object.entries(saved.profiles)) if (oauth(c)) {
    store.profiles[id] = c;
    if (saved.usageStats?.[id]) (store.usageStats ??= {})[id] = saved.usageStats[id];
  }
  for (const provider of ['openai', 'openai-codex']) {
    for (const field of ['order', 'lastGood']) {
      if (saved[field]?.[provider] !== undefined) (store[field] ??= {})[provider] = saved[field][provider];
      else if (store[field]) delete store[field][provider];
    }
  }
}
function staticOrders(backup) {
  return backup.map(({dir, store}) => ({dir, order: Object.fromEntries(
    ['openai', 'openai-codex'].map(provider => [provider,
      (store.order?.[provider] || []).filter(id => store.profiles[id] && !oauth(store.profiles[id]))]))}));
}

try {
  // A July maintenance release must still expose the native auth contract.
  // Check before touching any credential stores.
  for (const name of ['updateAuthProfileStoreWithLock', 'hasUsableOAuthCredential',
    'resolveOpenAICodexAuthIdentity', 'buildOpenAICodexCredentialExtra']) {
    if (typeof sdk[name] !== 'function') {
      emit({event: 'error', error_code: 'CODEX_SDK_INCOMPATIBLE'});
      process.exit(1);
    }
  }
  if (input.action === 'device-login') {
    const {loginOpenAICodexDeviceCode} = await import(pathToFileURL(path.join(input.package, 'dist/extensions/openai/openai-chatgpt-device-code.js')));
    const credential = await loginOpenAICodexDeviceCode({
      fetchFn: (url, opts) => {
        const job = JSON.parse(fs.readFileSync(input.job_path, 'utf8'));
        if (job.id !== input.job_id || !['starting', 'pending'].includes(job.status)) throw new Error('cancelled');
        return fetch(url, {...opts, signal: AbortSignal.timeout(20000)});
      },
      onVerification: async data => emit({event: 'verification', verification_url: data.verificationUrl,
        user_code: data.userCode, expires_at: Date.now() + data.expiresInMs}),
      onProgress: () => {},
    });
    emit({event: 'credential', credential});
  } else if (input.action === 'inspect') {
    const main = await local(input.main);
    const shared = main.profiles[GLOBAL];
    const existing = Object.values(main.profiles).find(c => ['valid', 'refreshable'].includes(health(c)));
    const agents = [];
    for (const agent of input.agents) {
      const store = await local(agent.dir);
      // A local account can override the main-store account. Do not claim global coverage then.
      const overrides = Object.entries(store.profiles).some(([id, c]) => oauth(c) && id !== GLOBAL)
        || (agent.dir !== input.main && !!store.profiles[GLOBAL]);
      const order = store.order?.openai || input.order || main.order?.openai || [];
      agents.push({id: agent.id, shared_auth: !!shared && !overrides && order[0] === GLOBAL});
    }
    emit({auth_status: health(shared), logged_in: ['valid', 'refreshable'].includes(health(shared)) && agents.every(a => a.shared_auth),
      canonical_auth_available: !!existing, account: summarize(shared), agents});
  } else if (input.action === 'list-models') {
    emit(await fetchCodexModels());
  } else if (input.action === 'apply') {
    let credential = input.credential;
    if (!credential) credential = Object.values((await local(input.main)).profiles)
      .find(c => ['valid', 'refreshable'].includes(health(c)));
    if (!credential?.access || !credential?.refresh) throw new Error('missing_oauth_credential');
    const identity = sdk.resolveOpenAICodexAuthIdentity({access: credential.access, accountId: credential.accountId});
    credential = {...credential, ...sdk.buildOpenAICodexCredentialExtra(identity),
      type: 'oauth', provider: 'openai', ...(identity.email ? {email: identity.email} : {})};
    const dirs = [...new Set([input.main, ...input.agents.map(a => a.dir)])];
    const backup = [];
    for (const dir of dirs) {
      const store = await local(dir);
      if (store.profiles[GLOBAL] && !oauth(store.profiles[GLOBAL])) throw new Error('reserved_profile_conflict');
      backup.push({dir, store: structuredClone(store)});
    }
    // This private recovery file survives a process crash between separate native transactions.
    const backupFd = fs.openSync(input.backup_path, 'wx', 0o600);
    try {
      fs.writeFileSync(backupFd, JSON.stringify(backup));
      fs.fsyncSync(backupFd);
    } finally {
      fs.closeSync(backupFd);
    }
    try {
      for (const dir of dirs) await local(dir, store => {
        clear(store);
        if (dir === input.main) store.profiles[GLOBAL] = credential;
        store.order ??= {};
        store.order.openai = [GLOBAL];
        return true;
      });
      emit({credentials_shared: true, account: summarize(credential), auth_order_restore: staticOrders(backup)});
    } catch {
      for (const {dir, store: saved} of backup) await local(dir, store => {
        restoreOAuth(store, saved);
        return true;
      });
      throw new Error('auth_store_update_failed');
    }
  } else if (input.action === 'rollback') {
    const backup = JSON.parse(fs.readFileSync(input.backup_path, 'utf8'));
    for (const {dir, store: saved} of backup) await local(dir, store => {
      restoreOAuth(store, saved);
      return true;
    });
    emit({rolled_back: true});
  } else if (input.action === 'logout') {
    let removed = 0;
    const restore = [...(input.restore_orders || [])];
    if (fs.existsSync(input.backup_path)) {
      for (const saved of staticOrders(JSON.parse(fs.readFileSync(input.backup_path, 'utf8'))))
        if (!restore.some(item => item.dir === saved.dir)) restore.push(saved);
    }
    for (const dir of new Set([input.main, ...input.agents.map(a => a.dir)]))
      await local(dir, store => {
        const managedOrder = ['openai', 'openai-codex'].filter(provider => !store.order?.[provider]
          || store.order[provider][0] === GLOBAL);
        removed += clear(store);
        const saved = restore.find(item => item.dir === dir);
        for (const provider of managedOrder) {
          const ids = (saved?.order[provider] || []).filter(id => store.profiles[id] && !oauth(store.profiles[id]));
          if (ids.length) (store.order ??= {})[provider] = ids;
        }
        return true;
      });
    emit({credentials_removed: removed});
  } else throw new Error('unsupported_action');
} catch {
  // Native errors may contain HTTP bodies or credential data; never forward them.
  emit({event: 'error', error_code: 'CODEX_AUTH_FAILED'});
  process.exitCode = 1;
}
