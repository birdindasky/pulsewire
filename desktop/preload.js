// Pulsewire 桌面 app 预载桥(contextIsolation 下渲染进程与主进程的唯一通道)
// 只暴露一个最小面:问答。渲染页(档案页)调 window.pulsewire.ask(问题) → 主进程跑
// `pulsewire ask --json` 并回结构化结果。不暴露任何文件/shell 能力,面越小越安全。
'use strict'

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('pulsewire', {
  ask: (question) => ipcRenderer.invoke('qa:ask', question),
})
