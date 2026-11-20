"""Optional stdio MCP adapter over the installed CollectiGen discovery console."""

import argparse
import json
import os
from pathlib import Path
from subprocess import DEVNULL
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field


ActionID = Annotated[str, Field(max_length=80, pattern=r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+){1,7}$")]
MAX_RESULT_BYTES = 1024 * 1024


def create_server(console, settings, code_root, *, trainer_software_root=None):
    """Bind paths from trusted harness configuration before exposing tools."""
    console, settings, code_root = map(Path, (console, settings, code_root))
    if any(not path.is_absolute() or ".." in path.parts for path in (console, settings, code_root)):
        raise ValueError("Configure absolute console, settings, and application code paths.")
    if console.name.casefold() != "collectigenassist.exe" or not console.is_file():
        raise ValueError("Select the installed CollectiGenAssist.exe beside Suite Manager.")
    if not code_root.is_dir():
        raise ValueError("Select the intended suite application code root.")
    command = (str(console), "--manager-settings", str(settings), "--code-root", str(code_root))
    if trainer_software_root is not None:
        trainer_software_root = Path(trainer_software_root)
        if not trainer_software_root.is_absolute() or ".." in trainer_software_root.parts:
            raise ValueError("Configure an absolute managed trainer software root.")
        command += ("--trainer-software-root", str(trainer_software_root))
    environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP")
                   if name in os.environ}
    server = FastMCP("CollectiGen Discovery")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False)

    async def read(*arguments):
        try:
            with anyio.fail_after(60):
                result = await anyio.run_process(
                    (*command, *arguments), env=environment, cwd=console.parent,
                    stdin=DEVNULL, check=False,
                )
            if result.returncode not in (0, 1) or len(result.stdout) > MAX_RESULT_BYTES:
                raise ValueError
            payload = json.loads(result.stdout)
            if (type(payload) is not dict or type(payload.get("schema_version")) is not int
                    or payload["schema_version"] != 1):
                raise ValueError
            return payload
        except (OSError, TimeoutError, ValueError, RecursionError):
            raise RuntimeError("CollectiGen discovery could not complete. Check the configured console and scope.") from None

    @server.tool(annotations=read_only)
    async def discover_collectigen() -> dict[str, Any]:
        """List components and versioned workflow guides in the configured installation."""
        return await read("discover")

    @server.tool(annotations=read_only)
    async def describe_collectigen_action(action_id: ActionID) -> dict[str, Any]:
        """Describe one stable workflow action and report its available readiness evidence."""
        return await read("describe-action", action_id)

    @server.tool(annotations=read_only)
    async def explain_training_collection() -> dict[str, Any]:
        """Explain collection preparation and what is needed to proceed to Train."""
        return await read("explain-training-collection")

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console", required=True, type=Path)
    parser.add_argument("--manager-settings", required=True, type=Path)
    parser.add_argument("--code-root", required=True, type=Path)
    parser.add_argument("--trainer-software-root", type=Path,
                        help="Optionally verify the exact saved managed trainer software root.")
    args = parser.parse_args()
    try:
        server = create_server(args.console, args.manager_settings, args.code_root,
                               trainer_software_root=args.trainer_software_root)
    except ValueError as error:
        parser.error(str(error))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
