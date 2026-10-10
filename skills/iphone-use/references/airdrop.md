# AirDrop 文件传回 Mac

手机里的图片、视频、文档等需要在 Mac 上读取、处理或交付时，可用 PUA 操作所在 App 的共享 / 导出菜单，通过 AirDrop 传回当前 Mac。

1. 在手机选择所需文件或照片 → 共享 → AirDrop → 当前 Mac。设备名不清楚时可用 `scutil --get ComputerName` 查看；Mac 出现接收提示时接受。
2. 发送后，到 `~/Downloads` 按最近时间找到收到的文件或文件夹，用绝对路径直接读取、处理或交付。
