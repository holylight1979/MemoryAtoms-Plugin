'use strict';
// MemoryAtoms plugin hook launcher (Node 18+, standard library only).
//
// hooks.json runs every hook in exec form:
//   node ${CLAUDE_PLUGIN_ROOT}/hooks/run.js ${CLAUDE_PLUGIN_ROOT}/hooks/<script>.py
// This file finds a Python >= 3.10, then runs <script> with stdin, stdout, stderr
// and the exit code passed through unchanged (exit 2 = block). Timeouts belong to
// Claude Code; the launcher sets none.
//
// It is copied as-is into every extension plugin: the plugin root is
// ${CLAUDE_PLUGIN_ROOT} (fallback: this file's parent folder) and the script path
// comes in as an absolute argument. The core plugin's folder is passed to Python as
// WG_CORE_ROOT (see coreRoot): an extension's Python cannot reach the core's lib/ by a
// relative path. In the core, SessionStart also writes WG_CORE_ROOT into
// CLAUDE_ENV_FILE, so tools that skills run through Bash find the core as well.
//
// Python lookup order:
//   1. CLAUDE_PLUGIN_OPTION_PYTHON (userConfig "python"), when the file exists
//   2. ${CLAUDE_PLUGIN_DATA}/python.json cache, when the exe still has the same size
//      and mtime it had when it was probed as >= 3.10 (no extra process per hook)
//   3. Windows: py -3, every python.exe on PATH outside \WindowsApps\ (the Store
//      placeholder exits 9009 with no output), then the newest >= 3.10 from py -0p
//      Others: python3, python
// No Python: SessionStart / UserPromptSubmit get one [Guardian:Degraded] block
// (from the core's workflow-guardian.py entry only, so it is said once per event);
// every other event passes silently with exit 0.

const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const MIN = [3, 10];
const IS_WIN = process.platform === 'win32';
const SPEAKER = 'workflow-guardian.py'; // the one entry that reports launcher problems
const PROBE = 'import sys;print("%d %d %s"%(sys.version_info[0],sys.version_info[1],sys.executable))';
const CORE_PLUGIN = 'memoryatoms';

const MSG = {
  en: {
    noPython: (tried, badOpt) =>
      '[Guardian:Degraded] MemoryAtoms needs Python 3.10 or newer and found none' +
      (badOpt ? ` (the configured path "${badOpt}" does not exist)` : '') +
      `. Checked: ${tried || 'nothing'}. Memory injection and card writing are off until this is fixed. ` +
      'Install Python 3.10 or newer (the installer from the official Python website), or enter the full path of python ' +
      'under `/plugin configure` (MemoryAtoms, field "python"), then restart Claude Code.',
    noScript: (p) => `[MemoryAtoms launcher] hook script not found: ${p}`,
    guardError: (e) => `[MemoryAtoms launcher] edition-guard failed, running anyway: ${e}`,
    noCore: () => '[Guardian:Degraded] MemoryAtoms extension: the core plugin "memoryatoms" was not found in plugins/installed_plugins.json; install or enable it (claude plugin install memoryatoms@memoryatoms).',
  },
  'zh-TW': {
    noPython: (tried, badOpt) =>
      '[Guardian:Degraded] MemoryAtoms 需要 Python 3.10 以上，這台機器找不到' +
      (badOpt ? `（設定的路徑「${badOpt}」不存在）` : '') +
      `。查過：${tried || '無'}。修好前，記憶注入與寫卡都不會動。` +
      '請到 Python 官方網站下載安裝 Python 3.10 以上，或在 `/plugin configure`' +
      '（MemoryAtoms 的「python」欄）填 python 的完整路徑，再重開 Claude Code。',
    noScript: (p) => `[MemoryAtoms 啟動器] 找不到 hook 腳本：${p}`,
    guardError: (e) => `[MemoryAtoms 啟動器] edition-guard 出錯，照常執行：${e}`,
    noCore: () => '[Guardian:Degraded] MemoryAtoms 擴充：在 plugins/installed_plugins.json 找不到核心插件「memoryatoms」；請安裝或啟用它（claude plugin install memoryatoms@memoryatoms）。',
  },
};

function lang() {
  const v = String(process.env.CLAUDE_PLUGIN_OPTION_LANGUAGE || '').trim().toLowerCase();
  return v.startsWith('zh') ? MSG['zh-TW'] : MSG.en;
}

function readStdin() {
  if (process.stdin.isTTY) return Buffer.alloc(0);
  try {
    const chunks = [];
    const buf = Buffer.alloc(65536);
    for (;;) {
      let n;
      try {
        n = fs.readSync(0, buf, 0, buf.length, null);
      } catch (e) {
        if (e.code === 'EAGAIN') continue;
        if (e.code === 'EOF') break;
        throw e;
      }
      if (n === 0) break;
      chunks.push(Buffer.from(buf.subarray(0, n)));
    }
    return Buffer.concat(chunks);
  } catch (_e) {
    return Buffer.alloc(0);
  }
}

