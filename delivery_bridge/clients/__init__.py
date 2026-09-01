"""执行器客户端：拉起 Codex / Claude，跑回合，读回会话正文。

这一层只负责「怎么和执行器说话」，不知道任务面板的存在：
- codex.py   Codex app-server 的 JSON-RPC 会话
- claude.py  Claude Code 的 print 模式子进程与本地 transcript
- journal.py Codex 读不回来的那部分回合条目，自己落盘再合并回去
- factory.py 按 provider 造客户端
- pool.py    只读会话的执行器复用池与短 TTL 快照
"""
