# 寻物导购

在 Windows 上双击 `start.cmd`。按提示输入 DeepSeek API 密钥，显示服务启动成功后打开[本机导购页](http://127.0.0.1:18766/)。输入时密钥隐藏，只用于本次进程。也可以在启动前设置环境变量 `DEEPSEEK_API_KEY`。未填写密钥时页面会提示缺少配置。

首次启动需要 Python 3.10 或以上。脚本将虚拟环境和安装临时文件放在 `E:\Codex\work\taobao-guide-agent`。

网页向 `/chat` 发送原话和会话编号。DeepSeek Flash 根据最近六轮会话决定是否查询商品、详情、优惠或订单。Python 在 `data` 中查询本地模拟数据，将结果回传给 DeepSeek 生成回答。点击回复下的“查看工具调用”可看到实际查询条件。
