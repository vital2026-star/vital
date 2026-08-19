# 唯特偶税务数据库 · 云端版

任何电脑点开这个链接都能用：`https://你的用户名.github.io/你的仓库名/vital_tax_database.html`

## 这是什么

- `vital_tax_database.html` —— 税务数据库网页（主体 / 税种 / 归档 / 案例 / 合规 / 提醒 六个面板）
- `tax_updates_archive.json` —— 税政归档数据，由下面这个定时任务自动更新
- `tax_updater.py` —— 自动抓取国家税务总局政策指引库并去重合并的更新器
- `.github/workflows/update.yml` —— GitHub Actions 定时任务：**每天 01:30（UTC）自动抓取并更新归档**，也支持手动触发
- `tax_cases_archive.json` —— 处罚案例（人工维护）

## 部署步骤（一次性，约 5 分钟）

1. 到 github.com 注册/登录账号。
2. 新建仓库（Repository），名字随意，例如 `vital-tax`，选 **Public**（免费）。
3. 在仓库里手动上传上面 5 个文件（网页里直接点 Upload files 即可），或让配置好账号的人帮你 push。
4. 进入仓库 **Settings → Pages**，Source 选 "Deploy from a branch"，Branch 选 `main / (root)`，保存。
5. 等一两分钟，访问 `https://你的用户名.github.io/你的仓库名/vital_tax_database.html`。

## 说明

- 更新是**定时自动**的；点击页面里的「手动重新加载」可刷新数据无需手动改任何文件。
- 要立即手动触发更新：仓库 **Actions** 页面 → 选中本工作流 → *Run workflow*。
- 打开的是公开网页，数据按需只放不涉密的内容。