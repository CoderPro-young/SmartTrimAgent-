# MUSIC/ —— 内置曲库（自动配乐候选池）

一键成片（one_click_reel / smart_create）自动配乐的**内置候选池**（V7.5.1）。
编译器展开时会把它和 `INPUT/` 里的纯音频一起作为 BGM 候选，`MUSIC/` 优先。

## 来源与授权

- **来源 1**：[Mixkit Free Stock Music](https://mixkit.co/free-stock-music/)（分类页列表 + JSON-LD 结构化数据抓取）
  —— [Mixkit Stock Music Free License](https://mixkit.co/license/#musicFree)：免费商用、**无需署名**。
- **来源 2**：[Incompetech](https://incompetech.com/music/royalty-free/music.html)（Kevin MacLeod 曲库，直链下载）
  —— [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)：免费商用，**必须署名**——
  在视频简介/致谢中注明 `Music: <曲名> by Kevin MacLeod (incompetech.com), Licensed under CC BY 4.0`；
  smart_create 的 credits 字幕可承载这行署名。
- ⚠️ **注意**：Mixkit 授权**禁止把原始音频文件独立再分发**（供他人下载）。
  因此 `MUSIC/` 已加入 `.gitignore`，**不要提交到公开仓库**；每首曲子的
  来源信息都记录在 `meta.json`，需要时任何人可用同样的链接自行下载。
- ⚠️ 商业歌曲（如 We Are the World 等受版权保护的作品）不在可下载范围，
  只能由使用者自备有授权的音频文件。

## 曲目清单（21 首，约 120 MB）

| 文件 | 曲名 | 作者 | 风格 | 时长 |
| --- | --- | --- | --- | --- |
| incompetech-carefree | Carefree | Kevin MacLeod | Pop | 3:25 |
| incompetech-inspired | Inspired | Kevin MacLeod | Film Score | 4:46 |
| incompetech-life-of-riley | Life of Riley | Kevin MacLeod | Pop | 3:55 |
| incompetech-monkeys-spinning-monkeys | Monkeys Spinning Monkeys | Kevin MacLeod | Children | 2:05 |
| incompetech-wallpaper | Wallpaper | Kevin MacLeod | Pop | 3:40 |
| mixkit-a-man-with-a-broken-heart-788 | A Man with a Broken Heart | Michael Ramir C. | Rock | 3:22 |
| mixkit-adone-635 | Adone | Eugenio Mininni | Funk | 3:02 |
| mixkit-classical-10-717 | Classical 10 | Jonny S. | Film Score | 3:32 |
| mixkit-deep-techno-ambience-134 | Deep Techno Ambience | Alejandro Magaña (A. M.) | Techno | 2:02 |
| mixkit-deep-urban-623 | Deep Urban | Eugenio Mininni | House | 4:49 |
| mixkit-do-you-know-god-s-name-775 | Do You Know God's Name? | Mauro Urbina | House | 4:54 |
| mixkit-drawing-the-sky-606 | Drawing the Sky | Eugenio Mininni | Orchestral Pop | 3:27 |
| mixkit-dreaming-of-you-952 | Dreaming of You | Michael Ramir C. | Film Score | 2:29 |
| mixkit-echoes-188 | Echoes | Andrew Ev | Atmospheres | 3:46 |
| mixkit-forest-walk-607 | Forest Walk | Eugenio Mininni | Electronica | 2:54 |
| mixkit-games-music-706 | Games Music | Grigoriy Nuzhny | Trailer Music | 1:58 |
| mixkit-gear-180 | Gear | Andrew Ev | EDM | 4:06 |
| mixkit-i-believe-in-us-1030 | I Believe in Us | Michael Ramir C. | Pop | 2:48 |
| mixkit-i-ll-take-you-to-my-heart-957 | I'll Take You to My Heart | Michael Ramir C. | Film Score | 2:43 |
| mixkit-just-kidding-11 | Just Kidding | Ahjay Stelino | Children | 2:59 |
| mixkit-kroks-591 | Kroks | Eugenio Mininni | Rock | 3:48 |
| mixkit-like-a-loop-machine-876 | Like a Loop Machine | Michael Ramir C. | Hip Hop | 2:09 |
| mixkit-machine-drum-vibes-117 | Machine Drum Vibes | Alejandro Magaña (A. M.) | Techno | 2:12 |
| mixkit-moon-walk-609 | Moon Walk | Eugenio Mininni | Electronica | 3:25 |
| mixkit-motivation-gets-in-the-way-519 | Motivation Gets in the Way | Diego Nava | Rock | 3:03 |
| mixkit-never-going-broke-301 | Never Going Broke | Arulo | Trap | 2:13 |
| mixkit-old-letter-854 | Old Letter | Michael Ramir C. | Pop | 2:45 |
| mixkit-playground-fun-12 | Playground Fun | Ahjay Stelino | Children | 3:02 |
| mixkit-pop-03-700 | Pop 03 | Grigoriy Nuzhny | Jazz | 2:51 |
| mixkit-pop-04-696 | Pop 04 | Grigoriy Nuzhny | Funk | 3:10 |
| mixkit-pop-181 | Pop | Andrew Ev | House | 4:26 |
| mixkit-rest-now-584 | Rest Now | Eugenio Mininni | Ambient | 5:00 |
| mixkit-river-flow-594 | River Flow | Eugenio Mininni | Electronica | 4:30 |
| mixkit-skyline-601 | Skyline | Eugenio Mininni | Classical | 3:26 |
| mixkit-smooth-like-jazz-24 | Smooth Like Jazz | Ahjay Stelino | Jazz | 2:38 |
| mixkit-vastness-184 | Vastness | Andrew Ev | Ambient | 3:50 |
| mixkit-voxscape-571 | Voxscape | Eugenio Mininni | Ambient | 5:00 |
| mixkit-wind-leaves-617 | Wind Leaves | Eugenio Mininni | Country | 3:25 |
| mixkit-your-breath-634 | Your Breath | Eugenio Mininni | Corporate Music | 3:56 |

完整溯源字段（source_url / category_page / license / downloaded_at / 实测时长）
与**选曲语义标签**（`tags`——Mixkit 编辑标注的情绪/场景/乐器标签，选曲
模型按「轻松欢快」「旅游」等氛围描述匹配的主要依据）见 [meta.json](./meta.json)。

## 打标 / 扩充

- **标签抓取**（零密钥）：`.venv/Scripts/python.exe scripts/label_music.py`
  —— 从分类页条目卡片抓每首曲子的人工标签写回 meta.tags；
- **听感打标升级**（可选）：配置 `DASHSCOPE_API_KEY` 后
  `.venv/Scripts/python.exe scripts/label_music.py --provider omni --force`
  —— 用 qwen3-omni-flash 真听音频生成 mood/scene/energy/描述
  （FireRed omni_bgm_label 同款思路）；
- **加新曲**：下载到本目录（命名 `<来源>-<slug>.mp3`，如 `mixkit-<slug>-<id>.mp3` /
  `incompetech-<slug>.mp3`）→ meta.json 补一条记录（file/title/artist/genre/
  duration/source_url/category_page/license）→ 有编辑标注来源的跑一遍标签抓取，
  其余手工补 tags。
