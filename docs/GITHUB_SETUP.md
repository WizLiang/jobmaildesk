# Windows 上维护 GitHub 仓库

此定制版的目标仓库为 `git@github.com:WizLiang/jobmaildesk.git`。
Git 仓库根目录应为当前源码目录（本地交付目录中的 `source`），不要把整个交付目录上传。
Python 源码、测试、构建脚本、依赖锁文件和许可证需要提交；邮箱数据、配置、密钥和安装包不进入源码 Git 历史。

## SSH 连接

本项目采用单仓库 Deploy key。把 `.pub` 文件内容添加到仓库的
[Deploy keys 设置](https://github.com/WizLiang/jobmaildesk/settings/keys)，需要推送时勾选 Allow write access。
这把密钥只授权此仓库；不要为本项目另行添加账号级 SSH key。
私钥不上传、不粘贴到网页，也不放入仓库。Git 的本地 SSH 配置显式指定密钥和 IdentitiesOnly，避免自动尝试其他密钥。

添加后通过 `git ls-remote git@github.com:WizLiang/jobmaildesk.git` 验证仓库读取权限。
认证失败时先核对 Deploy key 是否添加到正确仓库；无需重复初始化仓库。

## 首次导入

1. 检查远端分支和提交。已有内容先取回比较，不强制推送，不覆盖远端历史。
2. 确认 `.gitignore` 生效，审阅每个待提交文件及文档中的个人信息。
3. 在功能分支提交，执行完整测试、秘密扫描和 `git diff --check` 后推送。
4. 按 `PROJECT_RULES.md` 通过 Pull Request 合入主线；空仓库需先确定初始主线。

Windows 测试包已通过 [Releases](https://github.com/WizLiang/jobmaildesk/releases) 分发。推送源码不会自动更新朋友电脑上的程序；只有通过发布检查并附有完整 ZIP 和 SHA-256 的 Release 才会被更新器识别。
Deploy key 负责 Git 拉取和推送，不提供创建或合并 Pull Request 的 API 权限。使用 GitHub 网页创建与合并 PR；无需为此把部署密钥改成账号级密钥。
日常版本管理和验收步骤见 [维护手册](MAINTENANCE.md)。

## 日常同步

在源码目录使用 Git 或 GitHub Desktop 拉取更新、创建功能分支、提交和推送。
运行数据继续留在应用中选择的本地目录，每位使用者自行配置邮箱。
升级便携版时退出旧程序，替换完整程序文件夹，沿用已选择的数据位置。
