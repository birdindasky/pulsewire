"""日报档案:每日快照落盘 + 档案首页(全史检索)重建。

- 每日 webapp 交付后:把自包含的当日 index.html 快照成 `web/archive/daily/<日期>.html`
  (file:// 双击即看当天完整 App),数据另存 `<日期>.json` 供档案首页建检索索引。
- 档案首页 `web/archive/index.html`:跨天检索(headline/tldr/source 内联索引)+ 按日便签卡
  (带当日头条预览,同主 App 便签语言:旋转/胶带/硬投影/GSAP 入场)。
- 旧系统抢救件(legacy/ 下 app.html + digests/*.json,**仅文字数据,图卡已弃**)
  只存盘备查、不在页面露出——老视觉与锁定风格不统一;数据留着日后并入档案检索(2026-06-12 用户定)。
- 视觉同 STYLE.md(暖白+软黑+粉,硬投影,3px 锐角);STYLE.md §3.1 的「全史按天归档」即本模块。
- 档案是次要产物:快照/重建失败告警不拖垮 webapp 主交付(同 bio/geo 次领域先例)。
"""

from __future__ import annotations

import json
from pathlib import Path

from pulsewire.config import PROJECT_ROOT
from pulsewire.obs import get_logger

log = get_logger()

ARCHIVE_DIR = PROJECT_ROOT / "web" / "archive"

