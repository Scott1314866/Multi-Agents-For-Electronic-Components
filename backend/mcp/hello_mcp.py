# backend/mcp/hello_server.py —— 最小 MCP Server（教学演示，不依赖任何模型）

# mcp 2.x：FastMCP 改名为 MCPServer（mcp.server.fastmcp 模块已移除）
from mcp.server.mcpserver import MCPServer

# 创建一个 MCP Server 实例；传输层参数（stateless_http / json_response）
# 在 streamable_http_app() 时指定，见底部 __main__
mcp = MCPServer(name="HelloServer")


@mcp.tool()                          # 就这一个装饰器，把下面的函数注册成了一个 MCP 工具
async def add(a: int, b: int) -> int:
    """把两个整数相加（演示 MCP 工具的最小形态）"""   # docstring 会成为工具描述
    return a + b                     # 返回值由 MCPServer 自动包成 JSON 回给调用方


if __name__ == "__main__":
    import uvicorn
    # streamable_http_app() 把 mcp 变成标准 ASGI 应用，uvicorn 直接跑；端点为 /mcp
    # stateless_http / json_response 从 mcp 1.x 的构造参数挪到了这里（mcp 2.x 新封装要求）
    uvicorn.run(
        mcp.streamable_http_app(stateless_http=True, json_response=True),
        host="0.0.0.0",
        port=8009,
    )