function eventName(input) {
  try {
    return String(JSON.parse(input.toString('utf8')).hook_event_name || '');
  } catch (_e) {
    return '';
  }
}

function okVersion(maj, min) {
  return maj > MIN[0] || (maj === MIN[0] && min >= MIN[1]);
}

// Run `exe args -c PROBE`; return {exe: sys.executable, ver: [maj, min]} or null.
function probe(exe, args) {
  try {
    const out = execFileSync(exe, [...args, '-c', PROBE], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 10000,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    const m = /^(\d+) (\d+) (.+)$/m.exec(out.trim());
    if (!m) return null;
    const ver = [Number(m[1]), Number(m[2])];
    const real = m[3].trim();
    return { exe: real && fs.existsSync(real) ? real : exe, ver };
  } catch (_e) {
    return null;
  }
}

function stamp(exe) {
  const st = fs.statSync(exe);
  return { size: st.size, mtimeMs: Math.round(st.mtimeMs) };
}

function readCache(file) {
  try {
    const c = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!c || typeof c.exe !== 'string' || !Array.isArray(c.version)) return null;
    if (!okVersion(c.version[0], c.version[1])) return null;
    const s = stamp(c.exe);
    if (s.size !== c.size || s.mtimeMs !== c.mtimeMs) return null; // exe replaced: re-probe
    return c.exe;
  } catch (_e) {
    return null;
  }
}

function writeCache(file, found) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const data = { exe: found.exe, version: found.ver, ...stamp(found.exe) };
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data, null, 1) + '\n');
    fs.renameSync(tmp, file);
  } catch (_e) {
    // cache is an optimisation only
  }
}

