"""AppleScript transport. No database access, shell execution, or arbitrary scripts."""

import asyncio
import json
import platform
import re
from datetime import date
from importlib.resources import files

from .models import Fields, Kind, Query, Target


class AutomationError(RuntimeError):
    pass


def literal(value: str) -> str:
    """Encode data as one AppleScript string; never interpolate unquoted input."""
    if "\0" in value:
        raise ValueError("NUL is not supported")
    return (
        '"'
        + (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        + '"'
    )


CLASSES = {"to-do": "to do", "project": "project", "area": "area", "tag": "tag"}
COLLECTIONS = {
    "to-do": "to dos",
    "project": "projects",
    "area": "areas",
    "tag": "tags",
    "list": "lists",
    "selected": "selected to dos",
}


def reference(kind: str, item_id: str) -> str:
    return f"{CLASSES[kind]} id {literal(item_id)}"


def container_ref(target: Target) -> str:
    cls = "list" if target.kind == "list" else CLASSES[target.kind]
    return f"{cls} id {literal(target.id)}"


class AppleScript:
    def __init__(self, timeout: float = 45):
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.prelude = files("things_mcp").joinpath("serialization.applescript").read_text()

    async def run(self, body: str):
        if platform.system() != "Darwin":
            raise AutomationError("Things automation requires a local macOS session.")
        async with self.lock:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate((self.prelude + "\n" + body).encode()), self.timeout
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AutomationError(
                    "Automation timed out. The outcome may be partial or unknown. "
                    "Check Things before retrying a write. Check macOS Automation permissions."
                ) from None
            if process.returncode:
                # AppleScript diagnostics can contain notes or URL secrets. Never relay them.
                match = re.search(rb"\((-?\d+)\)\s*$", stderr)
                code = match[1].decode() if match else "unknown"
                hint = {
                    "-1743": (
                        "Allow the launching app to control Things in "
                        "macOS Privacy & Security > Automation."
                    ),
                    "-1728": "The requested item or container does not exist.",
                    "-128": "The user canceled the automation request.",
                    "-2700": (
                        "A precondition failed. Check target IDs, "
                        "existing tag names, and tag hierarchy."
                    ),
                }.get(
                    code, "Things rejected the operation. Check the tool arguments and app state."
                )
                raise AutomationError(
                    f"AppleScript error {code}. {hint} "
                    "A write may be partial; inspect before retrying."
                )
            try:
                return json.loads(stdout)
            except (ValueError, UnicodeDecodeError):
                raise AutomationError(
                    "Invalid automation response. Inspect Things before retrying."
                ) from None

    async def health(self):
        return await self.run("""
set output to current application's NSMutableDictionary's new()
tell application "Things3"
    my putValue(output, "version", version)
end tell
return my jsonText(output)
""")

    async def get(self, kind: Kind, item_id: str):
        return await self.run(f"""
tell application "Things3"
    set targetItem to {reference(kind, item_id)}
    return my jsonText(my snapshot(targetItem, {literal(kind)}))
end tell
""")

    async def query(self, query: Query):
        collection = COLLECTIONS[query.kind]
        if query.container:
            collection += f" of {container_ref(query.container)}"
        filters = []
        if query.text is not None:
            needle = literal(query.text)
            if query.kind in ("to-do", "project", "selected"):
                filters.append(f"(name contains {needle} or notes contains {needle})")
            else:
                filters.append(f"name contains {needle}")
        if query.status:
            filters.append(f"status is {query.status}")
        if query.tag:
            # Match complete tag names, including inherited behavior only if Things exposes it.
            filters.append(f"name of tags contains {literal(query.tag)}")
        if filters:
            collection = f"({collection} whose ({' and '.join(filters)}))"
        return await self.run(f"""
set output to current application's NSMutableDictionary's new()
set results to current application's NSMutableArray's new()
tell application "Things3"
    set foundItems to {collection}
    set totalCount to count of foundItems
    set lastIndex to {query.offset + query.limit}
    if lastIndex > totalCount then set lastIndex to totalCount
    if {query.offset} < totalCount then
        repeat with itemIndex from {query.offset + 1} to lastIndex
            results's addObject:(my snapshot(item itemIndex of foundItems, {literal(query.kind)}))
        end repeat
    end if
end tell
my putValue(output, "items", results)
my putValue(output, "total", totalCount)
my putValue(output, "offset", {query.offset})
set nextOffset to missing value
if lastIndex < totalCount then set nextOffset to lastIndex
my putValue(output, "next_offset", nextOffset)
return my jsonText(output)
""")

    def properties(self, fields: Fields):
        props = []
        checks = []
        clears = []
        for key, value in fields.model_dump(exclude_none=True).items():
            if key == "title":
                props.append(f"name:{literal(value)}")
            elif key == "notes":
                props.append(f"notes:{literal(value)}")
            elif key == "tags":
                for tag in value:
                    checks.append(
                        f'if not (exists tag named {literal(tag)}) then error "Unknown tag"'
                    )
                props.append(f"tag names:{literal(', '.join(value))}")
            elif key == "status":
                props.append(f"status:{value}")
            elif key == "deadline":
                if isinstance(value, date):
                    expr = f"my localDate({value.year}, {value.month}, {value.day})"
                    props.append(f"due date:({expr})")
                else:
                    clears.append("delete due date of targetItem")
            elif key == "keyboard_shortcut":
                props.append(f"keyboard shortcut:{literal(value)}")
            elif key == "parent_tag_id":
                if value:
                    checks.append(f"set parentTag to {reference('tag', value)}")
                    checks.append('if not (exists parentTag) then error "Unknown parent tag"')
                    props.append("parent tag:parentTag")
                else:
                    clears.append("delete parent tag of targetItem")
        return "\n".join(checks), "{" + ", ".join(props) + "}", "\n".join(clears)

    async def create(self, kind: Kind, fields: Fields, container: Target | None = None):
        fields.check_kind(kind, creating=True)
        if container and (kind != "to-do" and not (kind == "project" and container.kind == "area")):
            raise ValueError("only tasks accept any container; projects accept an area")
        checks, props, clears = self.properties(fields)
        placement = ""
        if container:
            target = container_ref(container)
            checks += f'\nif not (exists {target}) then error "Unknown container"'
            if container.kind == "list":
                placement = f"move targetItem to {target}"
            else:
                placement = f"set {container.kind} of targetItem to {target}"
        return await self.run(f"""
tell application "Things3"
    {checks}
    set targetItem to make new {CLASSES[kind]} with properties {props}
    {placement}
    {clears}
    return my jsonText(my snapshot(targetItem, {literal(kind)}))
end tell
""")

    async def update(self, kind: Kind, item_id: str, fields: Fields):
        fields.check_kind(kind)
        checks, props, clears = self.properties(fields)
        if fields.parent_tag_id:
            checks += """
set ancestor to parentTag
repeat while ancestor is not missing value
    if id of ancestor is id of targetItem then error "Tag hierarchy cycle"
    set ancestor to parent tag of ancestor
end repeat
"""
        return await self.run(f"""
tell application "Things3"
    set targetItem to {reference(kind, item_id)}
    if not (exists targetItem) then error "Unknown item"
    {checks}
    {"set properties of targetItem to " + props if props != "{}" else ""}
    {clears}
    return my jsonText(my snapshot(targetItem, {literal(kind)}))
end tell
""")

    async def move(self, kind: Kind, item_id: str, target: Target | None):
        if kind not in ("to-do", "project"):
            raise ValueError("only tasks and projects can move")
        if kind == "project" and target and target.kind == "project":
            raise ValueError("projects cannot contain projects")
        if target is None:
            action = (
                "if project of targetItem is not missing value then delete project of targetItem\n"
                "if area of targetItem is not missing value then delete area of targetItem"
            )
            if kind == "project":
                action = "if area of targetItem is not missing value then delete area of targetItem"
        elif target.kind == "list":
            action = f"move targetItem to {container_ref(target)}"
        else:
            action = f"set {target.kind} of targetItem to {container_ref(target)}"
        return await self.run(f"""
tell application "Things3"
    set targetItem to {reference(kind, item_id)}
    {action}
    return my jsonText(my snapshot(targetItem, {literal(kind)}))
end tell
""")

    async def delete(self, kind: Kind, item_id: str):
        return await self.run(f"""
tell application "Things3"
    set targetItem to {reference(kind, item_id)}
    set output to my snapshot(targetItem, {literal(kind)})
    delete targetItem
end tell
my putValue(output, "operation", "delete")
return my jsonText(output)
""")

    async def dispatch_url(self, url: str):
        if not url.startswith("things:///"):
            raise ValueError("only Things URLs are supported")
        result = literal(
            json.dumps(
                {
                    "status": "dispatched",
                    "verified": False,
                    "message": "Read the item to verify. Do not blindly retry creations.",
                }
            )
        )
        return await self.run(f"open location {literal(url)}\nreturn {result}")
