import subprocess
from mcp.server.fastmcp import FastMCP 

mcp = FastMCP("amux", json_response=True)

def _send_enter_to_pane(pane_id: int):
    subprocess.run(
        ["wezterm", "cli", "send-text", f"--pane-id={pane_id}", "--no-paste"],
        input="\r",
        text=True,
    )

@mcp.tool()
def send_message_to_pane(pane_id: int, message: str):
    subprocess.run(
        ["wezterm", "cli", "send-text", f"--pane-id={pane_id}", "--no-paste"],
        input=message,
        text=True,
    )
    _send_enter_to_pane(pane_id)
    return "sent!"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
