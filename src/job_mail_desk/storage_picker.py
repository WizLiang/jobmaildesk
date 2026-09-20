"""Native first-run picker; no app data or browser profile is created here."""
from __future__ import annotations

from pathlib import Path

from .storage_location import destination_for, StorageLocationError


def choose_initial_root(existing: Path | None) -> Path | None:
    import clr
    clr.AddReference("System")
    from System.Threading import ApartmentState, Thread, ThreadStart
    outcome = {}
    def run():
        try:
            outcome["path"] = _show_picker(existing)
        except Exception as exc:
            outcome["error"] = exc
    thread = Thread(ThreadStart(run))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("path")


def _show_picker(existing: Path | None) -> Path | None:
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Drawing import Point, Size
    from System.Windows.Forms import (
        Button, DialogResult, FolderBrowserDialog, Form, FormBorderStyle,
        FormStartPosition, Label, MessageBox, TextBox,
    )

    form = Form()
    form.Text = "JobMailDesk · 选择数据存放位置"
    form.ClientSize = Size(570, 320)
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MaximizeBox = False
    form.MinimizeBox = False
    label = Label()
    label.Text = ("请选择一个存放位置，程序会在其中创建 JobMailDeskData 文件夹。\n"
                  "待办、申请进展、配置、缓存、日志和本地导出统一保存在这里。\n"
                  "无需连接 Obsidian；邮箱授权码仍由 Windows 凭据管理器保管。")
    label.Location = Point(22, 20)
    label.Size = Size(526, 80)
    path_box = TextBox()
    path_box.ReadOnly = True
    path_box.Location = Point(22, 114)
    path_box.Size = Size(404, 30)
    browse = Button()
    browse.Text = "选择文件夹…"
    browse.Location = Point(436, 111)
    browse.Size = Size(112, 32)
    status = Label()
    status.Text = ("已有本地数据：选择新位置后，将校验并迁移数据，成功后移除旧副本。\n"
                   "外部 Obsidian 文件和手动备份保留原位置。" if existing else
                   "确认前不会创建数据文件夹。以后也可以在设置中更换位置。")
    status.Location = Point(22, 164)
    status.Size = Size(526, 65)
    accept = Button()
    accept.Text = "迁移并开始使用" if existing else "开始使用"
    accept.Enabled = False
    accept.Location = Point(393, 257)
    accept.Size = Size(155, 36)
    cancel = Button()
    cancel.Text = "取消"
    cancel.Location = Point(293, 257)
    cancel.Size = Size(88, 36)
    cancel.DialogResult = DialogResult.Cancel
    result = {"path": None}

    def select(_sender, _event):
        picker = FolderBrowserDialog()
        picker.Description = "选择数据存放位置（将在所选位置内创建 JobMailDeskData）"
        picker.ShowNewFolderButton = True
        try:
            if picker.ShowDialog(form) == DialogResult.OK:
                try:
                    destination = destination_for(str(picker.SelectedPath))
                except StorageLocationError as exc:
                    MessageBox.Show(str(exc), "无法使用此位置")
                    return
                result["path"] = destination
                path_box.Text = str(destination)
                accept.Enabled = True
        finally:
            picker.Dispose()

    def confirm(_sender, _event):
        form.DialogResult = DialogResult.OK
        form.Close()

    browse.Click += select
    accept.Click += confirm
    for control in (label, path_box, browse, status, accept, cancel):
        form.Controls.Add(control)
    if existing:
        keep = Button()
        keep.Text = "使用原位置"
        keep.Location = Point(22, 257)
        keep.Size = Size(125, 36)
        def use_existing(_sender, _event):
            result["path"] = existing
            confirm(_sender, _event)
        keep.Click += use_existing
        form.Controls.Add(keep)
    form.AcceptButton = accept
    form.CancelButton = cancel
    try:
        return result["path"] if form.ShowDialog() == DialogResult.OK else None
    finally:
        form.Dispose()
