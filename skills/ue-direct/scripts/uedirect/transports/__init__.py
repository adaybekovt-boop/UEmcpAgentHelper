"""Transports deliver one Python snippet to the running editor and return its text output.

* ``mcp``    - Epic's Unreal MCP plugin (UE 5.8+, Streamable HTTP, ``execute_python`` tool).
* ``remote`` - PythonScriptPlugin remote execution (UDP multicast discovery + TCP command channel).
* ``relay``  - any other MCP server with a python tool: UE-Direct prints the snippet, the
               agent runs it through its own MCP client, then calls ``ued result <id>``.

Results never depend on transport output: the editor writes a response file.
"""

from .base import Transport, TransportError  # noqa: F401
