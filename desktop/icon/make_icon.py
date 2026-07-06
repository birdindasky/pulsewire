"""生成 Pulsewire app 图标 1024px PNG(复用项目 playwright;锁定暖白+软黑+粉)。

跑法:cd ~/Projects/pulsewire && uv run python desktop/icon/make_icon.py
产物:desktop/icon/icon-1024.png(透明背景,圆角卡片在中间,留 macOS 图标安全边距)。
随后用 sips/iconutil 转 icon.icns(见 build.sh)。
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "icon-1024.png"

# 824/1024 的圆角卡片(macOS 现代图标规范留边距);暖白底+软黑描边+粉硬投影,
# 软黑衬线 Pulse 的 "P" + 粉斜体 wire 的脉冲点,呼应刊头。
HTML = """
<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,18..72,600;1,18..72,600&display=swap" rel="stylesheet">
<style>
  html,body{margin:0;width:1024px;height:1024px;background:transparent;}
  .stage{width:1024px;height:1024px;display:flex;align-items:center;justify-content:center;}
  .card{
    position:relative;width:824px;height:824px;background:#F9F6E5;
    border:14px solid #373A3E;border-radius:150px;
    box-shadow:34px 34px 0 0 #FE6CB7;            /* 粉色硬投影 */
    display:flex;align-items:center;justify-content:center;overflow:hidden;
  }
  .p{font-family:'Newsreader',serif;font-weight:600;color:#373A3E;
     font-size:600px;line-height:1;margin-top:-30px;}
  .dot{position:absolute;width:128px;height:128px;border-radius:50%;
       background:#FE6CB7;right:150px;bottom:170px;
       box-shadow:0 0 0 22px #F9F6E5;}            /* 暖白描边把粉点从 P 里托出来 */
</style></head>
<body><div class="stage"><div class="card">
  <div class="p">P</div><div class="dot"></div>
</div></div></body></html>
"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1024, "height": 1024}, device_scale_factor=1
        )
        page.set_content(HTML)
        page.wait_for_timeout(1500)  # 等 Newsreader 字体加载
        page.screenshot(path=str(OUT), omit_background=True)
        browser.close()
    print(f"图标已生成:{OUT}")


if __name__ == "__main__":
    main()
