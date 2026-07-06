// Pulsewire 桌面 app(Electron 主进程)
// 形态:独立窗口 + 原生菜单,加载项目目录里**实时**的日报/档案 HTML。
// 关键:不把 HTML 打进 app 包——日报每天被调度器重写,app 每次打开/刷新都读最新文件。
'use strict'

const { app, BrowserWindow, Menu, dialog, shell, Notification, ipcMain } = require('electron')
const { spawn } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')

// —— 项目根解析(按序):PULSEWIRE_ROOT 环境变量 → 开发模式取 desktop/ 上一级 →
//    首启弹窗选一次并记住(打包后 __dirname 在 asar 内推不出项目根,只能靠前两者或记忆文件)。
function rootMemoPath() {
  return path.join(app.getPath('userData'), 'root.json')
}

function isProjectRoot(dir) {
  try {
    return fs.existsSync(path.join(dir, 'config.yaml')) && fs.existsSync(path.join(dir, 'web'))
  } catch { return false }
}

function resolveRoot() {
  const envRoot = process.env.PULSEWIRE_ROOT
  if (envRoot && isProjectRoot(envRoot)) return envRoot
  if (!app.isPackaged) {
    const dev = path.resolve(__dirname, '..')
    if (isProjectRoot(dev)) return dev
  }
  try {
    const memo = JSON.parse(fs.readFileSync(rootMemoPath(), 'utf-8')).root
    if (memo && isProjectRoot(memo)) return memo
  } catch { /* 没记过或记的目录已失效 → 走弹窗 */ }
  return null
}

function askRootSync() {
  dialog.showMessageBoxSync({
    type: 'info',
    message: '请选择 pulsewire 项目目录',
    detail: '即 config.yaml 所在的那个文件夹(仓库根目录)。选一次就记住,之后不再问。',
  })
  const picked = dialog.showOpenDialogSync({ properties: ['openDirectory'] })
  if (!picked || !picked[0] || !isProjectRoot(picked[0])) return null
  fs.mkdirSync(path.dirname(rootMemoPath()), { recursive: true })
  fs.writeFileSync(rootMemoPath(), JSON.stringify({ root: picked[0] }))
  return picked[0]
}

// uv / docker 可执行文件:环境变量优先,否则按常见安装位置探测
function firstExisting(cands) {
  for (const c of cands) { try { if (fs.existsSync(c)) return c } catch { /* 继续探 */ } }
  return cands[0]
}
const HOME = process.env.HOME || ''
const UV = process.env.PULSEWIRE_UV
  || firstExisting([`${HOME}/.local/bin/uv`, '/opt/homebrew/bin/uv', '/usr/local/bin/uv'])
const DOCKER = process.env.PULSEWIRE_DOCKER
  || firstExisting(['/usr/local/bin/docker', '/opt/homebrew/bin/docker'])

// app ready 后由 resolveRoot()/askRootSync() 填充
let ROOT = null
let TODAY_HTML = null
let ARCHIVE_HTML = null

let win = null
let rerunning = false // 防重复触发整跑

function createWindow() {
  win = new BrowserWindow({
    width: 1240,
    height: 880,
    minWidth: 880,
    minHeight: 600,
    title: 'Pulsewire',
    backgroundColor: '#E6DFC9', // 剪报本"桌面"色(STYLE.md --desk;旧值是便签时代暖白,与新皮割裂)
    // 系统标题栏沉进纸面(2026-07-05 用户:"边框要和页面色调统一,现在像网页不像app"):
    // hiddenInset = 无白条标题栏、红绿灯浮在页面上;页面侧 UA 检出 Electron 后自铺
    // 36px 桌面色拖拽带(.topdrag,-webkit-app-region:drag),窗口照常可拖。
    titleBarStyle: 'hiddenInset',
    icon: path.join(__dirname, 'icon/icon.icns'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'), // 档案页问答桥(window.pulsewire.ask)
    },
  })
  loadToday()
  // 外链(读原文)用系统默认浏览器开,不在 app 内导航走丢
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) {
      shell.openExternal(url)
      return { action: 'deny' }
    }
    return { action: 'allow' }
  })
}

