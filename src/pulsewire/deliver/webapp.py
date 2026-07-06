"""网页 App 交付(零后端 SPA):「今日剪报本」——四板剪报 + 在追 + 检索 + 收藏本 + 往期。

锁定视觉「剪报涂鸦风」(新闻纸+撕边剪报+和纸胶带+便利贴批注+红笔涂鸦,见 STYLE.md,
2026-07-04 用户拍板换代);数据内联 → file:// 双击直看;收藏/隐藏线存 localStorage
(键 pw.fav / pw.hidethr,与旧版兼容,_uid=板key:id)。
结构=一本册子滚到底:报头 → 导读条 → 索引标签 → 编者按 → 各板剪报页 → 在追线索页 → 落款;
检索为覆盖层(命中 <mark> 高亮,点击跳回原剪报),收藏本为右侧抽屉。
字体走系统字体(宋体/手札体/Georgia),无外链(离线可看)。动效:剪报贴入/红笔画圈/荧光扫/星数跳表。
文案铁律(2026-07-04 用户):不写广告语/口号,版块不编号(平权),只留事实。
PNG 出图的静态版视觉同源,在 render/templates.py。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pulsewire.config import PROJECT_ROOT
from pulsewire.obs import get_logger

from .base import ChannelResult, DeliverPayload

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

# __DATA__ 处替换为内联 JSON。
_APP = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pulsewire 日报</title>
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
mark{background:linear-gradient(180deg,transparent 42%,var(--hlt) 42% 88%,transparent 88%);
  color:inherit;padding:0 1px;}

svg.doodle{position:absolute;pointer-events:none;overflow:visible;}
svg.doodle path{fill:none;stroke:var(--pen);stroke-width:3.2;stroke-linecap:round;stroke-linejoin:round;
  stroke-dasharray:1200;stroke-dashoffset:1200;}
svg.doodle.obj path{stroke:#7d786e;stroke-width:2.6;}
.in svg.doodle path,.loaded svg.doodle.md path{transition:stroke-dashoffset 1.1s ease .25s;stroke-dashoffset:0;}

/* ================= 本子封面头 ================= */
.mast{padding:34px 44px 6px;position:relative;}
@media (min-width:781px){.mast{padding-right:246px;}}
.tapecorner{position:absolute;width:112px;height:30px;background:var(--tape);
  box-shadow:0 2px 5px rgba(38,34,28,.14);mix-blend-mode:multiply;}
.tapecorner.tl{left:-24px;top:16px;transform:rotate(-38deg);}
.tapecorner.tr{right:-24px;top:16px;transform:rotate(38deg);}
.dateline{font-family:var(--hand);font-size:16.5px;color:var(--ink2);letter-spacing:.06em;
  animation:fadedown .5s ease both;position:relative;display:inline-block;}
.dateline .wavy{position:absolute;left:0;right:0;bottom:-9px;height:10px;}
.np{position:relative;display:inline-block;margin-top:10px;
  animation:inkin .8s .15s cubic-bezier(.22,1,.36,1) both;}
.np .en{font-family:var(--num);font-weight:700;font-size:clamp(42px,7vw,72px);
  letter-spacing:-.01em;line-height:1;}
.np .cn{position:relative;font-weight:900;font-size:clamp(28px,4.6vw,50px);margin-left:12px;letter-spacing:.1em;}
.np .circ{left:-14px;top:-16px;width:calc(100% + 28px);height:calc(100% + 30px);}
.np .starr{right:-46px;top:-24px;width:36px;height:36px;}
.mastnote{position:absolute;right:44px;top:30px;width:168px;padding:14px 15px 18px;
  background:var(--postit);transform:rotate(3deg);font-family:var(--hand);
  font-size:16px;line-height:1.55;color:#5d5426;box-shadow:0 8px 14px -6px rgba(38,34,28,.3);
  animation:slap .5s .95s cubic-bezier(.34,1.6,.64,1) both;--nr:3deg;z-index:6;}
.mastnote::before{content:'';position:absolute;top:-9px;left:50%;width:64px;height:20px;margin-left:-32px;
  background:var(--tape);transform:rotate(-2deg);box-shadow:0 1px 4px rgba(38,34,28,.15);}
.mastnote i{font-style:normal;color:var(--pen);}
.mastnote .bignum{font-family:var(--num);font-size:30px;color:var(--ink);display:block;line-height:1.1;}

.cutline{margin:22px 44px 0;border:none;border-top:2.5px dashed #b9b09a;position:relative;
  animation:fadein .5s .5s both;}
.cutline::before{content:'✂';position:absolute;left:-6px;top:-14px;font-size:19px;color:var(--mut);
  transform:rotate(-8deg);}
.wire{display:flex;align-items:center;gap:12px;padding:9px 44px 0;overflow:hidden;
  animation:fadein .5s .7s both;}
.wire .tag{flex:none;font-family:var(--hand);font-size:16px;color:var(--pen);}
.wire .belt{overflow:hidden;flex:1;}
.wire .run{display:inline-block;white-space:nowrap;font-family:var(--hand);font-size:15px;
  color:var(--mut);will-change:transform;animation:wirerun 44s linear infinite;}
.wire .run i{font-style:normal;color:var(--pen);margin:0 12px;}
@keyframes wirerun{from{transform:translateX(0)}to{transform:translateX(-50%)}}

/* ============ 先看这几张(导读撕纸条) ============ */
.lookfirst{position:relative;margin:20px 32px 0;padding:13px 20px 15px;background:#FBF7EA;
  display:flex;gap:8px 22px;align-items:center;flex-wrap:wrap;
  clip-path:polygon(0 18%,3% 4%,9% 14%,16% 2%,24% 12%,33% 0,42% 10%,52% 2%,61% 12%,70% 1%,79% 11%,88% 3%,95% 13%,100% 5%,100% 84%,96% 97%,89% 88%,81% 99%,72% 89%,62% 100%,52% 90%,42% 99%,32% 88%,22% 98%,13% 87%,6% 97%,0 86%);
  animation:fadein .5s .85s both;}
.lookfirst .lf-tag{font-family:var(--hand);font-size:17px;color:var(--pen);flex:none;}
.lookfirst a{font-family:var(--hand);font-size:15.5px;color:var(--ink2);text-decoration:none;
  transition:color .15s;}
.lookfirst a:hover{color:var(--pen);}
.lookfirst a b{color:var(--pen);font-weight:400;margin-right:4px;}

/* ================= 索引标签导航(sticky) ================= */
.bnav{position:sticky;top:0;z-index:80;display:flex;gap:6px;align-items:flex-end;
  padding:10px 44px 0;background:var(--paper);border-bottom:2px dashed #cfc6ac;
  overflow-x:auto;scrollbar-width:none;}
.bnav::-webkit-scrollbar{display:none;}
.bnav a{flex:none;font-family:var(--hand);font-size:16px;color:var(--ink);text-decoration:none;
  background:var(--tabc,#eee);padding:8px 17px 10px;border-radius:10px 10px 0 0;
  border:1.5px solid rgba(38,34,28,.25);border-bottom:none;transform:translateY(4px) rotate(var(--tr,0deg));
  box-shadow:0 -3px 8px -4px rgba(38,34,28,.2);transition:transform .18s;}
.bnav a:hover{transform:translateY(0) rotate(0);}
.bnav a.on{transform:translateY(0) rotate(0);font-weight:700;
  box-shadow:0 -5px 10px -4px rgba(38,34,28,.28);}
.bnav .brand{margin-right:auto;font-family:var(--num);font-weight:700;font-size:19px;
  background:none;border:none;box-shadow:none;transform:none;padding:8px 0 10px;}
.bnav .brand em{color:var(--pen);font-style:italic;}
.bnav .q{flex:none;align-self:center;margin-left:6px;display:flex;align-items:center;gap:6px;
  background:#fff;border:1.5px dashed #cfc6ac;border-radius:16px;padding:5px 12px;}
.bnav .q input{border:none;outline:none;background:none;font-family:var(--hand);font-size:14.5px;
  color:var(--ink);width:110px;}
.bnav .q .qi{color:var(--mut);font-size:13px;}

/* ================= 编者按大便利贴 ================= */
.edwrap{padding:26px 44px 0;display:flex;justify-content:center;}
.edpost{position:relative;max-width:640px;background:var(--postit);padding:26px 30px 30px;
  transform:rotate(-1.2deg);box-shadow:0 12px 22px -10px rgba(38,34,28,.35);
  animation:slap .55s 1.15s cubic-bezier(.34,1.6,.64,1) both;--nr:-1.2deg;}
.edpost::after{content:'';position:absolute;right:0;bottom:0;border:14px solid transparent;
  border-right-color:var(--paper);border-bottom-color:var(--paper);
  box-shadow:-3px -3px 6px -3px rgba(38,34,28,.25);}
.edpost .pin{position:absolute;top:-11px;left:38px;width:20px;height:20px;border-radius:50%;
  background:radial-gradient(circle at 35% 30%,#ff8a76,var(--pen) 65%);
  box-shadow:0 4px 7px -2px rgba(38,34,28,.5);}
.edpost h5{font-family:var(--hand);font-size:19px;color:var(--pen);margin-bottom:8px;}
.edpost p{font-family:var(--hand);font-size:18.5px;line-height:1.8;color:#4a431f;text-align:justify;}

/* ================= 版块页 ================= */
.page{padding:52px 44px 0;scroll-margin-top:64px;}
.phead{position:relative;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}
.phead h2{position:relative;font-size:clamp(26px,4vw,36px);font-weight:900;letter-spacing:.1em;z-index:1;}
.phead h2 .swipe{position:absolute;left:-8px;right:-10px;bottom:2px;height:.58em;z-index:-1;
  background:var(--hlt);transform:scaleX(0);transform-origin:left;opacity:.85;
  transition:transform .6s cubic-bezier(.22,1,.36,1) .15s;}
.page.in .phead h2 .swipe{transform:scaleX(1);}
.phead .cnt{font-family:var(--hand);font-size:17px;color:var(--ink2);}
.phead .cnt i{font-style:normal;font-family:var(--num);color:var(--pen);font-size:20px;}
.phead .arrow{position:relative;width:64px;height:30px;align-self:center;}
.pdig{font-family:var(--hand);font-size:16.5px;line-height:1.75;color:var(--ink2);
  margin-top:12px;max-width:62ch;}
.pdig::before{content:'☞ ';color:var(--pen);}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:40px 36px;padding:30px 2px 4px;}
.clipwrap{filter:drop-shadow(0 12px 12px rgba(38,34,28,.17));
  opacity:0;transform:translateY(26px) rotate(var(--rot,0deg)) scale(.97);
  transition:opacity .5s ease,transform .55s cubic-bezier(.3,1.4,.5,1),filter .2s;}
.page.in .clipwrap{opacity:1;transform:rotate(var(--rot,0deg));}
.clipwrap:hover{transform:rotate(0) translateY(-7px);filter:drop-shadow(0 18px 16px rgba(38,34,28,.22));z-index:5;position:relative;}
.clipwrap.big{grid-column:1/-1;}
.clip{position:relative;background:#FCFAF3;padding:26px 24px 20px;cursor:pointer;
  clip-path:polygon(1% 2.5%,7% .5%,15% 2%,26% 0,37% 2.5%,50% .5%,62% 2.5%,73% 0,84% 2%,93% .5%,99.5% 3%,99% 14%,100% 27%,98.5% 40%,100% 53%,99% 66%,100% 79%,98.5% 90%,99.5% 97%,93% 99.5%,82% 98%,70% 100%,59% 98.5%,47% 100%,35% 98%,23% 100%,12% 98.5%,3% 100%,.5% 95%,1.5% 83%,0 71%,1.5% 58%,0 45%,1.5% 32%,.5% 19%,1.5% 8%);}
.clip.t2{clip-path:polygon(.5% 4%,9% 1%,19% 3%,31% .5%,44% 2.5%,57% 0,69% 2%,80% .5%,90% 2.5%,99% 1%,99.5% 12%,98.5% 25%,100% 38%,99% 51%,100% 64%,98.5% 77%,100% 89%,98% 98%,90% 99.5%,79% 98%,67% 100%,55% 98%,43% 100%,31% 98.5%,19% 100%,9% 98%,1% 99.5%,0 88%,1.5% 76%,.5% 63%,1.5% 50%,0 37%,1.5% 24%,.5% 12%);}
.tape1{position:absolute;top:-10px;left:34px;width:88px;height:24px;background:var(--tape);
  transform:rotate(-3deg);box-shadow:0 2px 5px rgba(38,34,28,.15);mix-blend-mode:multiply;}
.tape2{position:absolute;top:-8px;right:30px;width:70px;height:22px;background:rgba(247,205,150,.55);
  transform:rotate(5deg);box-shadow:0 2px 5px rgba(38,34,28,.15);mix-blend-mode:multiply;}
.pclip{position:absolute;top:-16px;left:14px;width:26px;height:52px;z-index:4;transform:rotate(-7deg);}
.cutfrom{font-family:var(--hand);font-size:14px;color:var(--mut);}
.cutfrom i{font-style:normal;color:var(--pen);}
.clip h3{position:relative;font-size:20.5px;font-weight:900;line-height:1.42;margin:8px 0 8px;text-wrap:balance;}
.clipwrap.big .clip h3{font-size:clamp(23px,3vw,29px);}
.clipwrap.big .clip h3 .leadline{left:0;right:0;bottom:-8px;height:10px;}
.clip .body{font-size:14.5px;line-height:1.78;text-align:justify;color:var(--ink2);}
.clipwrap.big .clip .body{columns:2;column-gap:38px;column-rule:1px dashed #d8d0ba;}
.clip .repo{display:inline-block;font-family:var(--num);font-weight:700;font-size:13.5px;color:var(--ink);
  background:rgba(38,34,28,.05);border:1.5px dashed #c9c0a8;border-radius:5px;padding:3px 10px;margin:2px 0 8px;}
.clip .repo::before{content:'✂ ';color:var(--pen);}
.rvmark{position:absolute;top:10px;right:14px;font-family:var(--hand);font-size:15px;color:var(--pen);
  transform:rotate(7deg);padding:5px 9px;z-index:2;}
.rvmark svg{left:-4px;top:-4px;width:calc(100% + 8px);height:calc(100% + 8px);}
.leadstar{position:absolute;top:-16px;left:-14px;width:44px;height:44px;z-index:3;}
.trkmark{display:inline-block;font-family:var(--hand);font-size:13.5px;color:var(--pen);cursor:pointer;
  border:1.5px solid var(--pen);border-radius:5px;padding:0 8px 1px;transform:rotate(-3deg);opacity:.85;}
.ops{display:flex;gap:18px;margin-top:12px;font-family:var(--hand);font-size:15.5px;color:var(--ink2);
  align-items:center;flex-wrap:wrap;}
.ops a,.ops button{font-family:var(--hand);font-size:15.5px;color:var(--ink2);background:none;border:none;
  cursor:pointer;text-decoration:none;padding:0;transition:color .15s;}
.ops a:hover,.ops button:hover{color:var(--pen);}
.ops .mk{display:inline-block;transition:transform .3s;}
.open .ops .mk{transform:rotate(90deg);}
.ops .fav.on{color:var(--pen);}
.sticky{max-height:0;overflow:hidden;transition:max-height .4s ease;}
.open .sticky{max-height:900px;}
.sticky .note{position:relative;margin:14px 4px 6px;padding:16px 18px 18px;background:var(--postit);
  transform:rotate(-.8deg);box-shadow:0 8px 14px -7px rgba(38,34,28,.3);
  font-family:var(--hand);font-size:16px;line-height:1.75;color:#4a431f;text-align:justify;}
.clipwrap:nth-child(even) .sticky .note{background:var(--postit2);color:#6b3a4c;transform:rotate(.9deg);}
.sticky .note::before{content:'';position:absolute;top:-8px;left:26px;width:58px;height:17px;
  background:var(--tape);transform:rotate(-2deg);}
.sticky .note .lbl{color:var(--pen);margin-right:6px;}
.vacant{max-width:430px;margin:34px auto 8px;padding:26px 28px 30px;background:var(--postit);
  transform:rotate(-1.6deg);box-shadow:0 10px 18px -9px rgba(38,34,28,.33);
  font-family:var(--hand);font-size:18px;line-height:1.8;color:#5d5426;text-align:center;}
.vacant i{font-style:normal;color:var(--pen);}

/* ================= 摘星页 ================= */
.ghlist{padding-top:26px;display:flex;flex-direction:column;}
.ghrow{position:relative;display:grid;grid-template-columns:52px 1fr auto;gap:6px 16px;
  align-items:baseline;padding:15px 6px 13px;cursor:pointer;
  opacity:0;transform:translateX(-14px);transition:opacity .45s ease,transform .45s ease;}
.page.in .ghrow{opacity:1;transform:none;}
.ghrow::after{content:'';position:absolute;left:0;right:0;bottom:0;height:6px;
  background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='6' viewBox='0 0 120 6'%3E%3Cpath d='M0,3 Q10,0.5 20,3 T40,3 T60,3 T80,3 T100,3 T120,3' fill='none' stroke='%23cabfa2' stroke-width='1.4'/%3E%3C/svg%3E") repeat-x;}
.ghrow .rank{font-family:var(--hand);font-size:21px;color:var(--pen);}
.ghrow .repo{font-size:17.5px;font-weight:900;line-height:1.4;}
.ghrow .repo .rv{font-family:var(--hand);font-size:13.5px;color:var(--pen);margin-left:8px;}
.ghrow .stars{font-family:var(--hand);font-size:19px;color:var(--pen);white-space:nowrap;font-weight:700;}
.ghrow .stars .cnt{font-family:var(--num);font-size:18px;color:var(--ink);font-variant-numeric:tabular-nums;}
.ghrow .qtldr{grid-column:2/4;font-size:13.5px;line-height:1.65;color:var(--ink2);text-align:justify;}
.ghrow .ghsticky{grid-column:1/4;}
.ghrow:hover .repo{color:var(--pen);}

/* ================= 在追(线索页) ================= */
.tcardw{filter:drop-shadow(0 10px 12px rgba(38,34,28,.15));
  opacity:0;transform:translateY(22px) rotate(var(--rot,0deg));
  transition:opacity .5s ease,transform .5s cubic-bezier(.3,1.4,.5,1);}
.page.in .tcardw{opacity:1;transform:rotate(var(--rot,0deg));}
.tgrid{display:grid;grid-template-columns:1fr 1fr;gap:38px 36px;padding:30px 2px 4px;}
.tcard{position:relative;background:#FCFAF3;padding:24px 22px 18px;
  clip-path:polygon(.5% 4%,9% 1%,19% 3%,31% .5%,44% 2.5%,57% 0,69% 2%,80% .5%,90% 2.5%,99% 1%,99.5% 12%,98.5% 25%,100% 38%,99% 51%,100% 64%,98.5% 77%,100% 89%,98% 98%,90% 99.5%,79% 98%,67% 100%,55% 98%,43% 100%,31% 98.5%,19% 100%,9% 98%,1% 99.5%,0 88%,1.5% 76%,.5% 63%,1.5% 50%,0 37%,1.5% 24%,.5% 12%);}
.tcard .tape1{left:28px;}
.tchead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
.tchead h3{font-size:18.5px;font-weight:900;line-height:1.4;flex:1;}
.tchead .tdom{font-family:var(--hand);font-size:13px;color:var(--ink2);background:var(--paper);
  border:1px solid #ddd5c0;border-radius:10px;padding:2px 9px;white-space:nowrap;}
.tchead .tdays{font-family:var(--hand);font-size:13.5px;color:var(--pen);white-space:nowrap;}
.tchead .tx{font-family:var(--hand);font-size:13.5px;color:var(--mut);cursor:pointer;white-space:nowrap;}
.tchead .tx:hover{color:var(--pen);}
.tnow{font-family:var(--hand);font-size:14.5px;line-height:1.7;color:#4a431f;background:var(--postit);
  padding:9px 12px;margin:10px 0 4px;transform:rotate(-.4deg);
  box-shadow:0 4px 8px -5px rgba(38,34,28,.3);}
.tnow b{color:var(--pen);font-weight:400;margin-right:6px;}
.tline{margin-top:10px;border-left:2.5px dashed #cabfa2;padding-left:14px;}
.tevt{padding:5px 0;font-size:13.5px;line-height:1.55;}
.tevt .tdate{font-family:var(--num);font-size:12px;color:var(--pen);margin-right:8px;}
.tevt a{color:var(--ink2);text-decoration:none;}
.tevt a:hover{color:var(--pen);}
.tevt .tsrc{font-family:var(--hand);font-size:12px;color:var(--mut);margin-left:7px;}
.threstore{font-family:var(--hand);font-size:14.5px;color:var(--mut);margin-top:16px;}
.threstore b{color:var(--pen);cursor:pointer;font-weight:400;}

/* ================= 检索覆盖层 ================= */
.qlay{position:fixed;inset:0;z-index:90;background:rgba(38,34,28,.35);display:flex;justify-content:center;
  padding:70px 16px 30px;}
.qlay[hidden]{display:none;}
.qpage{width:min(680px,96vw);max-height:100%;overflow-y:auto;background:var(--paper);
  padding:24px 26px 32px;box-shadow:0 22px 60px -20px rgba(38,34,28,.65);position:relative;
  clip-path:polygon(0 1.2%,4% .2%,9% 1%,16% 0,24% .9%,33% 0,42% .8%,52% .1%,61% 1%,70% 0,79% .9%,88% .2%,95% 1%,100% .3%,100% 99%,96% 99.9%,89% 99%,81% 100%,72% 99.2%,62% 100%,52% 99.1%,42% 100%,32% 99%,22% 99.9%,13% 99%,6% 100%,0 99%);}
.qpage h4{font-family:var(--hand);font-size:20px;color:var(--pen);margin-bottom:2px;}
.qpage .sub{font-family:var(--hand);font-size:13.5px;color:var(--mut);margin-bottom:12px;}
.qclose{position:absolute;top:16px;right:18px;background:none;border:none;font-size:16px;
  color:var(--ink2);cursor:pointer;font-family:var(--hand);}
.qsec{font-family:var(--hand);font-size:15px;color:var(--pen);margin:14px 0 2px;}
.qitem{display:block;padding:12px 2px 12px 8px;border-bottom:1.5px dashed #d8d0ba;cursor:pointer;
  color:inherit;text-decoration:none;}
.qitem .b{font-family:var(--hand);font-size:12.5px;color:var(--pen);}
.qitem .h{font-size:15px;font-weight:900;line-height:1.5;margin-top:3px;}
.qitem .t{font-size:13px;color:var(--ink2);line-height:1.6;margin-top:3px;}
.qempty{font-family:var(--hand);font-size:16px;color:var(--mut);line-height:1.8;margin-top:16px;}
.qarch{display:block;margin-top:16px;padding:11px 14px;background:var(--postit);border-radius:6px;
  font-family:var(--hand);font-size:14.5px;color:#5d5426;text-decoration:none;line-height:1.6;
  box-shadow:0 5px 10px -6px rgba(38,34,28,.3);transform:rotate(-.5deg);}
.qarch:hover{color:var(--pen);}

/* ================= 收藏本抽屉 ================= */
.favtab{position:fixed;right:-6px;bottom:110px;z-index:88;background:var(--postit2);color:#6b3a4c;
  font-family:var(--hand);font-size:16px;border:none;padding:10px 18px 10px 14px;cursor:pointer;
  transform:rotate(-2deg);box-shadow:0 8px 16px -6px rgba(38,34,28,.4);border-radius:8px 0 0 8px;
  transition:transform .18s;}
.favtab:hover{transform:rotate(0) translateX(-4px);}
.favtab i{font-style:normal;font-family:var(--num);color:var(--pen);}
.favlay{position:fixed;inset:0;z-index:92;background:rgba(38,34,28,.35);display:flex;
  justify-content:flex-end;}
.favlay[hidden]{display:none;}
.favpage{width:min(400px,92vw);background:var(--paper);height:100%;overflow-y:auto;
  padding:26px 24px 40px;box-shadow:-16px 0 40px -18px rgba(38,34,28,.6);position:relative;}
.favpage::before{content:'';position:absolute;left:10px;top:0;bottom:0;width:1px;
  background:repeating-linear-gradient(180deg,transparent 0 30px,#cabfa2 30px 31px);}
.favpage h4{font-family:var(--hand);font-size:22px;color:var(--pen);margin-bottom:4px;}
.favpage .sub{font-family:var(--hand);font-size:14px;color:var(--mut);margin-bottom:14px;}
.favclose{position:absolute;top:18px;right:18px;background:none;border:none;font-size:20px;
  color:var(--ink2);cursor:pointer;font-family:var(--hand);}
.favitem{padding:12px 2px 12px 10px;border-bottom:1.5px dashed #d8d0ba;cursor:pointer;}
.favitem .b{font-family:var(--hand);font-size:13px;color:var(--pen);}
.favitem .h{font-size:15px;font-weight:900;line-height:1.5;margin-top:3px;}
.favitem .rm{font-family:var(--hand);font-size:13px;color:var(--mut);background:none;border:none;
  cursor:pointer;margin-top:5px;padding:0;}
.favitem .rm:hover{color:var(--pen);}
.favempty{font-family:var(--hand);font-size:16px;color:var(--mut);line-height:1.8;margin-top:20px;}

/* ====== 快照页左上「回往期」贴(仅 archive/daily/ 里打开时 JS 亮起,主 App 无) ====== */
.backpost{position:fixed;left:-6px;top:14px;z-index:86;display:inline-flex;align-items:center;gap:7px;
  min-height:40px;background:var(--postit2);color:#6b3a4c;font-family:var(--hand);font-size:16.5px;
  text-decoration:none;padding:9px 18px 10px 20px;border-radius:0 8px 8px 0;
  transform:rotate(-2deg);box-shadow:0 8px 16px -6px rgba(38,34,28,.4);transition:transform .18s;}
.backpost i{font-style:normal;color:var(--pen);font-weight:700;font-size:19px;line-height:1;}
.backpost::after{content:'';position:absolute;top:-8px;right:16px;width:44px;height:14px;
  background:var(--tape);transform:rotate(3deg);mix-blend-mode:multiply;}
.backpost:hover{transform:rotate(0) translateX(4px);}
.backpost[hidden]{display:none;}
body.snap .mast{padding-top:76px;}

/* ================= 收工落款 + 纸篓 ================= */
.sign{margin-top:64px;padding:26px 44px 46px;border-top:2.5px dashed #b9b09a;position:relative;
  display:flex;justify-content:space-between;align-items:flex-end;gap:22px;flex-wrap:wrap;}
.sign::before{content:'✂';position:absolute;right:38px;top:-14px;font-size:19px;color:var(--mut);transform:rotate(172deg);}
.coffee{position:absolute;left:30%;top:-40px;width:104px;height:104px;border-radius:50%;
  border:9px solid rgba(139,94,45,.11);transform:rotate(-12deg) scaleX(1.06);pointer-events:none;}
.sign .hand1{font-family:var(--hand);font-size:18px;line-height:1.8;color:var(--ink2);position:relative;}
.sign .hand1 b{color:var(--pen);font-weight:400;}
.sign small{font-family:var(--hand);font-size:14px;color:var(--mut);display:block;margin-top:10px;}
.bin{position:relative;flex:none;width:210px;text-align:center;}
.bin .binsvg{position:relative;width:96px;height:86px;margin:0 auto;display:block;}
.balls{position:absolute;top:-24px;left:50%;width:150px;margin-left:-75px;display:flex;justify-content:center;gap:8px;}
.ball{position:relative;width:34px;height:34px;border-radius:50%;
  background:radial-gradient(circle at 34% 30%,#fffdf6,#d9d2bd 72%);
  box-shadow:inset -3px -4px 6px rgba(38,34,28,.18),0 3px 5px -2px rgba(38,34,28,.3);
  transform:translateY(var(--by,0)) rotate(var(--br,0deg));}
.ball::after{content:'';position:absolute;inset:6px;border-radius:50%;
  background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 22 22'%3E%3Cpath d='M4,11 Q8,4 12,10 T19,9 M3,15 Q9,12 13,16 M8,5 Q12,8 16,5' fill='none' stroke='%23a89f88' stroke-width='1.2' stroke-linecap='round'/%3E%3C/svg%3E") center/contain no-repeat;}
.bin .lab{font-family:var(--hand);font-size:14.5px;color:var(--mut);margin-top:6px;line-height:1.6;}
.bin .lab i{font-style:normal;color:var(--pen);}

@keyframes fadedown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes inkin{from{opacity:0;filter:blur(7px);transform:translateY(10px)}to{opacity:1;filter:blur(0);transform:none}}
@keyframes slap{0%{opacity:0;transform:scale(1.35) rotate(9deg)}70%{opacity:1;transform:scale(.97) rotate(var(--nr,3deg))}
  100%{opacity:1;transform:scale(1) rotate(var(--nr,3deg))}}

@media (max-width:780px){
  .mast{padding:22px 20px 6px;}
  .mastnote{position:static;width:100%;margin-top:18px;transform:rotate(1.5deg);--nr:1.5deg;}
  .cutline{margin-left:20px;margin-right:20px;}
  .wire{padding:9px 20px 0;}
  .lookfirst{margin:18px 14px 0;}
  .bnav{padding:10px 20px 0;}
  .edwrap,.page{padding-left:20px;padding-right:20px;}
  .grid,.tgrid{grid-template-columns:1fr;gap:34px;}
  .clipwrap.big .clip .body{columns:1;}
  .np .starr{display:none;}
  .sign{padding:22px 20px 40px;}
  .coffee{display:none;}
  body::before{left:8px;}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important;}
  .clipwrap,.ghrow,.tcardw{opacity:1!important;transform:rotate(var(--rot,0deg))!important;}
  .phead h2 .swipe{transform:scaleX(1)!important;}
  svg.doodle path{stroke-dashoffset:0!important;}
}
/* ================= 桌面 App 模式(Electron 壳,hiddenInset 无系统标题栏) =================
   浏览器里 .topdrag 不显示、零变化;App 里(UA 含 Electron)顶端铺一条桌面色半透明拖拽带
   (-webkit-app-region:drag,红绿灯浮其上),册子整体下移让出位置——壳与纸面从此一个色系。
   z=85:压过 sticky 导航(80)、让路给搜索/收藏浮层(90/92,浮层顶部按钮不被挡)。 */
.topdrag{display:none;}
html.inapp .topdrag{display:block;position:fixed;top:0;left:0;right:0;height:36px;z-index:85;
  -webkit-app-region:drag;background:rgba(230,223,201,.72);
  -webkit-backdrop-filter:blur(9px);backdrop-filter:blur(9px);
  border-bottom:1px solid rgba(38,34,28,.08);}
html.inapp body{padding-top:34px;}
html.inapp .bnav{top:36px;}
html.inapp .backpost{top:48px;}
</style></head><body>
<script>if(navigator.userAgent.includes('Electron'))document.documentElement.classList.add('inapp');</script>
<div class="topdrag"></div>

<div class="grain"></div>

<header class="mast" id="top">
  <div class="tapecorner tl"></div><div class="tapecorner tr"></div>
  <div class="dateline">
    <span id="dl"></span>
    <svg class="doodle wavy md" viewBox="0 0 220 10" preserveAspectRatio="none"><path d="M2,6 Q16,1 30,6 T58,6 T86,6 T114,6 T142,6 T170,6 T198,6 T218,6"/></svg>
  </div>
  <br>
  <h1 class="np">
    <span class="en">Pulsewire</span><span class="cn">日报
      <svg class="doodle circ md" viewBox="0 0 140 74"><path d="M12,40 C8,16 44,6 74,8 C108,10 134,20 130,40 C126,60 88,70 56,66 C26,62 10,54 13,36 C16,22 40,12 68,13"/></svg>
    </span>
    <svg class="doodle starr md" viewBox="0 0 40 40"><path d="M20,3 L24,15 L37,15 L27,23 L31,36 L20,28 L9,36 L13,23 L3,15 L16,15 Z"/></svg>
  </h1>
  <aside class="mastnote"><span class="bignum" id="notetotal"></span>今天剪下的报道<br><i>每日 15:00 更新</i></aside>
</header>

<hr class="cutline">
<div class="wire"><span class="tag">✂ 今日剪自</span><div class="belt"><div class="run" id="wirerun"></div></div></div>

<div class="lookfirst"><span class="lf-tag">赶时间?先看这几张 ☞</span><span id="lfline"></span></div>

<nav class="bnav" id="bnav"><span class="brand">P<em>w</em></span></nav>

<section class="edwrap"><aside class="edpost">
  <span class="pin"></span>
  <h5>编辑今天想说 ——</h5>
  <p id="edtext"></p>
</aside></section>

<main id="book"></main>

<footer class="sign">
  <div class="coffee"></div>
  <div class="hand1">到这儿就贴完了。<br>
    <b>数字均对照原文核验</b>,拿不准的标了「待核实」。
    <small id="signmeta"></small>
  </div>
  <div class="bin">
    <div class="balls">
      <span class="ball" style="--by:2px;--br:14deg"></span>
      <span class="ball" style="--by:-7px;--br:-20deg"></span>
      <span class="ball" style="--by:1px;--br:40deg"></span>
    </div>
    <svg class="doodle obj binsvg" viewBox="0 0 96 86"><path d="M14,22 L82,22 M18,22 L26,80 L70,80 L78,22 M34,32 L37,70 M48,32 L48,70 M62,32 L59,70"/></svg>
    <div class="lab">今日未入选:<br><i>水货</i> · <i>跑题</i> · <i>不够格</i></div>
  </div>
</footer>

<a class="backpost" id="backpost" href="../index.html" hidden><i>←</i> 回往期</a>
<button class="favtab" id="favtab">❤ 收藏本 <i id="favn">0</i></button>
<div class="favlay" id="favlay" hidden>
  <div class="favpage">
    <button class="favclose" id="favclose" aria-label="关上收藏本">✕ 合上</button>
    <h4>我的收藏本</h4>
    <div class="sub">点「♡ 收进本子」的剪报都躺在这儿(存在你自己浏览器里)</div>
    <div id="favlist"></div>
  </div>
</div>

<div class="qlay" id="qlay" hidden>
  <div class="qpage">
    <button class="qclose" id="qclose">✕ 收起</button>
    <h4>翻本子找(今天+往期)——</h4>
    <div class="sub" id="qsub"></div>
    <div id="qlist"></div>
  </div>
</div>

<script>
const DATA=__DATA__;
const HIST=__HIST__; /* 往期索引(构建时内联,近180天:d/dom/h/t/s)——首页检索=今天+往期一起翻 */
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
/* 荧光笔:只划带单位的量数(数字均经对账,划出=编辑重点) */
const hl=s=>s.replace(/((?:\$|＄)\s?[\d,.]+\s?[MBK]?|[\d,.]+\s?(?:%|％|亿|万亿|万|美元|美金|倍|GPU|颗星|stars?|forks?))/g,'<mark>$1</mark>');
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;

/* 快照页(archive/daily/ 下打开)左上亮「回往期」贴;主 App 页不出现 */
if(/\/archive\/daily\//.test(location.pathname)){
  document.getElementById('backpost').hidden=false;
  document.body.classList.add('snap');
}

/* 存储:沿用旧版键(pw.fav / pw.hidethr,_uid=板key:id),老收藏无缝继承 */
const LS={get(k,d){try{return JSON.parse(localStorage.getItem('pw.'+k))??d;}catch(e){return d;}},
          set(k,v){localStorage.setItem('pw.'+k,JSON.stringify(v));}};
let FAV=new Set(LS.get('fav',[]));  const saveFav=()=>LS.set('fav',[...FAV]);
let HID=new Set(LS.get('hidethr',[])); const saveHide=()=>LS.set('hidethr',[...HID]);

/* 板块:domains(新闻)+ github(摘星);_uid 与旧版同构 */
const NEWS=(DATA.domains&&DATA.domains.length)?DATA.domains:
  [{key:'ai',label:DATA.title||'AI',digest:DATA.digest||'',items:DATA.items||[]}];
const boards=NEWS.map(b=>({...b,gh:false}));
if((DATA.github||[]).length)boards.push({key:'github',label:'开源摘星',
  digest:'今日 GitHub 上升最快的 AI 相关开源项目。',items:DATA.github,gh:true});
const ALL={};
boards.forEach(b=>b.items.forEach((it,i)=>{
  if(!it.id)it.id=(b.key==='github'?'g':b.key)+i;
  it._uid=b.key+':'+it.id; ALL[it._uid]={it,b};
}));
const total=boards.reduce((n,b)=>n+b.items.length,0);
const TABS=['#FFE787','#FFC9DB','#C9E8D8','#CFE3F7','#EBD9F5','#F7D9C4'];

/* 报头(全事实) */
const wk=(d=>{const t=new Date(d+'T12:00:00');return isNaN(t)?'':'星期'+'日一二三四五六'[t.getDay()];})(DATA.date||'');
document.getElementById('dl').textContent=
  `${DATA.date||''} ${wk}${DATA.issue?` · 总第 ${DATA.issue} 本`:''}`;
document.getElementById('notetotal').textContent=total+' 张';
document.getElementById('edtext').textContent=DATA.digest||'';
/* 老档(旧系统期)无整体编者按:空便利贴不挂 */
if(!DATA.digest)document.querySelector('.edwrap').style.display='none';
document.getElementById('signmeta').textContent=`${DATA.date||''} · Pulsewire`;

/* ticker */
const srcs=[...new Set(boards.flatMap(b=>b.items.map(i=>i.source)).filter(Boolean))];
const belt=srcs.map(esc).join('<i>·</i>')||'…';
document.getElementById('wirerun').innerHTML=belt+'<i>·</i>'+belt+'<i>·</i>';

/* 先看这几张(每板头条) */
document.getElementById('lfline').innerHTML=boards.map(b=>{
  const h=b.items[0]?b.items[0].headline:'(从缺)';
  return `<a href="#pg-${esc(b.key)}"><b>${esc(b.label)}</b>${esc(h.length>16?h.slice(0,16)+'…':h)}</a>`;
}).join(' ');

/* 索引标签 + 在追 + 往期 + 检索框 */
const bnav=document.getElementById('bnav');
boards.forEach((b,i)=>bnav.insertAdjacentHTML('beforeend',
  `<a href="#pg-${esc(b.key)}" data-k="${esc(b.key)}" style="--tabc:${TABS[i%TABS.length]};--tr:${(i%2?1.2:-1.2)}deg">${esc(b.label)}</a>`));
if((DATA.threads||[]).length)bnav.insertAdjacentHTML('beforeend',
  `<a href="#pg-thr" data-k="thr" style="--tabc:#E4E0D2;--tr:1deg">在追</a>`);
bnav.insertAdjacentHTML('beforeend',
  `<a href="../archive/index.html" style="--tabc:#DED8C6;--tr:-1deg">往期</a>
   <span class="q"><span class="qi">🔍</span><input id="q" placeholder="翻本子找(含往期)…" aria-label="检索"></span>`);

/* 剪报卡 */
const rvSVG=`<svg class="doodle" viewBox="0 0 90 40"><path d="M8,22 C6,10 28,4 48,5 C70,6 86,12 84,22 C82,32 60,37 40,35 C20,33 8,30 9,20"/></svg>`;
function clipHTML(it,i,rot,t2,withClip){
  const url=(it.url||'').startsWith('http')?it.url:'';
  const lead=i===0;
  const track=it.thread_id?`<span class="trkmark" data-thr="1">${it.tracking_days?`追 · 第${it.tracking_days}天`:'持续关注'} ⟿</span>`:'';
  return `<div class="clipwrap${lead?' big':''}" style="--rot:${rot}deg">
    <article class="clip${t2?' t2':''}" data-id="${esc(it._uid)}" tabindex="0">
      <div class="tape1"></div>${lead?'<div class="tape2"></div>':''}
      ${withClip?`<svg class="doodle obj pclip" viewBox="0 0 26 52"><path d="M8,44 L8,12 A5,5 0 0 1 18,12 L18,40 A8,8 0 0 1 2,40 L2,18"/></svg>`:''}
      ${lead?`<svg class="doodle leadstar" viewBox="0 0 44 44"><path d="M22,4 L26.5,16 L40,16 L29.5,24.5 L33.5,38 L22,30 L10.5,38 L14.5,24.5 L4,16 L17.5,16 Z"/></svg>`:''}
      ${it.needs_review?`<span class="rvmark">待核实?${rvSVG}</span>`:''}
      <div class="cutfrom">✂ 剪自 <i>${esc(it.source||'—')}</i></div>
      <h3>${esc(it.headline)}${lead?`<svg class="doodle leadline" viewBox="0 0 300 10" preserveAspectRatio="none"><path d="M2,6 Q20,1 38,6 T74,6 T110,6 T146,6 T182,6 T218,6 T254,6 T296,6"/></svg>`:''}</h3>
      ${it.repo?`<span class="repo">${esc(it.repo)}</span><br>`:''}
      <p class="body">${hl(esc(it.tldr))}</p>
      <div class="ops">
        <button class="tog"><span class="mk">▸</span> 看编辑批注</button>
        ${url?`<a href="${esc(url)}" target="_blank" rel="noopener">原文 ↗</a>`:''}
        <button class="fav${FAV.has(it._uid)?' on':''}">${FAV.has(it._uid)?'❤ 收进本子':'♡ 收进本子'}</button>
        ${track}
      </div>
      <div class="sticky"><div class="note"><span class="lbl">批注:</span>${esc(it.insight||'(这张还没写批注)')}</div></div>
    </article></div>`;
}

function ghHTML(items){
  return `<div class="ghlist">`+items.map((it,i)=>{
    const url=(it.url||'').startsWith('http')?it.url:'';
    return `<div class="ghrow" data-id="${esc(it._uid)}" style="transition-delay:${.06*i}s" tabindex="0">
      <span class="rank">${i+1}.</span>
      <span class="repo">${esc(it.headline)}${it.needs_review?'<span class="rv">待核实?</span>':''}</span>
      <span class="stars">★<span class="cnt" data-n="${Number(it.stars)||0}">0</span></span>
      <span class="qtldr">${esc(it.tldr)}</span>
      <div class="ghsticky sticky"><div class="note"><span class="lbl">批注:</span>${esc(it.insight||'(未写)')}
        ${url?` <a href="${esc(url)}" target="_blank" rel="noopener" style="font-family:inherit;color:#8a6a10">去 GitHub ↗</a>`:''}
      </div></div></div>`;
  }).join('')+`</div>`;
}

/* 在追(线索页) */
function threadsHTML(){
  const all=DATA.threads||[];
  if(!all.length)return '';
  const T=all.filter(t=>!HID.has(t.thread_id));
  const hidden=all.length-T.length;
  const rots=[-1.1,.9,-.7,1.2];
  const cards=T.map((t,i)=>{
    const tl=(t.timeline||[]).map(p=>{
      const d=/^\d{4}-(\d{2})-(\d{2})/.exec(p.date||'');
      const md=d?(+d[1])+'-'+(+d[2]):esc(p.date||'');
      const body=`${esc(p.headline)}${p.source?`<span class="tsrc">${esc(p.source)}</span>`:''}`;
      return `<div class="tevt"><span class="tdate">${md}</span>`+
        (p.url?`<a href="${esc(p.url)}" target="_blank" rel="noopener">${body}</a>`:body)+`</div>`;
    }).join('');
    return `<div class="tcardw" style="--rot:${rots[i%4]}deg;transition-delay:${.07*(i%4)}s"><div class="tcard">
      <div class="tape1"></div>
      <div class="tchead"><h3>${esc(t.name)}</h3>
        <span class="tdom">${esc(t.domain_label||t.domain||'')}</span>
        <span class="tdays">已追 ${esc(t.days)} 天</span>
        <span class="tx" data-hide="${esc(t.thread_id)}" title="不追了(仅本机隐藏,可恢复)">✕ 不追了</span></div>
      ${t.summary?`<div class="tnow"><b>现状</b>${esc(t.summary)}</div>`:''}
      <div class="tline">${tl}</div>
    </div></div>`;
  }).join('');
  const restore=hidden?`<div class="threstore">已收起 ${hidden} 条线 · <b data-unhideall>全部恢复</b></div>`:'';
  const empty=!T.length?`<aside class="vacant">在追的线都被你收起来了。</aside>`:'';
  return `<section class="page" id="pg-thr">
    <div class="phead"><h2>在追<span class="swipe"></span></h2>
      <span class="arrow"><svg class="doodle" style="position:static;width:100%;height:100%" viewBox="0 0 64 30"><path d="M4,18 C20,8 38,8 52,14 M44,8 L53,14 L45,21"/></svg></span>
      <span class="cnt"><i>${T.length}</i> 条跨天线</span></div>
    <p class="pdig">同一件事被连着几天报道,就在这里串成一条演进线。</p>
    <div class="tgrid">${cards}</div>${empty}${restore}</section>`;
}

/* 组页 */
const book=document.getElementById('book');
boards.forEach((b,bi)=>{
  const sec=document.createElement('section');
  sec.className='page'; sec.id='pg-'+b.key;
  const rots=[-1.4,1.1,-.8,1.6,-1.2,.9,-1.7,1.3,-.6,1.8,-1,1.4];
  const empty=`<aside class="vacant">今天这版<i>从缺</i>——没有过审的条目,不硬凑。</aside>`;
  sec.innerHTML=`
    <div class="phead">
      <h2>${esc(b.label)}<span class="swipe"></span></h2>
      <span class="arrow"><svg class="doodle" style="position:static;width:100%;height:100%" viewBox="0 0 64 30"><path d="M4,18 C20,8 38,8 52,14 M44,8 L53,14 L45,21"/></svg></span>
      <span class="cnt"><i>${b.items.length}</i> 张剪报</span>
    </div>
    ${b.digest?`<p class="pdig">${esc(b.digest)}</p>`:''}
    ${b.items.length===0?empty:(b.gh?ghHTML(b.items):`<div class="grid">${b.items.map((it,i)=>clipHTML(it,i,rots[i%rots.length],i%3===1,bi===0&&i===1)).join('')}</div>`)}`;
  book.appendChild(sec);
});
book.insertAdjacentHTML('beforeend',threadsHTML());

/* 交互:批注展开 + 收藏 + 在追跳转/隐藏 */
function refreshFavBtn(el,id){
  el.classList.toggle('on',FAV.has(id));
  el.textContent=FAV.has(id)?'❤ 收进本子':'♡ 收进本子';
}
function bindOpen(el){
  el.addEventListener('click',e=>{
    if(e.target.closest('a'))return;
    if(e.target.closest('[data-thr]')){document.getElementById('pg-thr')?.scrollIntoView({behavior:reduced?'auto':'smooth'});return;}
    const fav=e.target.closest('.fav');
    if(fav){const id=el.dataset.id;
      FAV.has(id)?FAV.delete(id):FAV.add(id);
      refreshFavBtn(fav,id);saveFav();renderFavs();return;}
    el.classList.toggle('open');
  });
  el.addEventListener('keydown',e=>{if(e.key==='Enter')el.classList.toggle('open');});
}
document.querySelectorAll('.clip').forEach(bindOpen);
document.querySelectorAll('.ghrow').forEach(bindOpen);
document.addEventListener('click',e=>{
  const hd=e.target.closest('[data-hide]');
  if(hd){HID.add(hd.dataset.hide);saveHide();rerenderThreads();return;}
  if(e.target.closest('[data-unhideall]')){HID.clear();saveHide();rerenderThreads();}
});
function rerenderThreads(){
  document.getElementById('pg-thr')?.remove();
  book.insertAdjacentHTML('beforeend',threadsHTML());
  const s=document.getElementById('pg-thr'); if(s){s.classList.add('in');io.observe(s);}
}

/* 收藏本抽屉 */
const favlay=document.getElementById('favlay'),favlist=document.getElementById('favlist'),
      favn=document.getElementById('favn');
function renderFavs(){
  favn.textContent=FAV.size;
  const rows=[...FAV].filter(id=>ALL[id]).map(id=>{
    const {it,b}=ALL[id];
    return `<div class="favitem" data-id="${esc(id)}">
      <div class="b">${esc(b.label)}</div>
      <div class="h">${esc(it.headline)}</div>
      <button class="rm" data-id="${esc(id)}">✕ 撕下来</button></div>`;
  }).join('');
  favlist.innerHTML=rows||`<div class="favempty">还空着呢。<br>看到喜欢的剪报,点「♡ 收进本子」,它就会躺到这页来。</div>`;
}
renderFavs();
document.getElementById('favtab').addEventListener('click',()=>{favlay.hidden=false;});
document.getElementById('favclose').addEventListener('click',()=>{favlay.hidden=true;});
favlay.addEventListener('click',e=>{if(e.target===favlay)favlay.hidden=true;});
favlist.addEventListener('click',e=>{
  const rm=e.target.closest('.rm');
  if(rm){const id=rm.dataset.id;FAV.delete(id);saveFav();
    const btn=document.querySelector(`[data-id="${CSS.escape(id)}"] .fav`);
    if(btn)refreshFavBtn(btn,id);
    renderFavs();return;}
  const item=e.target.closest('.favitem');
  if(item){favlay.hidden=true;jumpTo(item.dataset.id);}
});
function jumpTo(uid){
  const el=document.querySelector(`article[data-id="${CSS.escape(uid)}"],.ghrow[data-id="${CSS.escape(uid)}"]`);
  if(el){el.scrollIntoView({behavior:reduced?'auto':'smooth',block:'center'});el.classList.add('open');}
}

/* 检索覆盖层:全文字面匹配 + <mark> 高亮 */
const qlay=document.getElementById('qlay'),qlist=document.getElementById('qlist'),
      qsub=document.getElementById('qsub'),qinput=document.getElementById('q');
function markHit(s,q){
  const i=(s||'').toLowerCase().indexOf(q.toLowerCase());
  if(i<0)return esc(s);
  return esc(s.slice(0,i))+'<mark>'+esc(s.slice(i,i+q.length))+'</mark>'+esc(s.slice(i+q.length));
}
function doSearch(q){
  if(!q){qlay.hidden=true;return;}
  const lo=q.toLowerCase();
  const hits=[];
  Object.values(ALL).forEach(({it,b})=>{
    const blob=[it.headline,it.tldr,it.insight,it.source].join(' ').toLowerCase();
    if(blob.includes(lo))hits.push({it,b});
  });
  const hh=HIST.filter(x=>(x.h+' '+x.t+' '+x.s).toLowerCase().includes(lo));
  qsub.textContent=`「${q}」· 今天 ${hits.length} 张 · 往期 ${hh.length} 张`;
  const todayHtml=hits.map(({it,b})=>`
    <div class="qitem" data-id="${esc(it._uid)}">
      <div class="b">${esc(b.label)}</div>
      <div class="h">${markHit(it.headline,q)}</div>
      <div class="t">${markHit((it.tldr||'').slice(0,90),q)}</div>
    </div>`).join('');
  /* 从当天快照(archive/daily/)里打开时,相对路径基准不同 */
  const base=/\/archive\/daily\//.test(location.pathname)?'./':'../archive/daily/';
  const histHtml=hh.slice(0,60).map(x=>`
    <a class="qitem qh" href="${base}${esc(x.d)}.html">
      <div class="b">${esc(x.d)} · ${esc(x.dom)}</div>
      <div class="h">${markHit(x.h,q)}</div>
      <div class="t">${markHit((x.t||'').slice(0,90),q)}</div>
    </a>`).join('');
  const more=hh.length>60?`<div class="qempty">往期命中多,只摊前 60 张——加关键词收窄。</div>`:'';
  const archTip=`<a class="qarch" href="../archive/index.html">想向 AI 直接「问历史」?去「往期」页的问答面板(桌面 App 可用)→</a>`;
  qlist.innerHTML=
    (hits.length?`<div class="qsec">今天这本</div>`+todayHtml:'')+
    (hh.length?`<div class="qsec">往期册子 · 点开进当天</div>`+histHtml+more:'')+
    ((hits.length||hh.length)?'':`<div class="qempty">今天和往期都没翻到「${esc(q)}」。</div>`)+
    archTip;
  qlay.hidden=false;
}
let qtimer=null;
qinput?.addEventListener('input',e=>{clearTimeout(qtimer);
  qtimer=setTimeout(()=>doSearch(e.target.value.trim()),200);});
document.getElementById('qclose').addEventListener('click',()=>{qlay.hidden=true;});
qlay.addEventListener('click',e=>{
  if(e.target===qlay){qlay.hidden=true;return;}
  const item=e.target.closest('.qitem');
  if(item&&item.dataset.id){qlay.hidden=true;jumpTo(item.dataset.id);}
  /* 往期命中是 <a>(无 data-id),让它自然跳当天册子 */
});

/* 星数跳表 + 进场 */
function countUp(el){
  const n=Number(el.dataset.n)||0;
  if(reduced){el.textContent=n.toLocaleString();return;}
  const t0=performance.now(),D=950;
  (function f(t){const p=Math.min(1,(t-t0)/D),v=Math.round(n*(1-Math.pow(1-p,3)));
    el.textContent=v.toLocaleString(); if(p<1)requestAnimationFrame(f);})(t0);
}
const io=new IntersectionObserver(es=>{es.forEach(e=>{
  if(!e.isIntersecting)return;
  e.target.classList.add('in');
  e.target.querySelectorAll('.cnt[data-n]').forEach(countUp);
  io.unobserve(e.target);
});},{threshold:.1});
document.querySelectorAll('.page').forEach(s=>io.observe(s));
document.querySelectorAll('.grid').forEach(g=>{
  [...g.children].forEach((c,i)=>{c.style.transitionDelay=(i%5)*.09+'s';});
});
requestAnimationFrame(()=>document.body.classList.add('loaded'));

/* 标签高亮 */
const links=[...document.querySelectorAll('.bnav a[data-k]')];
const so=new IntersectionObserver(es=>{es.forEach(e=>{
  if(e.isIntersecting){const k=e.target.id.slice(3);
    links.forEach(a=>a.classList.toggle('on',a.dataset.k===k));}
});},{rootMargin:'-38% 0px -55% 0px'});
document.querySelectorAll('.page').forEach(s=>so.observe(s));
</script></body></html>"""