# 档案首页模板。__INDEX__ 处替换为内联检索索引 JSON。剪报语言同主 App(webapp._APP,STYLE.md v2);
# 问答面板桥协议(window.pulsewire.ask)与桌面 App 咬合,重构时逻辑勿动。
_INDEX_PAGE = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pulsewire 往期剪报</title>
<style>
:root{
  --desk:#E6DFC9; --paper:#F6F1E2; --ink:#26221C; --ink2:#57524A; --mut:#948D7D;
  --pen:#D9331F; --hlt:#FFE05C; --postit:#FFEE9C; --postit2:#FFD3E1; --tape:rgba(180,215,200,.6);
  --song:'Songti SC','STSong','Noto Serif SC',serif;
  --hand:'Hannotate SC','HanziPen SC','Kaiti SC',cursive;
  --num:Georgia,serif;
}
*{margin:0;padding:0;box-sizing:border-box;}
html{background:var(--desk);-webkit-text-size-adjust:100%;}
body{max-width:1150px;margin:0 auto;background:var(--paper);color:var(--ink);
  font-family:var(--song);min-height:100vh;position:relative;overflow-x:hidden;
  box-shadow:0 0 0 1px #d4ccb4,0 26px 70px -34px rgba(38,34,28,.6);}
body::before{content:'';position:absolute;left:16px;top:0;bottom:0;width:1px;
  background:repeating-linear-gradient(180deg,transparent 0 34px,#cabfa2 34px 35px);opacity:.6;}
.grain{position:fixed;inset:0;pointer-events:none;opacity:.05;z-index:95;mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
a{color:inherit;}
:focus-visible{outline:2.5px dashed var(--pen);outline-offset:3px;}
mark{background:linear-gradient(180deg,transparent 42%,var(--hlt) 42% 88%,transparent 88%);color:inherit;padding:0 1px;}

/* 顶栏:返回钮置顶左(用户点名要显眼),粉便签索引标签语言 */
.topbar{position:sticky;top:0;z-index:80;background:var(--paper);border-bottom:2px dashed #cfc6ac;
  display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 40px 12px 26px;}
.brand{font-family:var(--num);font-weight:700;font-size:26px;letter-spacing:-.01em;}
.brand em{font-style:italic;color:var(--pen);}
.brand .tag{position:relative;font-family:var(--song);font-weight:900;font-size:20px;margin-left:10px;letter-spacing:.1em;}
.brand .tag i{position:absolute;left:-6px;right:-8px;bottom:1px;height:.55em;z-index:-1;background:var(--hlt);opacity:.85;font-style:normal;}
.topbar a.back{position:relative;display:inline-flex;align-items:center;gap:8px;min-height:40px;
  font-family:var(--hand);font-size:17px;color:var(--ink);text-decoration:none;
  background:var(--postit2);padding:8px 20px 9px 15px;border-radius:4px;
  transform:rotate(-1.5deg);box-shadow:0 7px 13px -6px rgba(38,34,28,.38);transition:transform .15s;}
.topbar a.back i{font-style:normal;color:var(--pen);font-weight:700;font-size:19px;line-height:1;}
.topbar a.back::before{content:'';position:absolute;top:-8px;left:14px;width:48px;height:15px;
  background:var(--tape);transform:rotate(-3deg);mix-blend-mode:multiply;}
.topbar a.back:hover{transform:rotate(0) translateY(-2px);}
/* 头部 */
.hero{padding:30px 40px 0;}
.hero .dl{font-family:var(--hand);font-size:16.5px;color:var(--ink2);}
.hero .dl b{color:var(--pen);font-weight:400;font-family:var(--num);}
/* 问历史大便利贴(问答面板;桥逻辑见页尾脚本,勿动) */
.qa{position:relative;margin:26px 40px 0;background:var(--postit);padding:24px 28px 26px;
  transform:rotate(-.8deg);box-shadow:0 12px 22px -10px rgba(38,34,28,.35);}
.qa::before{content:'';position:absolute;top:-10px;left:36px;width:66px;height:19px;
  background:var(--tape);transform:rotate(-2deg);}
.qa-head{font-family:var(--hand);font-size:19px;color:var(--pen);margin-bottom:12px;}
.qa-row{display:flex;gap:12px;align-items:stretch;}
.qa-row input{flex:1;font-family:var(--hand);font-size:18px;padding:12px 16px;background:#FFFDF4;
  border:1.5px dashed #c9b96a;color:var(--ink);outline:none;border-radius:6px;}
.qa-row input::placeholder{color:#a89a55;}
.qa-row input:disabled{opacity:.6;}
.qa-btn{font-family:var(--hand);font-size:18px;padding:0 26px;background:var(--pen);color:#fff;
  border:none;border-radius:6px;cursor:pointer;letter-spacing:.2em;text-indent:.2em;transition:transform .15s;}
.qa-btn:hover:not(:disabled){transform:rotate(-2deg) scale(1.04);}
.qa-btn:disabled{opacity:.5;cursor:default;}
.qa-ans{margin-top:16px;}
.qa-ans:empty{display:none;}
.qa-think,.qa-hint,.qa-err{font-family:var(--hand);font-size:16px;color:#6b5f28;line-height:1.75;}
.qa-err{color:var(--pen);}
.qa-answer{font-family:var(--song);font-size:16px;line-height:1.85;color:var(--ink);text-align:justify;
  background:#FFFDF4;border-left:3px solid var(--pen);padding:12px 16px;border-radius:4px;}
.qa-answer sup{color:var(--pen);font-weight:700;font-size:12px;padding:0 1px;}
.qa-cites{margin-top:14px;padding-top:12px;border-top:1.5px dashed #d9c977;}
.qa-cites .lbl{font-family:var(--hand);font-size:13px;color:var(--pen);margin-bottom:6px;}
.qa-cite{display:block;font-size:13.5px;color:#6b5f28;text-decoration:none;padding:4px 0;line-height:1.5;}
.qa-cite:hover{color:var(--pen);}
/* 全史检索撕纸条 */
.search{position:relative;margin:26px 28px 0;padding:14px 22px 16px;background:#FBF7EA;
  clip-path:polygon(0 18%,3% 4%,9% 14%,16% 2%,24% 12%,33% 0,42% 10%,52% 2%,61% 12%,70% 1%,79% 11%,88% 3%,95% 13%,100% 5%,100% 84%,96% 97%,89% 88%,81% 99%,72% 89%,62% 100%,52% 90%,42% 99%,32% 88%,22% 98%,13% 87%,6% 97%,0 86%);}
.search input{width:100%;font-family:var(--hand);font-size:18px;padding:10px 14px;background:transparent;
  border:none;border-bottom:2.5px dashed #c2b99f;color:var(--ink);outline:none;}
.search input::placeholder{color:var(--mut);}
.scount{padding:18px 40px 0;font-family:var(--hand);font-size:15px;color:var(--ink2);}
/* 日卡/命中卡 = 撕边剪报 */
.grid{padding:26px 36px 30px;display:grid;grid-template-columns:1fr 1fr;gap:36px 32px;}
.clipw{filter:drop-shadow(0 12px 12px rgba(38,34,28,.17));transform:rotate(var(--rot,0deg));
  transition:transform .22s cubic-bezier(.3,1.4,.5,1),filter .2s;
  animation:paste .5s cubic-bezier(.3,1.4,.5,1) both;animation-delay:var(--d,0s);}
.clipw:hover{transform:rotate(0) translateY(-7px);filter:drop-shadow(0 18px 16px rgba(38,34,28,.22));}
@keyframes paste{from{opacity:0;transform:translateY(24px) rotate(var(--rot,0deg)) scale(.96);}
  to{opacity:1;transform:rotate(var(--rot,0deg));}}
.clip{display:block;position:relative;background:#FCFAF3;padding:24px 22px 18px;text-decoration:none;color:var(--ink);
  clip-path:polygon(1% 2.5%,7% .5%,15% 2%,26% 0,37% 2.5%,50% .5%,62% 2.5%,73% 0,84% 2%,93% .5%,99.5% 3%,99% 14%,100% 27%,98.5% 40%,100% 53%,99% 66%,100% 79%,98.5% 90%,99.5% 97%,93% 99.5%,82% 98%,70% 100%,59% 98.5%,47% 100%,35% 98%,23% 100%,12% 98.5%,3% 100%,.5% 95%,1.5% 83%,0 71%,1.5% 58%,0 45%,1.5% 32%,.5% 19%,1.5% 8%);}
.clip .tape{position:absolute;top:-9px;left:30px;width:80px;height:22px;background:var(--tape);
  transform:rotate(-3deg);mix-blend-mode:multiply;}
.clip .dchip{font-family:var(--num);font-weight:700;font-size:20px;color:var(--pen);}
.clip .wk{font-family:var(--hand);font-size:14px;color:var(--mut);margin:6px 0 8px;}
.clip h3{font-weight:900;font-size:19px;line-height:1.42;margin:6px 0 6px;}
.clip p{font-size:13.5px;line-height:1.7;color:var(--ink2);text-align:justify;}
.clip p b{font-family:var(--hand);color:var(--pen);font-weight:400;}
.clip .cmeta{font-family:var(--hand);font-size:13px;color:var(--mut);margin-top:10px;}
.clip .cmeta::before{content:'✂ ';color:var(--pen);}
.empty{padding:70px 40px;text-align:center;font-family:var(--hand);font-size:20px;color:var(--mut);line-height:1.9;}
.foot{margin-top:36px;padding:22px 40px 34px;border-top:2.5px dashed #b9b09a;position:relative;
  display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;}
.foot::before{content:'✂';position:absolute;right:38px;top:-14px;font-size:19px;color:var(--mut);transform:rotate(172deg);}
.foot b{font-family:var(--num);font-weight:700;font-size:20px;}
.foot b .cn{font-family:var(--song);font-weight:900;}
.foot span{font-family:var(--hand);font-size:14px;color:var(--ink2);}
@media (max-width:760px){
  .topbar{padding:12px 18px;}
  .hero,.scount{padding-left:18px;padding-right:18px;}
  .qa{margin:22px 14px 0;}
  .search{margin:22px 10px 0;}
  .grid{padding:22px 16px 26px;grid-template-columns:1fr;gap:30px;}
  body::before{left:8px;}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important;}
  .clipw{opacity:1!important;}
}
/* 桌面 App 模式(同 webapp._APP 的 .topdrag 口径):壳无系统标题栏,顶部铺桌面色拖拽带 */
.topdrag{display:none;}
html.inapp .topdrag{display:block;position:fixed;top:0;left:0;right:0;height:36px;z-index:85;
  -webkit-app-region:drag;background:rgba(230,223,201,.72);
  -webkit-backdrop-filter:blur(9px);backdrop-filter:blur(9px);
  border-bottom:1px solid rgba(38,34,28,.08);}
html.inapp body{padding-top:34px;}
html.inapp .topbar{top:36px;}
</style></head><body>
<script>if(navigator.userAgent.includes('Electron'))document.documentElement.classList.add('inapp');</script>
<div class="topdrag"></div>
<div class="grain"></div>
<div class="topbar">
  <a class="back" href="../app/index.html"><i>←</i> 回今日册子</a>
  <div class="brand">Pulse<em>wire</em><span class="tag">往期剪报<i></i></span></div>
</div>
<div class="hero"><div class="dl" id="meta"></div></div>
<div class="qa" id="qa">
  <div class="qa-head">✍ 问历史 —— AI 据真卡回答,每句标来源</div>
  <div class="qa-row">
    <input id="qq" placeholder="想问什么?如「最近中东局势怎么样」「上次说的烤机是咋回事」">
    <button class="qa-btn" id="qbtn">问</button>
  </div>
  <div class="qa-ans" id="qans"></div>
</div>
<div class="search"><input id="q" placeholder="🔍 翻全部往期(标题 / 速读 / 来源,字面匹配)…"></div>
<div id="out"></div>
<div class="foot"><b>Pulse<span class="cn">wire</span></b><span>每日自动归档 · 数字均对照原文核验</span></div>
<script>const INDEX=__INDEX__;</script>
<script>
const ROTS=[-1.4,1.1,-.8,1.5,-1.1,.8,-1.6,1.2];
const WK=['周日','周一','周二','周三','周四','周五','周六'];
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $=id=>document.getElementById(id);
const DAYS=INDEX.days||[];
const ITEMS=[];
DAYS.forEach(d=>d.items.forEach(it=>ITEMS.push({...it,date:d.date})));
$('meta').textContent=`共 ${DAYS.length} 本 · ${ITEMS.length} 张剪报 · ${DAYS.length?DAYS[DAYS.length-1].date+' ~ '+DAYS[0].date:'暂无归档'}`;
const wkOf=d=>{const m=d.match(/^(\d{4})-(\d{2})-(\d{2})$/);return m?WK[new Date(+m[1],+m[2]-1,+m[3]).getDay()]:'';};
function dayCards(){
  if(!DAYS.length)return '<div class="empty">还没有归档——明天这个时候再来。</div>';
  let h='<div class="grid">';
  DAYS.forEach((d,i)=>{
    const top=d.top?`<p><b>头条</b> ${esc(d.top)}</p>`:'';
    h+=`<div class="clipw" style="--rot:${ROTS[i%8]}deg;--d:${(i%8)*.06}s"><a class="clip" href="daily/${esc(d.date)}.html">
      <div class="tape"></div>
      <div class="dchip">${esc(d.date)}</div>
      <div class="wk">${wkOf(d.date)} · ${d.n} 张剪报</div>
      ${top}
      <div class="cmeta">${esc(d.doms.join(' / '))}</div></a></div>`;
  });
  return h+'</div>';
}
function hits(q){
  const lo=q.toLowerCase();
  const list=ITEMS.filter(it=>(it.h+' '+it.t+' '+it.s).toLowerCase().includes(lo));
  const hl=t=>esc(t).replace(new RegExp('('+q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig'),'<mark>$1</mark>');
  let h=`<div class="scount">翻到 ${list.length} 张 · 点卡片进当天册子</div><div class="grid">`;
  list.slice(0,200).forEach((it,i)=>{h+=`<div class="clipw" style="--rot:${ROTS[i%8]}deg;--d:${(i%8)*.05}s"><a class="clip" href="daily/${esc(it.date)}.html">
    <div class="tape"></div>
    <div class="dchip">${esc(it.date.slice(5))}</div>
    <h3>${hl(it.h)}</h3><p>${hl(it.t)}</p>
    <div class="cmeta">${esc(it.dom)} · ${esc(it.s)}</div></a></div>`;});
  h+='</div>';
  if(list.length>200)h+=`<div class="scount" style="padding-bottom:20px">只摊开前 200 张,加个关键词收窄。</div>`;
  return h;
}
function render(){
  const q=$('q').value.trim(); $('out').innerHTML=q?hits(q):dayCards();
}
document.addEventListener('input',e=>{if(e.target.id==='q')render();});
render();
// —— 问答面板:经 Electron 预载桥调 `pulsewire ask --json`;浏览器直开无桥 → 降级成提示 ——
// ⚠️ 桥协议(window.pulsewire.ask / res.ok/enough/answer/cards)与桌面 App 预载脚本咬合,勿改。
(function(){
  const qq=$('qq'), qbtn=$('qbtn'), qans=$('qans');
  const bridge=window.pulsewire&&window.pulsewire.ask;
  if(!bridge){  // 浏览器里没有 Electron 桥:禁用问答、引导去桌面 App,关键词检索仍可用
    qq.disabled=true; qbtn.disabled=true; qq.placeholder='问答需在 Pulsewire 桌面 App 里使用';
    qans.innerHTML='<div class="qa-hint">💡 用桌面 App 打开往期就能向 AI 提问;浏览器里可以用下面的关键词翻找。</div>';
    return;
  }
  let asking=false;
  function renderAns(res){
    if(!res||!res.ok){qans.innerHTML='<div class="qa-err">⚠️ 问答暂时不可用 — 多半 Docker 没起来,稍等它开好再问,或看运行日志。</div>';return;}
    if(!res.enough){qans.innerHTML='<div class="qa-hint">档案里没找到相关内容,换个说法、或换个问题试试。</div>';return;}
    const ans=esc(res.answer).replace(/\[(\d+)\]/g,'<sup>[$1]</sup>');
    const cites=(res.cards||[]).map(c=>`<a class="qa-cite" href="daily/${esc(c.date)}.html">[${c.n}] ${esc(c.date||'')} · ${esc(c.headline)}</a>`).join('');
    qans.innerHTML=`<div class="qa-answer">${ans}</div>`+(cites?`<div class="qa-cites"><div class="lbl">引用来源 · 点击进当天册子</div>${cites}</div>`:'');
  }
  async function ask(){
    const q=qq.value.trim(); if(!q||asking)return;
    asking=true; qbtn.disabled=true;
    qans.innerHTML='<div class="qa-think">🤔 思考中…(首次提问要加载模型;若 Docker 没开会自动帮你开,可能要等一两分钟)</div>';
    let res; try{res=await bridge(q);}catch(err){res={ok:false,error:String(err)};}
    asking=false; qbtn.disabled=false; renderAns(res);
  }
  qbtn.addEventListener('click',ask);
  qq.addEventListener('keydown',e=>{if(e.key==='Enter')ask();});
})();
</script></body></html>"""

def snapshot_day(archive_dir: Path, *, date_str: str, html: str, data: dict) -> Path:
    """把当日自包含 App 页 + 数据快照进 daily/(同日重跑覆盖,幂等)。

    主 App 的「档案」链接是 `../archive/index.html`(从 web/app/ 出发);快照住在
    web/archive/daily/ 下,同样的相对路径会指到不存在的 archive/archive/ —— 落盘前改写。
    """
    daily = archive_dir / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / f"{date_str}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    out = daily / f"{date_str}.html"
    out.write_text(
        html.replace('href="../archive/index.html"', 'href="../index.html"'), encoding="utf-8"
    )
    return out


def rerender_all_days(archive_dir: Path) -> int:
    """把 daily/ 下每个 <日期>.json 用当前 _APP 模板整批重渲成 <日期>.html(换皮不动数据)。

    - json 是唯一真相,html 只是渲染产物;HIST 内联空数组(快照不背全史索引,
      「翻本子找」的往期区自然为空,回档案首页翻即可)。
    - 同 snapshot_day:改写档案回链(../archive/index.html → ../index.html)。
    - 单日失败只记日志跳过,绝不 raise(档案错误哲学:次要产物不拖垮)。返回重写张数。
    """
    from .webapp import _APP

    daily = archive_dir / "daily"
    if not daily.exists():
        return 0
    n = 0
    for f in sorted(daily.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            html = (_APP.replace("__DATA__", json.dumps(data, ensure_ascii=False))
                        .replace("__HIST__", "[]")
                        .replace('href="../archive/index.html"', 'href="../index.html"'))
            (daily / f"{f.stem}.html").write_text(html, encoding="utf-8")
            n += 1
        except Exception as exc:  # noqa: BLE001 — 单日坏档跳过,不拖垮整批
            log.warning("archive.rerender.day_skipped", file=f.name, error=str(exc))
    log.info("archive.rerender.done", rewritten=n)
    return n


def _compact_day(data: dict) -> dict:
    """单日数据 → 检索索引条目(只留 headline/tldr/source,控档案首页体积)。"""
    items: list[dict] = []
    doms: list[str] = []
    for d in data.get("domains") or []:
        doms.append(d.get("label", d.get("key", "?")))
        for it in d.get("items", []):
            items.append({"dom": d.get("label", "?"), "h": it.get("headline", ""),
                          "t": it.get("tldr", ""), "s": it.get("source", "")})
    if data.get("github"):
        doms.append("GitHub")
        for it in data["github"]:
            items.append({"dom": "GitHub", "h": it.get("headline", ""),
                          "t": it.get("tldr", ""), "s": it.get("source", "")})
    return {"date": data.get("date", "?"), "doms": doms, "n": len(items),
            "top": items[0]["h"] if items else "", "items": items}


def rebuild_index(archive_dir: Path) -> Path:
    """扫 daily/*.json 重建档案首页(新日在前)。旧系统抢救件(legacy/)只存盘,不挂入口。"""
    daily = archive_dir / "daily"
    days = []
    if daily.exists():
        for f in sorted(daily.glob("*.json"), reverse=True):
            try:
                days.append(_compact_day(json.loads(f.read_text(encoding="utf-8"))))
            except Exception as exc:  # 单日索引坏不拖垮整页,但要吼出来
                log.warning("archive.index.day_skipped", file=f.name, error=str(exc))

    page = _INDEX_PAGE.replace("__INDEX__", json.dumps({"days": days}, ensure_ascii=False))
    out = archive_dir / "index.html"
    archive_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


# --------------------------- 旧系统历史回填(legacy) --------------------------- #
# 把抢救的旧系统文字日报(legacy/digests/*.json)按锁定风格重渲成 daily/<日期> 日页,
# 并入档案首页便签卡 + 检索池。一次性回填:与 pulsewire 已有快照同日则跳过(新系统优先)。

def _tr_lead(insight: str, limit: int = 90) -> str:
    """从 insight 取首句当速读(旧条目无 tldr;机械截取,不编造)。"""
    text = (insight or "").strip()
    for sep in ("。", "!", "?", "!", "?", "\n"):
        idx = text.find(sep)
        if 0 <= idx < limit:
            return text[: idx + 1]
    return text[:limit] + ("…" if len(text) > limit else "")


def _legacy_to_data(tr: dict) -> dict:
    """旧系统单份 JSON → webapp data 结构(domains 驱动「今日」下拉,同主 App)。

    映射:category→domain(label=name, digest=summary),item.title_zh→headline、
    insight 全文→insight、首句→tldr;无 tldr/数字对账,故 needs_review 一律 False、github 空。
    """
    import hashlib

    domains: list[dict] = []
    for i, cat in enumerate(tr.get("report", {}).get("categories", []) or []):
        items = []
        for it in cat.get("items", []) or []:
            url = it.get("url", "")
            headline = it.get("title_zh") or it.get("title") or ""
            insight = it.get("insight", "")
            items.append({
                "id": "tr_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
                "headline": headline,
                "tldr": _tr_lead(insight),
                "insight": insight,
                "source": it.get("source", ""),
                "url": url,
                "needs_review": False,
                "category": cat.get("name", "?"),
            })
        if not items:
            continue
        domains.append({"key": f"tr{i}", "label": cat.get("name", "?"),
                        "digest": cat.get("summary", ""), "items": items})

    return {
        "title": domains[0]["label"] if domains else "往期档案",
        "date": tr.get("date", "?"),
        "digest": "",  # 旧系统无整体概述,只有各领域 summary(进 domain.digest)
        "image": None, "overview_image": None, "github_image": None,
        "items": domains[0]["items"] if domains else [],  # back-compat:主领域条目
        "github": [],
        "domains": domains,
    }


def _merge_legacy_day(datas: list[dict]) -> dict:
    """同一天多份(8 天有 2-3 份):合并各份 domains,域内条目按 url 去重(留先出现的)。"""
    if len(datas) == 1:
        return datas[0]
    merged: dict[str, dict] = {}  # label → domain
    order: list[str] = []
    for data in datas:
        for d in data["domains"]:
            label = d["label"]
            if label not in merged:
                merged[label] = {**d, "items": []}
                order.append(label)
            seen = {it["url"] for it in merged[label]["items"] if it["url"]}
            for it in d["items"]:
                # 只按非空 url 去重;空 url 不参与(其 id=sha1("") 也不唯一),全保留免误合并
                if it["url"] and it["url"] in seen:
                    continue
                if it["url"]:
                    seen.add(it["url"])
                merged[label]["items"].append(it)
    base = dict(datas[0])
    base["domains"] = [merged[label] for label in order]
    base["items"] = base["domains"][0]["items"] if base["domains"] else []
    base["title"] = base["domains"][0]["label"] if base["domains"] else "往期档案"
    return base


def import_legacy_history(archive_dir: Path = ARCHIVE_DIR, *, rebuild: bool = True) -> dict:
    """把 legacy/digests/*.json 重渲成锁定风格 daily/<日期> 日页,并入档案。

    - 同日多份合并去重;与 pulsewire 已有 daily/<日期>.json 撞期则跳过(新系统优先,不覆盖)。
    - 复用 webapp `_APP` 模板渲染(视觉与主 App 一致),snapshot_day 落盘 + 改写档案链接。
    - 末尾 rebuild_index 让老日进便签卡 + 检索池。返回 {imported, skipped, dates}。
    """
    from .webapp import _APP

    digests = archive_dir / "legacy" / "digests"
    daily = archive_dir / "daily"
    by_date: dict[str, list[dict]] = {}
    for f in sorted(digests.glob("*.json")):
        try:
            data = _legacy_to_data(json.loads(f.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning("archive.legacy.parse_skipped", file=f.name, error=str(exc))
            continue
        by_date.setdefault(data["date"], []).append(data)

    imported, skipped = [], []
    for date_str, datas in sorted(by_date.items()):
        if (daily / f"{date_str}.json").exists():
            skipped.append(date_str)  # pulsewire 已有当日快照,新系统优先,不覆盖
            continue
        data = _merge_legacy_day(datas)
        # __HIST__ 也必须替换:漏掉会让页面 JS 报 ReferenceError 整页白渲
        html = (_APP.replace("__DATA__", json.dumps(data, ensure_ascii=False))
                    .replace("__HIST__", "[]"))
        snapshot_day(archive_dir, date_str=date_str, html=html, data=data)
        imported.append(date_str)

    if rebuild:
        rebuild_index(archive_dir)
    log.info("archive.legacy.imported", imported=len(imported), skipped=len(skipped),
             skipped_dates=skipped)
    return {"imported": imported, "skipped": skipped, "dates": sorted(by_date)}