function loadToday() {
  if (!win) return
  if (!fs.existsSync(TODAY_HTML)) {
    // 全新装机还没跑过流水线:给一页人话提示,别白屏
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      '<body style="background:#E6DFC9;color:#26221C;font-family:Songti SC,serif;display:flex;align-items:center;justify-content:center;height:96vh"><div style="max-width:34em;text-align:center"><h2>还没有日报</h2><p>先跑一次流水线:<br><code style="background:#FCFAF3;padding:.2em .5em">uv run pulsewire run --force</code><br>跑完点 ⌘R 刷新。</p></div></body>'))
    return
  }
  win.loadFile(TODAY_HTML)
}
function loadArchive() {
  if (win) win.loadFile(ARCHIVE_HTML)
}

// —— 立即重跑日报:有真推飞书副作用,先确认,再后台跑,不卡窗口 ——
async function rerunDaily() {
  if (rerunning) {
    dialog.showMessageBox(win, { type: 'info', message: '日报正在重跑中…', detail: '完成后窗口会自动刷新。' })
    return
  }
  const { response } = await dialog.showMessageBox(win, {
    type: 'warning',
    buttons: ['取消', '开始重跑'],
    defaultId: 0,
    cancelId: 0,
    message: '立即重跑今天的日报?',
    detail:
      '整套流水线约 12–18 分钟,跑完会真推送到你的飞书(同一天幂等,只推一次)。\n会自动开 Docker、跑完自动关;期间窗口可正常用,完成后自动刷新。',
  })
  if (response !== 1) return

  rerunning = true
  new Notification({ title: 'Pulsewire', body: '日报开始重跑,约 12–18 分钟…' }).show()
  // 自动管 Docker:没起则拉起 → 等就绪 → 跑 --force → 用完关 Docker(与每日脚本同款生命周期)。
  // $i/$(...)/$rc 是 bash(单 $,不与 JS 的 ${} 冲突);只有 ROOT/DOCKER/UV 被 JS 内插。
  const rerunSh = `
set -uo pipefail
if ! ${DOCKER} info >/dev/null 2>&1; then /usr/bin/open -a Docker >/dev/null 2>&1 || true; fi
for i in $(seq 1 60); do ${DOCKER} info >/dev/null 2>&1 && break; sleep 3; done
${DOCKER} compose up -d postgres >/dev/null 2>&1 || true
for i in $(seq 1 30); do ${DOCKER} compose ps postgres 2>/dev/null | grep -q healthy && break; sleep 2; done
rc=0
${UV} run pulsewire run --force || rc=$?
/usr/bin/osascript -e 'quit app "Docker Desktop"' >/dev/null 2>&1 || true
exit $rc
`
  const proc = spawn('/bin/bash', ['-c', rerunSh], { cwd: ROOT })
  proc.on('error', (err) => {
    rerunning = false
    dialog.showMessageBox(win, { type: 'error', message: '启动重跑失败', detail: String(err) })
  })
  proc.on('close', (code) => {
    rerunning = false
    if (code === 0) {
      new Notification({ title: 'Pulsewire', body: '日报已更新,正在刷新窗口。' }).show()
      loadToday()
    } else {
      new Notification({ title: 'Pulsewire', body: `重跑结束但退出码 ${code},看 deploy/logs 排查。` }).show()
    }
  })
}

// —— 档案页问答(window.pulsewire.ask → 跑 `pulsewire ask --json`)——
// 生命周期:首问时确保 Docker+postgres 起来(没开就帮开),用时常驻;闲置 10 分钟自动关 Docker
// (仅当 Docker 是问答自己拉起的——用户原本开着的不动)。问题以 argv 传入、绝不拼进 shell,免注入。
let qaDockerStarted = false // 本轮 Docker 是不是问答拉起的
let qaIdleTimer = null
const QA_IDLE_MS = 10 * 60 * 1000

function sh(script) {
  return new Promise((resolve) => {
    const p = spawn('/bin/bash', ['-c', script], { cwd: ROOT })
    let out = '', err = ''
    p.stdout.on('data', (d) => (out += d))
    p.stderr.on('data', (d) => (err += d))
    p.on('close', (code) => resolve({ code, out, err }))
    p.on('error', (e) => resolve({ code: -1, out, err: String(e) }))
  })
}