// Every match of `name` on PATH (Windows: name.exe), skipping \WindowsApps\ stubs.
function onPath(name) {
  const hits = [];
  const seen = new Set();
  for (const dir of String(process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    if (IS_WIN && /[\\/]WindowsApps([\\/]|$)/i.test(dir)) continue;
    const p = path.join(dir.replace(/^"|"$/g, ''), IS_WIN ? `${name}.exe` : name);
    const key = IS_WIN ? p.toLowerCase() : p;
    if (seen.has(key)) continue;
    seen.add(key);
    try {
      if (fs.statSync(p).isFile()) hits.push(p);
    } catch (_e) {
      // not here
    }
  }
  return hits;
}

// Windows: newest >= 3.10 the py launcher knows about (py -3 may default to an old one).
function pyList(pyExe) {
  try {
    const out = execFileSync(pyExe, ['-0p'], {
      encoding: 'utf8', windowsHide: true, timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'],
    });
    const rows = [];
    for (const line of out.split(/\r?\n/)) {
      const m = /-V?:?(\d+)\.(\d+)\S*\s+\*?\s*(\S.*\.exe)\s*$/i.exec(line.trim());
      if (m && okVersion(Number(m[1]), Number(m[2]))) rows.push([Number(m[1]), Number(m[2]), m[3].trim()]);
    }
    rows.sort((a, b) => b[0] - a[0] || b[1] - a[1]);
    return rows.map((r) => r[2]);
  } catch (_e) {
    return [];
  }
}

// Folder of the core plugin: this plugin when it is the core (has hooks/workflow-guardian.py);
// else WG_CORE_ROOT if already set; else the installPath of "memoryatoms@<market>" in
// <CLAUDE_CONFIG_DIR or ~/.claude>/plugins/installed_plugins.json ("memoryatoms@memoryatoms" first).
// '' when not found.
function coreRoot(pluginRoot, env = process.env) {
  if (fs.existsSync(path.join(pluginRoot, 'hooks', SPEAKER))) return pluginRoot;
  if (env.WG_CORE_ROOT) return env.WG_CORE_ROOT;
  const configDir = env.CLAUDE_CONFIG_DIR || path.join(require('os').homedir(), '.claude');
  let plugins;
  try {
    plugins = JSON.parse(fs.readFileSync(path.join(configDir, 'plugins', 'installed_plugins.json'), 'utf8')).plugins || {};
  } catch (_e) {
    return '';
  }
  const own = `${CORE_PLUGIN}@${CORE_PLUGIN}`;
  const ids = Object.keys(plugins).filter((id) => id.split('@')[0] === CORE_PLUGIN).sort((a, b) => (a === own ? -1 : b === own ? 1 : a < b ? -1 : 1));
  for (const id of ids) {
    for (const e of Array.isArray(plugins[id]) ? plugins[id] : []) {
      const where = e && typeof e === 'object' ? String(e.installPath || '') : '';
      if (where && fs.existsSync(path.join(where, 'hooks', SPEAKER))) return where;
    }
  }
  return '';
}

// Line appended to CLAUDE_ENV_FILE (Claude Code sources it before each Bash command).
function envFileLine(core) {
  return `export WG_CORE_ROOT="${String(core).replace(/\\/g, '/').replace(/"/g, '\\"')}"\n`;
}

function findPython(dataDir) {
  const tried = [];
  let badOpt = '';
  const opt = String(process.env.CLAUDE_PLUGIN_OPTION_PYTHON || '').trim();
  if (opt) {
    if (fs.existsSync(opt)) return { exe: opt, tried, badOpt };
    badOpt = opt;
    tried.push(`option:${opt}`);
  }
  const cacheFile = dataDir ? path.join(dataDir, 'python.json') : '';
  if (cacheFile) {
    const cached = readCache(cacheFile);
    if (cached) return { exe: cached, tried, badOpt };
  }
  const attempts = [];
  if (IS_WIN) {
    const pys = onPath('py');
    for (const py of pys) attempts.push([py, ['-3'], `${py} -3`]);
    for (const p of onPath('python')) attempts.push([p, [], p]);
    for (const py of pys.slice(0, 1)) {
      attempts.push(() => pyList(py).map((p) => [p, [], `${p} (py -0p)`]));
    }
  } else {
    for (const p of onPath('python3')) attempts.push([p, [], p]);
    for (const p of onPath('python')) attempts.push([p, [], p]);
  }
  const queue = attempts.slice();
  while (queue.length) {
    const a = queue.shift();
    if (typeof a === 'function') {
      queue.unshift(...a());
      continue;
    }
    const [exe, args, label] = a;
    const r = probe(exe, args);
    if (r && okVersion(r.ver[0], r.ver[1])) {
      if (cacheFile) writeCache(cacheFile, r);
      return { exe: r.exe, tried, badOpt };
    }
    tried.push(r ? `${label}=${r.ver.join('.')}` : label);
  }
  return { exe: '', tried, badOpt };
}

function main() {
  const script = process.argv[2] || '';
  const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT || path.dirname(__dirname);
  const dataDir = process.env.CLAUDE_PLUGIN_DATA || '';
  const input = readStdin();
  const event = eventName(input);
  const speaks = path.basename(script).toLowerCase() === SPEAKER;
  const m = lang();

  // 1. installed edition running? -> step aside (stderr one line at SessionStart, never stdout)
  try {
    const eg = require(path.join(pluginRoot, 'hooks', 'edition-guard.js'));
    const guard = eg.editionGuard({ event, pluginRoot });
    if (guard && guard.active === false) {
      if (event === 'SessionStart' && speaks) process.stderr.write(eg.yieldMessage() + '\n');
      process.exitCode = 0;
      return;
    }
  } catch (e) {
    if (e && e.code !== 'MODULE_NOT_FOUND') process.stderr.write(m.guardError(e && e.message) + '\n');
  }

  // 2. script must exist (non-blocking error otherwise)
  if (!script || !fs.existsSync(script)) {
    process.stderr.write(m.noScript(script) + '\n');
    process.exitCode = 1;
    return;
  }

  // 3. Python
  const found = findPython(dataDir);
  if (!found.exe) {
    if ((event === 'SessionStart' || event === 'UserPromptSubmit') && speaks) {
      process.stdout.write(m.noPython(found.tried.join(', '), found.badOpt) + '\n');
    }
    process.exitCode = 0;
    return;
  }

  // 4. run it, pass everything through
  const env = { ...process.env, PYTHONUTF8: '1', WG_PLUGIN_LAUNCHER: '1' };
  if (dataDir) env.PYTHONPYCACHEPREFIX = path.join(dataDir, 'pycache');
  const core = coreRoot(pluginRoot);
  if (core) {
    env.WG_CORE_ROOT = core;
    if (core === pluginRoot && event === 'SessionStart' && process.env.CLAUDE_ENV_FILE && speaks) {
      try { fs.appendFileSync(process.env.CLAUDE_ENV_FILE, envFileLine(core)); } catch (_e) { /* Bash tools fall back to parents[3] */ }
    }
  } else {
    process.stderr.write(m.noCore() + '\n');
  }
  const child = spawn(found.exe, [script, ...process.argv.slice(3)], {
    env,
    windowsHide: true,
    stdio: ['pipe', 'inherit', 'inherit'],
  });
  child.on('error', (e) => {
    process.stderr.write(`[MemoryAtoms launcher] ${found.exe}: ${e.message}\n`);
    process.exitCode = 1;
  });
  child.stdin.on('error', () => {}); // child may exit without reading stdin
  child.stdin.end(input);
  if (!IS_WIN) {
    for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
      process.on(sig, () => { try { child.kill(sig); } catch (_e) { /* gone */ } });
    }
  }
  child.on('close', (code, signal) => {
    process.exitCode = code === null ? (signal ? 1 : 0) : code;
  });
}

main();
