# Pulsewire 桌面 app(Electron)

把每天的日报包成一个 macOS 原生窗口 app:Launchpad/Dock 里有图标,双击打开就是**当天最新**日报,不用记路径、不用开浏览器找标签。

## 形态
- 独立窗口(与日报同款剪报本色系,自带图标),不是浏览器标签。
- **加载项目目录里的实时文件**(`web/app/index.html`),不把 HTML 打进包——所以每天调度器跑完新日报,app 一刷新就是最新。
- 原生菜单:
  - **视图**:今日日报(⌘1)/ 全史档案(⌘2)/ 刷新(⌘R)
  - **操作**:立即重跑日报(会推飞书,带确认框,后台跑约 20–90 分钟——判官走 pro,判决缓存暖时偏快、冷启动偏长)/ 打开渲染图文件夹 / 查看运行日志

## 项目目录怎么找到
app 按以下顺序确定项目根(`config.yaml` 所在目录):
1. 环境变量 `PULSEWIRE_ROOT`;
2. 开发模式(`npm start`)自动取 `desktop/` 的上一级;
3. 打包版首次启动弹窗选一次,之后记住(项目挪走后会再问一次)。

`uv` / `docker` 可执行文件按常见安装位置自动探测,特殊安装位置用 `PULSEWIRE_UV` / `PULSEWIRE_DOCKER` 环境变量指定。

## 重新构建(改了代码/图标后)
```bash
cd <项目根>/desktop
# 1) 图标改了才需要:重生成 PNG + icns
uv run python icon/make_icon.py && bash icon/build_icns.sh
# 2) 打包 .app
npm run build
# 3) 装进 /Applications(覆盖旧的)+ 去隔离属性
rm -rf /Applications/Pulsewire.app
cp -R dist/mac-arm64/Pulsewire.app /Applications/Pulsewire.app
xattr -cr /Applications/Pulsewire.app
```

## 首次开发调试(不打包)
```bash
cd <项目根>/desktop && npm install && npm start
```

## 说明
- **未签名**(没买 Apple 开发者证书)。本地自己构建自己用没问题;`xattr -cr` 去掉隔离属性即可避免 Gatekeeper 拦。若哪天提示"已损坏/无法打开",重跑上面第 3 步即可。
- `node_modules/`(~已装)和 `dist/`(构建产物 ~233MB)都已 gitignore;提交的是源码 + `icon.icns`。