// 确保 Docker 起着 + postgres healthy。返回 { ok, startedDocker }。
async function ensureStack() {
  const wasUp = (await sh(`${DOCKER} info >/dev/null 2>&1 && echo UP`)).out.includes('UP')
  if (!wasUp) await sh(`/usr/bin/open -a Docker >/dev/null 2>&1 || true`)
  const r = await sh(`
set -uo pipefail
for i in $(seq 1 40); do ${DOCKER} info >/dev/null 2>&1 && break; sleep 3; done
${DOCKER} info >/dev/null 2>&1 || { echo NODOCKER; exit 1; }
${DOCKER} compose up -d postgres >/dev/null 2>&1 || true
for i in $(seq 1 30); do ${DOCKER} compose ps postgres 2>/dev/null | grep -q healthy && { echo OK; exit 0; }; sleep 2; done
echo NOPG; exit 1
`)
  return { ok: r.out.includes('OK'), startedDocker: !wasUp }
}

function scheduleQaIdleRelease() {
  if (qaIdleTimer) clearTimeout(qaIdleTimer)
  qaIdleTimer = setTimeout(() => {
    qaIdleTimer = null
    if (qaDockerStarted && !rerunning) {
      sh(`/usr/bin/osascript -e 'quit app "Docker Desktop"' >/dev/null 2>&1 || true`)
      qaDockerStarted = false
    }
  }, QA_IDLE_MS)
}

ipcMain.handle('qa:ask', async (_e, question) => {
  const q = String(question || '').trim()
  if (!q) return { ok: true, enough: false, answer: '问题是空的。', cards: [] }
  if (qaIdleTimer) { clearTimeout(qaIdleTimer); qaIdleTimer = null } // 用时常驻:取消待关

  let stack
  try { stack = await ensureStack() } catch { stack = { ok: false } }
  // 即便没等到 postgres healthy,只要 Docker 是问答自己拉起的就记下,好让闲置释放能把它关掉(免漏关)
  if (stack.startedDocker) qaDockerStarted = true
  if (!stack.ok) { scheduleQaIdleRelease(); return { ok: false, error: 'docker_or_pg_unavailable' } }

  // 跑 ask --json:问题作为独立 argv(不进 shell,免注入)。stdout 掺结构化日志行 →
  // 从后往前找第一行能 JSON.parse 成对象的(即真正的结果行)。
  const res = await new Promise((resolve) => {
    const p = spawn(UV, ['run', 'pulsewire', 'ask', q, '--json'], { cwd: ROOT })
    let out = '', err = ''
    p.stdout.on('data', (d) => (out += d))
    p.stderr.on('data', (d) => (err += d))
    p.on('error', (e) => resolve({ ok: false, error: String(e) }))
    p.on('close', () => {
      const lines = out.split('\n').map((s) => s.trim()).filter(Boolean)
      for (let i = lines.length - 1; i >= 0; i--) {
        if (lines[i][0] === '{') {
          try { return resolve(JSON.parse(lines[i])) } catch { /* 继续往上找 */ }
        }
      }
      resolve({ ok: false, error: 'parse_failed', raw: (err || out).slice(-400) })
    })
  })
  scheduleQaIdleRelease()
  return res
})

function buildMenu() {
  const isMac = process.platform === 'darwin'
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: '视图',
      submenu: [
        { label: '今日日报', accelerator: 'CmdOrCtrl+1', click: loadToday },
        { label: '全史档案', accelerator: 'CmdOrCtrl+2', click: loadArchive },
        { type: 'separator' },
        { label: '刷新', accelerator: 'CmdOrCtrl+R', click: loadToday },
        { role: 'toggleDevTools' },
      ],
    },
    {
      label: '操作',
      submenu: [
        { label: '立即重跑日报(会推飞书)', click: rerunDaily },
        { type: 'separator' },
        {
          label: '打开渲染图文件夹',
          click: () => shell.openPath(path.join(ROOT, 'web/rendered')),
        },
        {
          label: '查看运行日志',
          click: () => shell.openPath(path.join(ROOT, 'deploy/logs')),
        },
      ],
    },
    { role: 'editMenu' }, // 复制/粘贴/全选——检索框要用
    { role: 'windowMenu' },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

app.whenReady().then(() => {
  ROOT = resolveRoot() || askRootSync()
  if (!ROOT) {
    dialog.showErrorBox('Pulsewire', '没有选到有效的项目目录(需含 config.yaml),应用退出。')
    app.quit()
    return
  }
  TODAY_HTML = path.join(ROOT, 'web/app/index.html')
  ARCHIVE_HTML = path.join(ROOT, 'web/archive/index.html')
  buildMenu()
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
