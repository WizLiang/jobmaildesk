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

当前已有 Windows 本地候选包，但尚不代表正式发布完成。
推送源码不会自动更新朋友电脑上的程序，也不应顺带创建版本标签或 Release。
后续发布需完成项目规定的验证；安装包届时作为 Release 附件分发。

## 日常同步

在源码目录使用 Git 或 GitHub Desktop 拉取更新、创建功能分支、提交和推送。
运行数据继续留在应用中选择的本地目录，每位使用者自行配置邮箱。
升级便携版时退出旧程序，替换完整程序文件夹，沿用已选择的数据位置。
