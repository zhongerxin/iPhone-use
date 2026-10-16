"""Private native dialog. Output goes only to the installer child's pipe."""
import json
import sys
import tkinter as tk
from tkinter import ttk

mode = sys.argv[1]
root = tk.Tk()
root.title("Windows iPhone Control — Apple 签名")
root.attributes("-topmost", True)
root.resizable(False, False)
frame = ttk.Frame(root, padding=24)
frame.pack()
result = None
entries = {}

if mode == "credentials":
    ttk.Label(frame, text="登录 Apple 开发账号，为 WDA 签名安装", font=("Microsoft YaHei UI", 12)).pack(anchor="w")
    ttk.Label(frame, text="账号和密码仅交给本机签名进程，不写入日志。\n签名使用与 CrossCode 相同的 Sidestore Anisette 服务。\n本工具不会卸载已有 App 或撤销证书。", padding=(0, 10)).pack(anchor="w")
    for key, label, hidden in [("apple_id", "Apple 账号", False), ("password", "密码", True)]:
        ttk.Label(frame, text=label).pack(anchor="w")
        entry = ttk.Entry(frame, width=45, show="•" if hidden else "")
        entry.pack(pady=(3, 10))
        entries[key] = entry
elif mode == "otp":
    ttk.Label(frame, text="请输入 Apple 验证码（在手机上确认登录提示）").pack()
    entries["code"] = ttk.Entry(frame, width=28)
    entries["code"].pack(pady=12)
elif mode == "team":
    ttk.Label(frame, text="选择签名开发团队").pack()
    choices = json.loads(sys.argv[2])
    selected = ttk.Combobox(frame, values=choices, state="readonly", width=32)
    selected.current(0)
    selected.pack(pady=12)
else:
    raise SystemExit(2)

def submit():
    global result
    if mode == "team":
        result = {"index": selected.current()}
    else:
        values = {key: entry.get() for key, entry in entries.items()}
        if not all(values.values()):
            return
        if mode == "otp" and not (values["code"].isdigit() and len(values["code"]) == 6):
            return
        result = values
    root.destroy()

ttk.Button(frame, text="继续", command=submit).pack(side="right", padx=5)
ttk.Button(frame, text="取消", command=root.destroy).pack(side="right", padx=5)
root.bind("<Return>", lambda event: submit())
if entries:
    next(iter(entries.values())).focus_set()
root.update_idletasks()
width, height = root.winfo_width(), root.winfo_height()
root.geometry(f"+{(root.winfo_screenwidth()-width)//2}+{(root.winfo_screenheight()-height)//2}")
root.mainloop()
if result is None:
    raise SystemExit(1)
print(json.dumps(result, ensure_ascii=True))
