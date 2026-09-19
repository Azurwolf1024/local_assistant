"""自家能力域的 MCP 服务器。

每个模块都要提供::

    def build_server(settings=None, skills=None, logger=None) -> MCPServer

- 被助手进程内用（``transport = "inproc"``）时，宿主会把**助手自己那份**
  settings / skills 传进来，两边共用同一份数据，不会各认一份缓存。
- 被 `python -m voice_loop.mcp.serve <模块名>` 挂到 stdio 给别人用时，
  参数全是 None，模块自己从 config.toml 建一套（独立进程，互不影响）。
"""