async def send(payload: DeliverPayload, settings: Settings, *, out_dir: Path | None = None) -> ChannelResult:
    out_dir = out_dir or (PROJECT_ROOT / "web" / "app")
    out_dir.mkdir(parents=True, exist_ok=True)

    def _copy(src: str | None) -> str | None:
        if src and Path(src).exists():
            dest = out_dir / Path(src).name
            shutil.copyfile(src, dest)
            return dest.name
        return None

    image_rel = _copy(payload.image_path)
    overview_rel = _copy(payload.overview_image_path)

    github_rel = _copy(payload.github_image_path)

    # 总第 N 本:按档案里已归档的天数(含今天)计——纯事实,失败不拖垮交付
    archive_dir = out_dir.parent / "archive"
    try:
        days = {p.stem for p in (archive_dir / "daily").glob("*.json")}
        days.add(payload.date_str)
        issue = len(days)
    except Exception:  # noqa: BLE001 — 增强字段,算不出就不显示
        issue = None

    data = {
        "title": payload.title,
        "date": payload.date_str,
        "issue": issue,           # 总第 N 本(档案天数,前端缺省容错)
        "digest": payload.digest,
        "image": image_rel,
        "overview_image": overview_rel,
        "github_image": github_rel,
        "items": payload.items,       # back-compat:主领域(AI)条目
        "github": payload.github,
        # 多领域 [{key,label,digest,items}];剥掉 image_path 等内部字段(那是飞书卡用的本地路径)
        "domains": [{k: v for k, v in d.items() if k != "image_path"} for d in payload.domains],
        "threads": payload.threads,   # 事件线「在追」[{name,domain,summary,heat,days,timeline:[...]}]
    }
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 往期索引内联(近 180 天,复用档案的 _compact_day 口径):首页「翻本子找」=今天+往期一起翻。
    # 次要增强:任何失败退成空索引,绝不拖垮主交付。今天自身不进 HIST(已在 DATA 里)。
    hist_items: list[dict] = []
    try:
        from . import archive as _archive

        daily_dir = archive_dir / "daily"
        if daily_dir.exists():
            for f in sorted(daily_dir.glob("*.json"), reverse=True)[:180]:
                if f.stem == payload.date_str:
                    continue
                try:
                    day = _archive._compact_day(json.loads(f.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001 — 单日坏档跳过,不拖垮
                    continue
                for it in day["items"]:
                    hist_items.append({"d": day["date"], "dom": it["dom"],
                                       "h": it["h"], "t": it["t"], "s": it["s"]})
    except Exception as exc:  # noqa: BLE001
        log.warning("webapp.hist_index.failed", error=str(exc))
        hist_items = []

    html = (_APP.replace("__DATA__", json.dumps(data, ensure_ascii=False))
                .replace("__HIST__", json.dumps(hist_items, ensure_ascii=False)))
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    # 全史归档(STYLE.md):当日快照 + 重建档案首页。次要产物:失败告警不拖垮主交付。
    # 档案目录从 out_dir 推导(web/app → web/archive),测试传 tmp 时落 tmp 侧、不污染真实档案。
    archived = None
    try:
        from . import archive as _archive

        _archive.snapshot_day(archive_dir, date_str=payload.date_str, html=html, data=data)
        # 自愈:daily/ 是 gitignore 的运行时产物,被清空时也确保旧系统老历史回到档案
        # (幂等,跳过已存在日期,无 legacy/ 目录则 no-op)。rebuild 由下一行统一做。
        _archive.import_legacy_history(archive_dir, rebuild=False)
        _archive.rebuild_index(archive_dir)
        archived = payload.date_str
    except Exception as exc:
        log.warning("webapp.archive.failed", error=str(exc), error_type=type(exc).__name__)

    return ChannelResult("webapp", "sent",
                         extra={"dir": str(out_dir), "image": image_rel, "overview_image": overview_rel,
                                "archived": archived})
